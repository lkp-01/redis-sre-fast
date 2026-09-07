from __future__ import annotations

import pytest

from redis_sre_agent.evaluation import retrieval_eval
from redis_sre_agent.evaluation.retrieval_eval import RetrievalEvaluator, RetrievalTestCase


def test_deduplicate_retrieved_documents_keeps_first_ranked_chunk() -> None:
    retrieved = [
        "Redis persistence (Part 24)",
        "Redis persistence",
        "CONFIG SET (Part 2)",
        "config set (PART 3)",
        "Unrelated document",
    ]

    assert RetrievalEvaluator.deduplicate_retrieved_documents(retrieved) == [
        "Redis persistence (Part 24)",
        "CONFIG SET (Part 2)",
        "Unrelated document",
    ]


@pytest.mark.asyncio
async def test_evaluate_single_query_scores_logical_documents_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_search_knowledge_base(**_kwargs):
        return {
            "results": [
                {"title": "Redis persistence (Part 24)"},
                {"title": "Redis persistence"},
                {"title": "CONFIG SET (Part 2)"},
                {"title": "Unrelated document"},
            ]
        }

    monkeypatch.setattr(retrieval_eval, "search_knowledge_base", fake_search_knowledge_base)
    evaluator = RetrievalEvaluator(k_values=[10])
    test_case = RetrievalTestCase(
        query="Redis persistence configuration",
        relevant_docs=["Redis persistence", "CONFIG GET", "CONFIG SET", "Redis configuration"],
        description="Document-level deduplication regression",
    )

    result = await evaluator.evaluate_single_query(test_case)

    assert result.retrieved_docs == [
        "Redis persistence (Part 24)",
        "CONFIG SET (Part 2)",
        "Unrelated document",
    ]
    assert result.precision_at_k[10] == pytest.approx(2 / 3)
    assert result.recall_at_k[10] == pytest.approx(2 / 4)
