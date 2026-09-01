"""Helpers for source-document parsing and metadata normalization."""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...pipelines.scraper.base import (
    DocumentCategory,
    DocumentType,
    ScrapedDocument,
    SeverityLevel,
)

logger = logging.getLogger(__name__)

CATEGORY_NAME_MAP = {
    "oss": DocumentCategory.OSS,
    "shared": DocumentCategory.SHARED,
}

SEVERITY_NAME_MAP = {
    "critical": SeverityLevel.CRITICAL,
    "high": SeverityLevel.HIGH,
    "warning": SeverityLevel.MEDIUM,
    "medium": SeverityLevel.MEDIUM,
    "normal": SeverityLevel.MEDIUM,
    "low": SeverityLevel.LOW,
    "info": SeverityLevel.LOW,
}

RESERVED_METADATA_KEYS = {
    "file_path",
    "file_size",
    "original_category",
    "original_severity",
    "original_doc_type",
    "determined_category",
    "doc_type",
    "name",
    "summary",
    "priority",
    "pinned",
    "source_document_path",
    "source_document_scope",
    "retrieval_category",
}

OPENAPI_METHODS = {"delete", "get", "head", "options", "patch", "post", "put", "trace"}
RELEASE_EXAMPLE_DIRECTORY_PATTERN = re.compile(r"release-v[\w.-]+-example", re.IGNORECASE)


def parse_bool(value: Any, default: bool = False) -> bool:
    """Best-effort boolean parser for chunk metadata fields."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y", "on"}:
        return True
    if normalized in {"false", "0", "no", "n", "off"}:
        return False
    return default


def strip_yaml_front_matter(text: str) -> tuple[str, bool]:
    """Remove YAML front-matter delimited by leading --- blocks."""
    if not text.startswith("---"):
        return text, False

    try:
        end_idx = text.find("\n---", 3)
        if end_idx == -1:
            return text, False
        closing_line_end = end_idx + len("\n---")
        remainder = text[closing_line_end:]
        if remainder.startswith("\n"):
            remainder = remainder[1:]
        return remainder, True
    except Exception:
        return text, False


def normalize_metadata_key(key: str) -> str:
    """Normalize metadata keys into snake_case aliases."""
    normalized = re.sub(r"[\s-]+", "_", key.strip().lower())
    return re.sub(r"[^\w]", "", normalized)


def _is_excluded_source_path(path: Path, source_dir: Path) -> bool:
    """Return whether a local source is a release fixture rather than corpus content."""
    try:
        relative_parts = path.resolve().relative_to(source_dir.resolve()).parts[:-1]
    except ValueError:
        relative_parts = path.parts[:-1]
    return any(RELEASE_EXAMPLE_DIRECTORY_PATTERN.fullmatch(part) for part in relative_parts)


def parse_markdown_metadata(content: str) -> Dict[str, str]:
    """Extract metadata from a markdown source document."""
    metadata: Dict[str, str] = {}
    front_matter_match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", content, re.DOTALL)
    if front_matter_match:
        front_matter = front_matter_match.group(1)
        for line in front_matter.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            metadata[normalize_metadata_key(key)] = value.strip().strip('"').strip("'")

    title_match = re.search(r"^# (.+)", content, re.MULTILINE)
    if title_match and "title" not in metadata:
        metadata["title"] = title_match.group(1).strip()

    metadata_pattern = r"^\*\*([^*]+)\*\*:\s*(.+)$"
    for match in re.finditer(metadata_pattern, content, re.MULTILINE):
        key = normalize_metadata_key(match.group(1))
        if key in metadata:
            continue
        metadata[key] = match.group(2).strip()

    return metadata


def normalize_doc_type(doc_type_raw: str) -> tuple[DocumentType, str]:
    """Normalize canonical doc_type values."""
    normalized = re.sub(r"[\s-]+", "_", (doc_type_raw or "").strip().lower())
    if not normalized:
        normalized = "knowledge"

    try:
        return DocumentType(normalized), normalized
    except ValueError:
        logger.debug("Unknown document type '%s'; defaulting to knowledge", doc_type_raw)
        return DocumentType.KNOWLEDGE, "knowledge"


def normalize_priority(priority_raw: Any) -> str:
    """Normalize priority values to the ADR enum."""
    normalized = str(priority_raw or "").strip().lower()
    if normalized in {"low", "normal", "high", "critical"}:
        return normalized
    return "normal"


def find_source_documents_root(source_dir: Path) -> Path:
    """Resolve the canonical source_documents root when ingesting a subtree."""
    resolved_source_dir = source_dir.resolve()
    for candidate in (resolved_source_dir, *resolved_source_dir.parents):
        if candidate.name == "source_documents":
            return candidate
    return resolved_source_dir


def resolve_source_document_identity(md_file: Path, source_dir: Path) -> tuple[str, str]:
    """Return the stable source path and scope prefix for a source document."""
    resolved_file = md_file.resolve()
    resolved_source_dir = source_dir.resolve()
    source_root = find_source_documents_root(source_dir)

    try:
        source_document_path = resolved_file.relative_to(source_root).as_posix()
    except ValueError:
        source_document_path = resolved_file.relative_to(resolved_source_dir).as_posix()

    try:
        scope_prefix = resolved_source_dir.relative_to(source_root).as_posix()
    except ValueError:
        scope_prefix = ""

    if scope_prefix in {".", ""}:
        return source_document_path, ""
    return source_document_path, f"{scope_prefix.rstrip('/')}/"


def determine_retrieval_category(
    source_file: Path,
    source_dir: Path,
    metadata: Dict[str, Any],
) -> str:
    """Derive the indexed category without expanding live-target category boundaries."""
    explicit_category = normalize_metadata_key(str(metadata.get("category") or ""))
    if explicit_category:
        return explicit_category

    source_root = find_source_documents_root(source_dir)
    try:
        relative_parts = source_file.resolve().relative_to(source_root).parts
    except ValueError:
        try:
            relative_parts = source_file.resolve().relative_to(source_dir.resolve()).parts
        except ValueError:
            relative_parts = source_file.parts

    if len(relative_parts) > 1:
        derived = normalize_metadata_key(relative_parts[0])
        if derived:
            return derived
    return DocumentCategory.SHARED.value


def determine_document_category(md_file: Path, metadata: Dict[str, Any]) -> DocumentCategory:
    """Determine document category from explicit metadata or directory structure."""
    explicit_category = metadata.get("category", "").lower()
    if explicit_category in CATEGORY_NAME_MAP:
        return CATEGORY_NAME_MAP[explicit_category]

    for part in md_file.parts:
        if part in CATEGORY_NAME_MAP:
            return CATEGORY_NAME_MAP[part]

    return DocumentCategory.SHARED


def create_scraped_document_from_markdown(
    md_file: Path, source_dir: Optional[Path] = None
) -> ScrapedDocument:
    """Convert a markdown file into a ScrapedDocument."""
    content = md_file.read_text(encoding="utf-8")
    metadata = parse_markdown_metadata(content)
    source_document_path = ""
    source_document_scope = ""
    if source_dir is not None:
        source_document_path, source_document_scope = resolve_source_document_identity(
            md_file, source_dir
        )
        retrieval_category = determine_retrieval_category(md_file, source_dir, metadata)
    else:
        retrieval_category = normalize_metadata_key(str(metadata.get("category") or ""))
        retrieval_category = retrieval_category or DocumentCategory.SHARED.value

    title = metadata.get("title", md_file.stem.replace("-", " ").title())
    category = determine_document_category(md_file, metadata)
    priority = normalize_priority(metadata.get("priority"))
    severity_str = str(metadata.get("severity") or priority).strip().lower()

    severity = SEVERITY_NAME_MAP.get(severity_str.lower(), SeverityLevel.MEDIUM)

    doc_type_raw = str(metadata.get("doc_type", "knowledge"))
    doc_type, normalized_doc_type = normalize_doc_type(doc_type_raw)
    name = str(metadata.get("name") or md_file.stem).strip() or md_file.stem
    summary_raw = metadata.get("summary")
    summary = str(summary_raw).strip() if summary_raw is not None else ""
    pinned = parse_bool(metadata.get("pinned"), default=False)
    explicit_url = str(metadata.get("url") or "").strip()
    passthrough_metadata = {
        key: value for key, value in metadata.items() if key not in RESERVED_METADATA_KEYS
    }

    return ScrapedDocument(
        title=title,
        source_url=explicit_url or f"file://{md_file.absolute()}",
        content=content,
        category=category,
        doc_type=doc_type,
        severity=severity,
        metadata={
            **passthrough_metadata,
            "file_path": str(md_file),
            "file_size": md_file.stat().st_size,
            "original_category": metadata.get("category", "shared").lower(),
            "original_severity": severity_str,
            "original_doc_type": doc_type_raw,
            "determined_category": category.value,
            "doc_type": normalized_doc_type,
            "name": name,
            "summary": summary or None,
            "priority": priority,
            "pinned": pinned,
            "source_document_path": source_document_path,
            "source_document_scope": source_document_scope,
            "retrieval_category": retrieval_category,
        },
    )


def _stable_fragment(value: str, fallback: str) -> str:
    """Normalize an OpenAPI fragment identifier for stable source tracking."""
    normalized = re.sub(r"[^A-Za-z0-9._~-]+", "-", str(value or "")).strip("-")
    return normalized or fallback


def _openapi_markdown(title: str, payload: Dict[str, Any]) -> str:
    """Render a bounded OpenAPI object as searchable Markdown."""
    rendered = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    return f"# {title}\n\n```json\n{rendered}\n```\n"


def _create_openapi_document(
    *,
    spec_path: Path,
    source_dir: Path,
    source_document_base: str,
    source_document_scope: str,
    retrieval_category: str,
    fragment: str,
    title: str,
    content: str,
    name: str,
    summary: str = "",
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> ScrapedDocument:
    """Build one independently tracked document from an OpenAPI fragment."""
    source_document_path = f"{source_document_base}#{fragment}"
    return ScrapedDocument(
        title=title,
        source_url=f"file://{spec_path.absolute()}#{fragment}",
        content=content,
        category=DocumentCategory.SHARED,
        doc_type=DocumentType.API_DOC,
        severity=SeverityLevel.LOW,
        metadata={
            "file_path": str(spec_path),
            "file_size": spec_path.stat().st_size,
            "original_category": retrieval_category,
            "original_severity": SeverityLevel.LOW.value,
            "original_doc_type": DocumentType.API_DOC.value,
            "determined_category": DocumentCategory.SHARED.value,
            "doc_type": DocumentType.API_DOC.value,
            "name": name,
            "summary": summary or None,
            "priority": "normal",
            "pinned": False,
            "source_document_path": source_document_path,
            "source_document_scope": source_document_scope,
            "retrieval_category": retrieval_category,
            **(extra_metadata or {}),
        },
    )


def create_scraped_documents_from_openapi(
    spec_path: Path,
    source_dir: Optional[Path] = None,
) -> List[ScrapedDocument]:
    """Expand an OpenAPI JSON file into overview, operation, and component documents."""
    data = json.loads(spec_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not (data.get("openapi") or data.get("swagger")):
        raise ValueError(f"Not an OpenAPI document: {spec_path}")

    source_root = source_dir or spec_path.parent
    source_document_base, source_document_scope = resolve_source_document_identity(
        spec_path, source_root
    )
    retrieval_category = determine_retrieval_category(spec_path, source_root, {})
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    api_title = str(info.get("title") or spec_path.stem)
    api_version = str(info.get("version") or data.get("openapi") or data.get("swagger") or "")

    overview_payload = {
        "openapi": data.get("openapi") or data.get("swagger"),
        "info": info,
        "servers": data.get("servers") or [],
        "tags": data.get("tags") or [],
        "externalDocs": data.get("externalDocs") or {},
        "security": data.get("security") or [],
    }
    documents = [
        _create_openapi_document(
            spec_path=spec_path,
            source_dir=source_root,
            source_document_base=source_document_base,
            source_document_scope=source_document_scope,
            retrieval_category=retrieval_category,
            fragment="overview",
            title=f"{api_title} Overview",
            content=_openapi_markdown(f"{api_title} Overview", overview_payload),
            name=f"{spec_path.stem}-overview",
            summary=str(info.get("description") or ""),
            extra_metadata={"openapi_version": api_version, "resource_kind": "overview"},
        )
    ]

    paths = data.get("paths") if isinstance(data.get("paths"), dict) else {}
    for api_path, path_item in sorted(paths.items()):
        if not isinstance(path_item, dict):
            continue
        path_parameters = path_item.get("parameters") or []
        for method, operation in sorted(path_item.items()):
            normalized_method = str(method).lower()
            if normalized_method not in OPENAPI_METHODS or not isinstance(operation, dict):
                continue
            method_upper = normalized_method.upper()
            fallback_id = _stable_fragment(f"{method_upper}-{api_path}", "operation")
            operation_id = str(operation.get("operationId") or fallback_id)
            fragment_id = _stable_fragment(operation_id, fallback_id)
            summary = str(operation.get("summary") or operation.get("description") or "")
            title = f"{method_upper} {api_path}"
            if summary:
                title = f"{title} — {summary}"
            operation_payload = {
                "method": method_upper,
                "path": api_path,
                "pathParameters": path_parameters,
                "operation": operation,
            }
            documents.append(
                _create_openapi_document(
                    spec_path=spec_path,
                    source_dir=source_root,
                    source_document_base=source_document_base,
                    source_document_scope=source_document_scope,
                    retrieval_category=retrieval_category,
                    fragment=f"operation/{fragment_id}",
                    title=title,
                    content=_openapi_markdown(title, operation_payload),
                    name=operation_id,
                    summary=summary,
                    extra_metadata={
                        "openapi_version": api_version,
                        "resource_kind": "operation",
                        "operation_id": operation_id,
                        "http_method": method_upper,
                        "api_path": api_path,
                        "tags": operation.get("tags") or [],
                    },
                )
            )

    components = data.get("components") if isinstance(data.get("components"), dict) else {}
    for component_kind, component_entries in sorted(components.items()):
        if not isinstance(component_entries, dict):
            continue
        for component_name, component_payload in sorted(component_entries.items()):
            if not isinstance(component_payload, dict):
                continue
            kind_fragment = _stable_fragment(str(component_kind), "component")
            name_fragment = _stable_fragment(str(component_name), "item")
            if component_kind == "schemas":
                fragment = f"schema/{name_fragment}"
                title = f"{api_title} Schema: {component_name}"
                resource_kind = "schema"
            else:
                fragment = f"component/{kind_fragment}/{name_fragment}"
                title = f"{api_title} {component_kind}: {component_name}"
                resource_kind = "component"
            documents.append(
                _create_openapi_document(
                    spec_path=spec_path,
                    source_dir=source_root,
                    source_document_base=source_document_base,
                    source_document_scope=source_document_scope,
                    retrieval_category=retrieval_category,
                    fragment=fragment,
                    title=title,
                    content=_openapi_markdown(title, component_payload),
                    name=str(component_name),
                    extra_metadata={
                        "openapi_version": api_version,
                        "resource_kind": resource_kind,
                        "component_kind": str(component_kind),
                    },
                )
            )

    return documents


def find_markdown_files(source_dir: Path) -> List[Path]:
    """Return markdown source files, excluding README placeholders."""
    return sorted(
        (
            path
            for path in source_dir.rglob("*.md")
            if path.name.lower() != "readme.md" and not _is_excluded_source_path(path, source_dir)
        ),
        key=lambda path: path.as_posix(),
    )


def find_openapi_files(source_dir: Path) -> List[Path]:
    """Return JSON source files whose top-level object declares OpenAPI/Swagger."""
    files: List[Path] = []
    for path in sorted(source_dir.rglob("*.json"), key=lambda item: item.as_posix()):
        if _is_excluded_source_path(path, source_dir):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("Skipping unreadable JSON source %s: %s", path, exc)
            continue
        if isinstance(data, dict) and (data.get("openapi") or data.get("swagger")):
            files.append(path)
    return files


def find_supported_source_files(source_dir: Path) -> List[Path]:
    """Return all directly supported local corpus source files."""
    return sorted(
        [*find_markdown_files(source_dir), *find_openapi_files(source_dir)],
        key=lambda path: path.as_posix(),
    )
