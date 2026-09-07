"""Skill-following checks remain intact after moving them out of the graph."""

from langchain_core.messages import AIMessage

from redis_sre_agent.agent.skill_contracts import (
    _build_skill_contract_repair_message,
    _collect_skill_contract_gaps,
)


def test_contract_requires_both_evidence_and_report_structure():
    envelopes = [
        {
            "name": "incident_skill",
            "data": {
                "skill_name": "incident",
                "workflow_contract": {"required_tool_calls": ["info"]},
                "output_contract": {"required_order": ["## Findings", "## Next steps"]},
            },
        }
    ]
    messages = [AIMessage(content="## Findings\nRedis is healthy.")]
    gaps = _collect_skill_contract_gaps(envelopes, messages)
    assert gaps[0]["missing_tools"] == ["info"]
    assert "## Next steps" in gaps[0]["missing_output"][0]
    reminder = _build_skill_contract_repair_message(envelopes, messages)
    assert "Missing required tool calls: `info`" in reminder.content

    envelopes.append({"tool_key": "redis_123abc_info", "name": "info", "data": {}})
    complete = [AIMessage(content="## Findings\nEvidence.\n## Next steps\nVerify.")]
    assert _collect_skill_contract_gaps(envelopes, complete) == []
    assert _build_skill_contract_repair_message(envelopes, complete) is None
