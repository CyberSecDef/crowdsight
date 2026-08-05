"""Async client for the local Ollama instance, via the OpenAI-compatible API.

Two entry points:

* :meth:`LLMClient.complete` — free text.
* :meth:`LLMClient.complete_json` — structured output, and the load-bearing
  one. Nearly every downstream stage expects JSON from the model, and
  14b-class models are markedly less reliable at strict JSON than frontier
  models. A single malformed response part-way through a 300-agent profile
  generation should cost one retry, not the whole run.

The JSON path therefore has three layers of defence, cheapest first:

1. **Ask the server to enforce it.** Ollama's OpenAI-compatible endpoint
   accepts ``response_format={"type": "json_object"}``. When the server
   rejects the parameter the client notices once and stops sending it, rather
   than failing every subsequent call.
2. **Repair locally without a round trip.** Strip markdown fences, then scan
   for the first balanced JSON value. Models wrap output in prose far more
   often than they emit genuinely broken syntax, and re-prompting for that
   wastes 30–90 seconds of local inference.
3. **Re-prompt with the error.** Only when the text truly will not parse or
   fails validation, feed the parser's own message back and try again, up to
   ``max_json_attempts``. Then raise :class:`LLMJSONError` carrying every raw
   response, because a debugging session without the raw text is guesswork.

Async-first. CAMEL/OASIS are async internally, so Phase 6 binds directly, and
Phase 4's fan-out is ``asyncio.gather`` rather than a thread pool. Flask
routes use :class:`SyncLLMClient`, which owns a background event loop —
``asyncio.run`` per call would close the loop the underlying HTTP pool is
bound to.

Concurrency limiting and retry live in :mod:`app.utils.retry`. Requests pass
through the process-wide gate, shared with the embedding service because both
contend for the same GPU, and transient failures are retried with jittered
backoff. The gate is acquired per attempt rather than around the retry loop,
so a coroutine sleeping through backoff does not hold an in-flight slot.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import threading
from typing import Any, AsyncContextManager, Callable, Mapping, Sequence, TypeVar

import httpx
from openai import APIStatusError, AsyncOpenAI
from pydantic import BaseModel

from app.config import Config, get_config
from app.utils.retry import RetryPolicy, get_llm_gate, retry_async

logger = logging.getLogger(__name__)

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMJSONError",
    "SyncLLMClient",
    "extract_json_candidate",
    "strip_code_fences",
]

Message = Mapping[str, Any]
ModelT = TypeVar("ModelT", bound=BaseModel)

# Structured output benefits from near-greedy decoding; creativity here shows
# up as invented fields, not better prose.
DEFAULT_JSON_TEMPERATURE = 0.1

MAX_LOGGED_RESPONSE = 2000


class LLMError(RuntimeError):
    """Base class for every failure this module raises."""


class LLMJSONError(LLMError):
    """The model would not produce valid JSON within the attempt budget.

    Carries every raw response and every parser error, in order. Without them
    a failure at agent 217 of 300 is unreproducible.
    """

    def __init__(self, attempts: Sequence[str], errors: Sequence[str]) -> None:
        self.attempts = list(attempts)
        self.errors = list(errors)
        detail = "\n".join(
            f"  attempt {i}: {err}" for i, err in enumerate(self.errors, start=1)
        )
        super().__init__(
            f"Model did not return valid JSON after {len(self.attempts)} attempt(s):\n"
            f"{detail}"
        )


# --------------------------------------------------------------------------
# Text salvage
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(
    r"^\s*```[ \t]*(?:json|JSON)?[ \t]*\r?\n(?P<body>.*?)\r?\n?[ \t]*```\s*$",
    re.DOTALL,
)

_OPENERS = {"{": "}", "[": "]"}


def strip_code_fences(text: str) -> str:
    """Remove a single surrounding markdown code fence, if present."""
    match = _FENCE_RE.match(text.strip())
    return match.group("body").strip() if match else text.strip()


def extract_json_candidate(text: str) -> str | None:
    """Return the first balanced JSON object or array embedded in ``text``.

    Scans rather than regexes, so nested structures survive, and tracks string
    state so a brace inside a string literal does not end the scan early.
    Returns ``None`` when no opener is found.
    """
    start = next(
        (i for i, char in enumerate(text) if char in _OPENERS),
        None,
    )
    if start is None:
        return None

    closer = _OPENERS[text[start]]
    opener = text[start]
    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _parse_json(text: str) -> Any:
    """Parse ``text`` as JSON, salvaging fenced or prose-wrapped output."""
    cleaned = strip_code_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        candidate = extract_json_candidate(cleaned)
        if candidate is None:
            raise
        return json.loads(candidate)


# --------------------------------------------------------------------------
# Schema handling
# --------------------------------------------------------------------------


def _is_model_class(schema: Any) -> bool:
    return isinstance(schema, type) and issubclass(schema, BaseModel)


def _schema_json(schema: Any) -> dict[str, Any]:
    """The JSON Schema to show the model, from either accepted form."""
    if _is_model_class(schema):
        return schema.model_json_schema()
    if isinstance(schema, Mapping):
        return dict(schema)
    raise TypeError(
        f"schema must be a pydantic model class or a JSON Schema mapping, "
        f"got {type(schema).__name__}"
    )


def _validate(data: Any, schema: Any) -> Any:
    """Validate parsed data, returning a model instance or the plain dict."""
    if _is_model_class(schema):
        return schema.model_validate(data)

    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError as JSONSchemaValidationError

    validator = Draft202012Validator(dict(schema))
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        # Report all of them; fixing one field per round trip is expensive
        # when each round trip is a local generation.
        summary = "; ".join(
            f"{'/'.join(str(p) for p in err.path) or '(root)'}: {err.message}"
            for err in errors[:10]
        )
        raise JSONSchemaValidationError(summary)
    return data


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class LLMClient:
    """Async wrapper over the OpenAI SDK, pointed at local Ollama."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        client: Any | None = None,
        max_json_attempts: int = 3,
        timeout: float | None = None,
        gate: Callable[[], AsyncContextManager[Any]] | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.config = config or get_config()
        self.model = self.config.LLM_MODEL_NAME
        self.max_json_attempts = max(1, max_json_attempts)
        self.retry_policy = retry_policy or self.config.llm_retry_policy()
        # Shared with the embedding service: both contend for the same GPU, so
        # bounding them separately would let their sum exceed the limit that
        # exists to prevent an OOM.
        self._gate = gate or get_llm_gate()
        self._owns_client = client is None
        # Split timeouts. Generation legitimately takes minutes; a connection
        # either establishes at once or is not going to, and a 300s connect
        # timeout would turn "Ollama is down" into a five-minute hang.
        read_timeout = timeout if timeout is not None else self.config.LLM_TIMEOUT
        self._client = client or AsyncOpenAI(
            base_url=self.config.LLM_BASE_URL,
            api_key=self.config.llm_api_key,
            timeout=httpx.Timeout(read_timeout, connect=self.config.LLM_CONNECT_TIMEOUT),
            max_retries=0,  # retry.py owns retry policy; two layers would compound.
        )
        # Set to False permanently the first time the server rejects the
        # parameter, so one 400 does not become a 400 on every later call.
        self._supports_json_mode = True

    # -- text ---------------------------------------------------------------

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> str:
        """Return the assistant's message content as text."""
        return await self._chat(
            messages, temperature=temperature, max_tokens=max_tokens, **kwargs
        )

    # -- structured ---------------------------------------------------------

    async def complete_json(
        self,
        messages: Sequence[Message],
        schema: Any,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> Any:
        """Return validated structured output.

        ``schema`` is either a pydantic model class — in which case a validated
        instance is returned — or a JSON Schema mapping, which returns the
        validated dict.
        """
        schema_json = _schema_json(schema)
        if temperature is None:
            temperature = DEFAULT_JSON_TEMPERATURE

        conversation = [*self._with_schema_instruction(messages, schema_json)]
        raw_responses: list[str] = []
        errors: list[str] = []

        for attempt in range(1, self.max_json_attempts + 1):
            text = await self._chat(
                conversation,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=True,
                **kwargs,
            )
            raw_responses.append(text)

            try:
                return _validate(_parse_json(text), schema)
            except Exception as exc:  # parse or validation failure
                message = f"{exc.__class__.__name__}: {exc}"
                errors.append(message)
                logger.warning(
                    "complete_json attempt %d/%d failed (%s). Raw response: %s",
                    attempt,
                    self.max_json_attempts,
                    message,
                    text[:MAX_LOGGED_RESPONSE],
                )
                if attempt == self.max_json_attempts:
                    break
                conversation = [
                    *conversation,
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": self._repair_prompt(message, schema_json)},
                ]

        raise LLMJSONError(raw_responses, errors)

    @staticmethod
    def _with_schema_instruction(
        messages: Sequence[Message], schema_json: Mapping[str, Any]
    ) -> list[Message]:
        """Prepend the schema as a system message.

        Appended to any existing system message rather than replacing it —
        callers set persona and task there, and losing that would change what
        the model is being asked to do.
        """
        instruction = (
            "Respond with a single JSON value and nothing else. No prose, no "
            "explanation, no markdown code fences. It must validate against "
            "this JSON Schema:\n"
            f"{json.dumps(schema_json, indent=2, sort_keys=True)}"
        )
        messages = list(messages)
        if messages and messages[0].get("role") == "system":
            head = dict(messages[0])
            head["content"] = f"{head.get('content', '')}\n\n{instruction}".strip()
            return [head, *messages[1:]]
        return [{"role": "system", "content": instruction}, *messages]

    @staticmethod
    def _repair_prompt(error: str, schema_json: Mapping[str, Any]) -> str:
        return (
            "That response could not be used. The error was:\n"
            f"{error}\n\n"
            "Return the corrected value as a single raw JSON document with no "
            "surrounding text or code fences. It must validate against:\n"
            f"{json.dumps(schema_json, indent=2, sort_keys=True)}"
        )

    # -- transport ----------------------------------------------------------

    async def _chat(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
        **kwargs: Any,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(m) for m in messages],
            **kwargs,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if json_mode and self._supports_json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            response = await self._send(payload)
        except APIStatusError as exc:
            if json_mode and self._supports_json_mode and self._is_format_rejection(exc):
                logger.warning(
                    "Server rejected response_format; disabling JSON mode for "
                    "this client and relying on prompt plus repair loop. (%s)",
                    exc,
                )
                self._supports_json_mode = False
                payload.pop("response_format", None)
                response = await self._send(payload)
            else:
                raise

        return self._content_of(response)

    async def _send(self, payload: dict[str, Any]) -> Any:
        """One request, retried on transient failure.

        The gate is acquired *inside* the retry loop, not around it, so a
        coroutine sleeping through backoff does not hold one of the four
        in-flight slots hostage while doing nothing.
        """

        async def attempt() -> Any:
            async with self._gate():
                return await self._client.chat.completions.create(**payload)

        return await retry_async(
            attempt,
            policy=self.retry_policy,
            description=f"Ollama chat completion ({self.model})",
        )

    @staticmethod
    def _is_format_rejection(exc: APIStatusError) -> bool:
        """Distinguish 'I don't support that parameter' from a real error."""
        return getattr(exc, "status_code", None) in (400, 404, 422) and (
            "response_format" in str(exc) or "format" in str(exc).lower()
        )

    @staticmethod
    def _content_of(response: Any) -> str:
        choices = getattr(response, "choices", None)
        if not choices:
            raise LLMError(f"Model returned no choices: {response!r}")
        content = choices[0].message.content
        if content is None:
            raise LLMError("Model returned a message with no content")
        return content

    # -- lifecycle ----------------------------------------------------------

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.close()


# --------------------------------------------------------------------------
# Sync facade
# --------------------------------------------------------------------------


class _LoopThread:
    """A private event loop on a background thread.

    ``asyncio.run`` per call would create and close a loop each time, while the
    underlying HTTP connection pool stays bound to the first one — the second
    call then fails with "Event loop is closed". A single long-lived loop keeps
    the pool valid and connections warm.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="crowdsight-llm-loop", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro: Any, timeout: float | None = None) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


class SyncLLMClient:
    """Blocking facade over :class:`LLMClient`, for Flask request handlers."""

    def __init__(self, client: LLMClient | None = None, **kwargs: Any) -> None:
        self._client = client or LLMClient(**kwargs)
        self._loop = _LoopThread()

    def complete(self, messages: Sequence[Message], **kwargs: Any) -> str:
        return self._loop.submit(self._client.complete(messages, **kwargs))

    def complete_json(self, messages: Sequence[Message], schema: Any, **kwargs: Any) -> Any:
        return self._loop.submit(self._client.complete_json(messages, schema, **kwargs))

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._loop.submit(self._client.aclose(), timeout=10)
        self._loop.close()
