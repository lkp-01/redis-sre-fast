"""Tests for provider-compatible LLM helper behavior."""

from langchain_openai import ChatOpenAI

from redis_sre_agent.core.llm_helpers import bind_structured_output, bind_tools_for_provider


class RecordingLLM:
    def __init__(self, base_url: str) -> None:
        self.openai_api_base = base_url
        self.structured_calls: list[tuple[object, dict[str, object]]] = []
        self.tool_calls: list[tuple[object, dict[str, object]]] = []

    def with_structured_output(self, schema: object, **kwargs: object) -> object:
        self.structured_calls.append((schema, kwargs))
        return {"bound": schema}

    def bind_tools(self, tools: object, **kwargs: object) -> object:
        self.tool_calls.append((tools, kwargs))
        return {"bound": tools}


def test_bind_structured_output_uses_deepseek_compatible_function_calling() -> None:
    llm = RecordingLLM("https://api.deepseek.com")
    schema = object()

    result = bind_structured_output(llm, schema)

    assert result == {"bound": schema}
    assert llm.structured_calls == [
        (
            schema,
            {
                "method": "function_calling",
                "strict": False,
                "extra_body": {"thinking": {"type": "disabled"}},
            },
        )
    ]


def test_bind_structured_output_omits_deepseek_options_for_other_providers() -> None:
    llm = RecordingLLM("https://api.openai.com/v1")
    schema = object()

    bind_structured_output(llm, schema)

    assert llm.structured_calls == [
        (
            schema,
            {
                "method": "function_calling",
                "strict": False,
            },
        )
    ]


def test_bind_tools_disables_thinking_for_deepseek() -> None:
    llm = RecordingLLM("https://api.deepseek.com/v1")
    tools = [object()]

    result = bind_tools_for_provider(llm, tools)

    assert result == {"bound": tools}
    assert llm.tool_calls == [
        (
            tools,
            {"extra_body": {"thinking": {"type": "disabled"}}},
        )
    ]


def test_bind_tools_omits_deepseek_options_for_other_providers() -> None:
    llm = RecordingLLM("https://api.openai.com/v1")
    tools = [object()]

    bind_tools_for_provider(llm, tools, tool_choice="required")

    assert llm.tool_calls == [(tools, {"tool_choice": "required"})]


def test_deepseek_first_and_later_tool_bindings_keep_thinking_disabled() -> None:
    llm = ChatOpenAI(
        model="deepseek-v4-pro",
        api_key="test-key",
        base_url="https://api.deepseek.com",
    )

    tool_bound = bind_tools_for_provider(llm, [])
    required = tool_bound.bind(tool_choice="required")

    assert tool_bound.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert required.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert required.kwargs["tool_choice"] == "required"
