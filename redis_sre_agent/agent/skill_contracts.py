"""Check retrieved skill contracts and construct follow-up instructions.

These pure checks do not call an LLM, execute tools, or persist agent state.
"""

import re
from typing import Any, Dict, List, Optional

from langchain_core.messages import BaseMessage, SystemMessage

from redis_sre_agent.skills.contracts import TEMPLATE_PLACEHOLDER_RE, template_segment_to_pattern

from .helpers import extract_last_ai_response


def _normalize_contract_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _contract_requirement_label(requirement: Any) -> str:
    if isinstance(requirement, dict):
        operation = str(requirement.get("operation") or "").strip()
        server_name = str(requirement.get("server_name") or "").strip()
        if operation and server_name:
            return f"{server_name}.{operation}"
        if operation:
            return operation
    return str(requirement or "").strip()


def _tool_requirement_matches(requirement: Any, envelope: Dict[str, Any]) -> bool:
    tool_key = _normalize_contract_token(envelope.get("tool_key"))
    tool_name = _normalize_contract_token(envelope.get("name"))
    candidates = [candidate for candidate in (tool_key, tool_name) if candidate]
    if not candidates:
        return False

    if isinstance(requirement, dict):
        required_operation = _normalize_contract_token(requirement.get("operation"))
        required_server = _normalize_contract_token(requirement.get("server_name"))
        if not required_operation:
            return False
        for candidate in candidates:
            if not (
                candidate == required_operation or candidate.endswith(f"_{required_operation}")
            ):
                continue
            if required_server and required_server not in candidate:
                continue
            return True
        return False

    required = _normalize_contract_token(requirement)
    if not required:
        return False
    return any(
        candidate == required or candidate.endswith(f"_{required}") for candidate in candidates
    )


def _required_pattern_entries(output_contract: Dict[str, Any]) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    for item in output_contract.get("required_patterns") or []:
        if isinstance(item, dict):
            pattern = str(item.get("pattern") or "").strip()
            description = str(item.get("description") or pattern).strip()
        else:
            pattern = str(item or "").strip()
            description = pattern
        if pattern:
            entries.append({"pattern": pattern, "description": description})
    return entries


def _contract_template_token_matches(token: str, response_text: str) -> bool:
    if TEMPLATE_PLACEHOLDER_RE.search(token):
        return re.search(rf"(?m)^{template_segment_to_pattern(token)}$", response_text) is not None
    return token in response_text


def _missing_output_contract_items(
    output_contract: Dict[str, Any],
    response_text: str,
) -> List[str]:
    if not response_text.strip():
        return ["Return the required markdown document."]

    missing: List[str] = []
    for line in output_contract.get("required_preamble_lines") or []:
        literal = str(line or "").strip()
        if "<" in literal:
            literal = literal.split("<", 1)[0].rstrip()
        if literal and literal not in response_text:
            missing.append(f"Include `{literal}` in the report preamble.")

    cursor = 0
    for heading in output_contract.get("required_order") or []:
        token = str(heading or "").strip()
        if not token:
            continue
        position = response_text.find(token, cursor)
        if position == -1:
            missing.append(f"Include heading `{token}` in the required order.")
            continue
        cursor = position + len(token)

    for heading in output_contract.get("required_subsections") or []:
        token = str(heading or "").strip()
        if not token or _contract_template_token_matches(token, response_text):
            continue
        if TEMPLATE_PLACEHOLDER_RE.search(token):
            missing.append(f"Include a subsection matching `{token}`.")
            continue
        missing.append(f"Include subsection `{token}`.")

    for pattern_entry in _required_pattern_entries(output_contract):
        try:
            if re.search(pattern_entry["pattern"], response_text):
                continue
        except re.error:
            pass
        missing.append(pattern_entry["description"])

    return missing


def _extract_active_skill_contracts(
    envelopes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    contracts: List[Dict[str, Any]] = []
    for envelope in envelopes:
        data = envelope.get("data")
        if not isinstance(data, dict):
            continue
        output_contract = data.get("output_contract")
        workflow_contract = data.get("workflow_contract")
        contract_summary = data.get("contract_summary")
        if not output_contract and not workflow_contract and not contract_summary:
            continue
        contracts.append(
            {
                "skill_name": str(
                    data.get("skill_name") or envelope.get("name") or "skill"
                ).strip(),
                "output_contract": output_contract if isinstance(output_contract, dict) else {},
                "workflow_contract": (
                    workflow_contract if isinstance(workflow_contract, dict) else {}
                ),
                "contract_summary": [
                    str(item).strip() for item in (contract_summary or []) if str(item).strip()
                ],
            }
        )
    return contracts


def _collect_skill_contract_gaps(
    envelopes: List[Dict[str, Any]],
    messages: List[BaseMessage],
) -> List[Dict[str, Any]]:
    response_text = extract_last_ai_response(messages, terminal_only=True)
    gaps: List[Dict[str, Any]] = []
    for contract in _extract_active_skill_contracts(envelopes):
        workflow_contract = contract["workflow_contract"]
        output_contract = contract["output_contract"]
        missing_tools = [
            _contract_requirement_label(requirement)
            for requirement in (workflow_contract.get("required_tool_calls") or [])
            if not any(_tool_requirement_matches(requirement, envelope) for envelope in envelopes)
        ]
        missing_output = (
            _missing_output_contract_items(output_contract, response_text) if response_text else []
        )
        if not missing_tools and not missing_output:
            continue
        gaps.append(
            {
                "skill_name": contract["skill_name"],
                "missing_tools": missing_tools,
                "missing_output": missing_output,
                "template": str(output_contract.get("template") or "").strip(),
                "required_followups": [
                    str(item).strip()
                    for item in (workflow_contract.get("required_followups") or [])
                    if str(item).strip()
                ],
                "contract_summary": contract["contract_summary"],
            }
        )
    return gaps


def _build_skill_contract_repair_message(
    envelopes: List[Dict[str, Any]],
    messages: List[BaseMessage],
    gaps: Optional[List[Dict[str, Any]]] = None,
) -> Optional[SystemMessage]:
    gaps = gaps if gaps is not None else _collect_skill_contract_gaps(envelopes, messages)
    if not gaps:
        return None

    lines = [
        "Binding skill contract reminder:",
        "Do not finalize yet until you satisfy the retrieved skill contract.",
    ]
    for gap in gaps:
        lines.append(f"Skill: `{gap['skill_name']}`")
        if gap["missing_tools"]:
            lines.append(
                "Missing required tool calls: "
                + ", ".join(f"`{name}`" for name in gap["missing_tools"])
            )
        if gap["required_followups"]:
            lines.append("Required follow-up rules:")
            for item in gap["required_followups"]:
                lines.append(f"- {item}")
        if gap["missing_output"]:
            lines.append("Missing required output constraints:")
            for item in gap["missing_output"]:
                lines.append(f"- {item}")
    lines.append(
        "When the contract requires exact headings or layout, copy them verbatim. "
        "Return only the required markdown document and do not append extra footer text "
        "unless the contract explicitly asks for it."
    )
    return SystemMessage(content="\n".join(lines))


def _skill_contract_gaps_need_followup(gaps: List[Dict[str, Any]]) -> bool:
    return any(gap["missing_output"] and not gap["missing_tools"] for gap in gaps)
