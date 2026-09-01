"""Coverage for local Markdown and OpenAPI knowledge-source ingestion."""

from __future__ import annotations

import json
from pathlib import Path

from redis_sre_agent.core.redis import SRE_KNOWLEDGE_SCHEMA
from redis_sre_agent.pipelines.ingestion.document_processor import DocumentProcessor
from redis_sre_agent.pipelines.ingestion.processor_source_helpers import (
    create_scraped_document_from_markdown,
    create_scraped_documents_from_openapi,
    find_markdown_files,
    find_openapi_files,
)
from redis_sre_agent.pipelines.scraper.base import DocumentCategory, DocumentType


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_markdown_discovery_excludes_readmes_and_release_examples(tmp_path: Path) -> None:
    source_root = tmp_path / "source_documents"
    included = _write(source_root / "shared" / "runbook.md", "# Runbook\n\nUseful content.")
    _write(source_root / "README.md", "# Placeholder")
    _write(
        source_root / "shared" / "release-v123-example" / "fixture.md",
        "# Release fixture",
    )

    assert find_markdown_files(source_root) == [included]


def test_source_directory_becomes_retrieval_category_without_expanding_enum(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source_documents"
    source = _write(
        source_root / "product_a" / "article.md",
        "# Product Article\n\n" + ("Actionable guidance. " * 12),
    )

    document = create_scraped_document_from_markdown(source, source_root)
    chunks = DocumentProcessor().chunk_document(document)

    assert document.category is DocumentCategory.SHARED
    assert document.metadata["retrieval_category"] == "product_a"
    assert chunks
    assert {chunk["category"] for chunk in chunks} == {"product_a"}
    assert {chunk["source_document_path"] for chunk in chunks} == {"product_a/article.md"}


def test_knowledge_schema_indexes_stable_source_identity() -> None:
    field_names = {field["name"] for field in SRE_KNOWLEDGE_SCHEMA["fields"]}

    assert {"source_document_path", "source_document_scope"} <= field_names


def test_long_document_is_not_truncated_after_ten_chunks(tmp_path: Path) -> None:
    source_root = tmp_path / "source_documents"
    source = _write(
        source_root / "shared" / "long.md",
        "# Long Document\n\n" + ("diagnostic guidance " * 700) + "FINAL_MARKER",
    )
    document = create_scraped_document_from_markdown(source, source_root)

    chunks = DocumentProcessor().chunk_document(document)

    assert len(chunks) > 10
    assert len(chunks) <= 100
    assert chunks[-1]["content"].endswith("FINAL_MARKER")


def test_openapi_discovery_and_expansion_are_bounded_and_stable(tmp_path: Path) -> None:
    source_root = tmp_path / "source_documents"
    spec_path = source_root / "api" / "service.json"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(
        json.dumps(
            {
                "openapi": "3.0.1",
                "info": {"title": "Example Service", "version": "1.2.3"},
                "servers": [{"url": "https://api.example.test"}],
                "paths": {
                    "/widgets": {
                        "get": {
                            "operationId": "listWidgets",
                            "summary": "List widgets",
                            "tags": ["widgets"],
                            "responses": {"200": {"description": "Success"}},
                        }
                    }
                },
                "components": {
                    "schemas": {
                        "Widget": {
                            "type": "object",
                            "properties": {"id": {"type": "string"}},
                        }
                    },
                    "securitySchemes": {"token": {"type": "http", "scheme": "bearer"}},
                },
            }
        ),
        encoding="utf-8",
    )
    _write(source_root / "api" / "ordinary.json", '{"kind": "not-an-api-spec"}')

    assert find_openapi_files(source_root) == [spec_path]

    documents = create_scraped_documents_from_openapi(spec_path, source_root)
    documents_by_path = {
        document.metadata["source_document_path"]: document for document in documents
    }

    assert set(documents_by_path) == {
        "api/service.json#overview",
        "api/service.json#operation/listWidgets",
        "api/service.json#schema/Widget",
        "api/service.json#component/securitySchemes/token",
    }
    assert all(document.doc_type is DocumentType.API_DOC for document in documents)
    assert all(document.category is DocumentCategory.SHARED for document in documents)
    assert all(document.metadata["retrieval_category"] == "api" for document in documents)
    assert "GET /widgets" in documents_by_path["api/service.json#operation/listWidgets"].content
    assert '"properties"' in documents_by_path["api/service.json#schema/Widget"].content
