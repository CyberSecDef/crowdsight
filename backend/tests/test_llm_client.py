"""Phase 2 Step 1 — the LLM client, mocked at the HTTP layer.

``respx`` intercepts real HTTP rather than replacing the OpenAI client, so
these tests exercise the actual SDK path: the base URL it builds, the payload
it serialises, the response it parses. A stubbed client object would assert
that the code calls the stub, which is a much weaker statement.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from pydantic import BaseModel

from app.utils.llm_client import (
    LLMClient,
    LLMError,
    LLMJSONError,
    SyncLLMClient,
    extract_json_candidate,
    strip_code_fences,
)
from app.utils.retry import RetryPolicy

CHAT_URL = "http://ollama:11434/v1/chat/completions"
MESSAGES = [{"role": "user", "content": "make one"}]


class Profile(BaseModel):
    name: str
    age: int


PROFILE_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    "required": ["name", "age"],
}


def completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "qwen2.5:14b",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        },
    )


def sent(route) -> list[dict]:
    return [json.loads(call.request.content) for call in route.calls]


@pytest.fixture
def client(config):
    return LLMClient(config, retry_policy=RetryPolicy(max_attempts=2, base_delay=0.0))


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


@respx.mock
async def test_uses_the_configured_local_base_url(client):
    """The whole privacy story rests on this address being local."""
    route = respx.post(CHAT_URL).mock(return_value=completion("hi"))
    assert await client.complete(MESSAGES) == "hi"
    assert route.called
    assert str(route.calls[0].request.url) == CHAT_URL


@respx.mock
async def test_payload_carries_model_and_options(client):
    route = respx.post(CHAT_URL).mock(return_value=completion("hi"))
    await client.complete(MESSAGES, temperature=0.7, max_tokens=64)
    payload = sent(route)[0]
    assert payload["model"] == "qwen2.5:14b"
    assert payload["temperature"] == 0.7
    assert payload["max_tokens"] == 64
    assert payload["messages"] == MESSAGES


@respx.mock
async def test_text_completions_do_not_request_json_mode(client):
    route = respx.post(CHAT_URL).mock(return_value=completion("hi"))
    await client.complete(MESSAGES)
    assert "response_format" not in sent(route)[0]


@respx.mock
async def test_empty_choices_raises(client):
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json={"id": "x", "object": "chat.completion",
                                               "created": 0, "model": "m", "choices": []})
    )
    with pytest.raises(LLMError):
        await client.complete(MESSAGES)


# --------------------------------------------------------------------------
# Structured output
# --------------------------------------------------------------------------


@respx.mock
async def test_pydantic_schema_returns_validated_instance(client):
    respx.post(CHAT_URL).mock(return_value=completion('{"name":"Ada","age":36}'))
    result = await client.complete_json(MESSAGES, Profile)
    assert isinstance(result, Profile)
    assert result.age == 36


@respx.mock
async def test_dict_schema_returns_validated_dict(client):
    respx.post(CHAT_URL).mock(return_value=completion('{"name":"Ada","age":36}'))
    result = await client.complete_json(MESSAGES, PROFILE_SCHEMA)
    assert result == {"name": "Ada", "age": 36}


@respx.mock
async def test_json_mode_is_requested(client):
    route = respx.post(CHAT_URL).mock(return_value=completion('{"name":"Ada","age":36}'))
    await client.complete_json(MESSAGES, Profile)
    assert sent(route)[0]["response_format"] == {"type": "json_object"}


@respx.mock
async def test_schema_is_injected_as_a_system_message(client):
    route = respx.post(CHAT_URL).mock(return_value=completion('{"name":"Ada","age":36}'))
    await client.complete_json(MESSAGES, Profile)
    first = sent(route)[0]["messages"][0]
    assert first["role"] == "system"
    assert "JSON Schema" in first["content"]


@respx.mock
async def test_existing_system_prompt_is_preserved(client):
    """Callers set persona and task there; replacing it changes the request."""
    route = respx.post(CHAT_URL).mock(return_value=completion('{"name":"Ada","age":36}'))
    await client.complete_json(
        [{"role": "system", "content": "You are terse."}, *MESSAGES], Profile
    )
    head = sent(route)[0]["messages"][0]["content"]
    assert "You are terse." in head
    assert "JSON Schema" in head


async def test_bad_schema_type_rejected(client):
    with pytest.raises(TypeError):
        await client.complete_json(MESSAGES, "not a schema")


# --------------------------------------------------------------------------
# Salvage: no round trip
# --------------------------------------------------------------------------


@respx.mock
async def test_fenced_json_is_stripped_without_a_retry(client):
    route = respx.post(CHAT_URL).mock(
        return_value=completion('```json\n{"name":"Ada","age":36}\n```')
    )
    result = await client.complete_json(MESSAGES, Profile)
    assert result.name == "Ada"
    assert route.call_count == 1


@respx.mock
async def test_prose_wrapped_json_is_salvaged_without_a_retry(client):
    """Re-prompting costs 30-90s locally; salvage is free."""
    route = respx.post(CHAT_URL).mock(
        return_value=completion('Sure!\n{"name":"Ada","age":36}\nHope that helps.')
    )
    result = await client.complete_json(MESSAGES, Profile)
    assert result.age == 36
    assert route.call_count == 1


# --------------------------------------------------------------------------
# Repair loop
# --------------------------------------------------------------------------


@respx.mock
async def test_repair_loop_recovers_from_malformed_json(client):
    route = respx.post(CHAT_URL).mock(
        side_effect=[completion('{"name":"Ada", "age":}'), completion('{"name":"Ada","age":36}')]
    )
    result = await client.complete_json(MESSAGES, Profile)
    assert result.age == 36
    assert route.call_count == 2


@respx.mock
async def test_repair_prompt_feeds_back_the_parser_error(client):
    route = respx.post(CHAT_URL).mock(
        side_effect=[completion("not json at all"), completion('{"name":"Ada","age":36}')]
    )
    await client.complete_json(MESSAGES, Profile)
    conversation = sent(route)[1]["messages"]
    assert conversation[-2]["role"] == "assistant"
    assert conversation[-2]["content"] == "not json at all"
    assert "JSONDecodeError" in conversation[-1]["content"]


@respx.mock
async def test_repair_loop_recovers_from_schema_violation(client):
    """Valid JSON, wrong shape — must repair, not return junk."""
    route = respx.post(CHAT_URL).mock(
        side_effect=[completion('{"name":"Ada"}'), completion('{"name":"Ada","age":36}')]
    )
    result = await client.complete_json(MESSAGES, Profile)
    assert result.age == 36
    assert route.call_count == 2


@respx.mock
async def test_dict_schema_violation_also_repairs(client):
    route = respx.post(CHAT_URL).mock(
        side_effect=[completion('{"name":"Ada"}'), completion('{"name":"Ada","age":36}')]
    )
    assert await client.complete_json(MESSAGES, PROFILE_SCHEMA) == {"name": "Ada", "age": 36}
    assert route.call_count == 2


@respx.mock
async def test_raises_after_exhausting_repairs(config):
    client = LLMClient(config, max_json_attempts=3,
                       retry_policy=RetryPolicy(max_attempts=1, base_delay=0.0))
    route = respx.post(CHAT_URL).mock(
        side_effect=[completion("nope"), completion("still nope"), completion("nope again")]
    )
    with pytest.raises(LLMJSONError) as excinfo:
        await client.complete_json(MESSAGES, Profile)
    assert route.call_count == 3
    # Raw text is the only thing that makes a failure at agent 217 reproducible.
    assert excinfo.value.attempts == ["nope", "still nope", "nope again"]
    assert len(excinfo.value.errors) == 3


# --------------------------------------------------------------------------
# response_format downgrade
# --------------------------------------------------------------------------


@respx.mock
async def test_server_rejecting_response_format_downgrades_once(config):
    """One 400 must not become a 400 on every later call."""
    client = LLMClient(config, retry_policy=RetryPolicy(max_attempts=1, base_delay=0.0))
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if "response_format" in payload:
            return httpx.Response(400, json={"error": {"message": "unknown parameter response_format"}})
        return completion('{"name":"Ada","age":36}')

    respx.post(CHAT_URL).mock(side_effect=handler)

    assert (await client.complete_json(MESSAGES, Profile)).age == 36
    assert len(calls) == 2 and "response_format" not in calls[1]

    await client.complete_json(MESSAGES, Profile)
    assert "response_format" not in calls[2]


# --------------------------------------------------------------------------
# Retry integration
# --------------------------------------------------------------------------


@respx.mock
async def test_transient_transport_failure_is_retried(config):
    client = LLMClient(config, retry_policy=RetryPolicy(max_attempts=3, base_delay=0.0))
    route = respx.post(CHAT_URL).mock(
        side_effect=[httpx.ConnectError("refused"), completion("recovered")]
    )
    assert await client.complete(MESSAGES) == "recovered"
    assert route.call_count == 2


@respx.mock
async def test_client_error_is_not_retried(config):
    client = LLMClient(config, retry_policy=RetryPolicy(max_attempts=3, base_delay=0.0))
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(404, json={"error": {"message": "model not found"}})
    )
    with pytest.raises(Exception):
        await client.complete(MESSAGES)
    assert route.call_count == 1


def test_sdk_retries_are_disabled(config):
    """Two retry layers would compound into surprising latency."""
    assert LLMClient(config)._client.max_retries == 0


def test_timeouts_are_split(config):
    timeout = LLMClient(config)._client.timeout
    assert timeout.read == config.LLM_TIMEOUT
    assert timeout.connect == config.LLM_CONNECT_TIMEOUT


# --------------------------------------------------------------------------
# Text salvage helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("```json\n{}\n```", "{}"),
        ("```\n{}\n```", "{}"),
        ('  {"a":1} ', '{"a":1}'),
        ("no fence here", "no fence here"),
    ],
)
def test_strip_code_fences(raw, expected):
    assert strip_code_fences(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('x {"a":{"b":[1,2]}} y', '{"a":{"b":[1,2]}}'),
        ('{"a":"}"}', '{"a":"}"}'),
        (r'{"a":"\""}', r'{"a":"\""}'),
        ("see [1,{\"a\":2}] end", '[1,{"a":2}]'),
        ("no json here", None),
        ("", None),
    ],
)
def test_extract_json_candidate(raw, expected):
    assert extract_json_candidate(raw) == expected


# --------------------------------------------------------------------------
# Sync facade
# --------------------------------------------------------------------------


@respx.mock
def test_sync_facade_survives_repeated_calls(config):
    """asyncio.run per call closes the loop the HTTP pool is bound to."""
    respx.post(CHAT_URL).mock(side_effect=[completion("one"), completion("two")])
    client = SyncLLMClient(LLMClient(config))
    try:
        assert client.complete(MESSAGES) == "one"
        assert client.complete(MESSAGES) == "two"
    finally:
        client.close()
