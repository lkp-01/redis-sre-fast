"""Tests for provider-compatible LLM helper behavior."""

from redis_sre_agent.core.llm_helpers import bind_structured_output


class RecordingLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[object, dict[str, object]]] = []

    def with_structured_output(self, schema: object, **kwargs: object) -> object:
        self.calls.append((schema, kwargs))
        return {"bound": schema}


def test_bind_structured_output_uses_deepseek_compatible_function_calling() -> None:
    llm = RecordingLLM()
    schema = object()

    result = bind_structured_output(llm, schema)

    assert result == {"bound": schema}
    assert llm.calls == [
        (
            schema,
            {
                "method": "function_calling",
                "strict": False,
                "extra_body": {"thinking": {"type": "disabled"}},
            },
        )
    ]
