from __future__ import annotations

import json

import httpx
import pytest

from pc_subtitle_translator.protocol import ApiError
from pc_subtitle_translator.settings import Settings
from pc_subtitle_translator.translation import TextTranslator


def records(*events):
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events)


async def run_translation(events, *, status=200, payload=None):
    requests = []
    deltas = []

    def handler(request):
        requests.append(request)
        if payload is not None:
            return httpx.Response(status, json=payload)
        return httpx.Response(status, text=records(*events), headers={"Content-Type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await TextTranslator("sample-api-key", Settings(), client).translate(
            "Hola", [{"source": "Antes", "translation": "Before"}], deltas.append,
        )
    return result, deltas, requests


@pytest.mark.asyncio
async def test_streamed_translation_grows_and_done_replaces_partial_text():
    result, deltas, requests = await run_translation([
        {"type": "response.created", "response": {"id": "ignored"}},
        {"type": "response.output_text.delta", "delta": "Hel"},
        {"type": "response.output_text.delta", "delta": "lo"},
        {"type": "response.output_text.done", "text": "Hello!"},
        {"type": "response.completed", "response": {"status": "completed"}},
    ])
    assert result == "Hello!"
    assert deltas == ["Hel", "Hello"]
    assert len(requests) == 1
    request = requests[0]
    assert request.url == "https://api.openai.com/v1/responses"
    assert request.headers["authorization"] == "Bearer sample-api-key"
    payload = json.loads(request.content)
    assert payload["store"] is False
    assert "sample-api-key" not in request.content.decode()


@pytest.mark.asyncio
async def test_completed_snapshot_recovers_output_when_no_deltas_arrive():
    result, deltas, _ = await run_translation([
        {"type": "response.completed", "response": {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "こんにちは"}]},
        ]}},
    ])
    assert result == "こんにちは"
    assert deltas == []


@pytest.mark.asyncio
async def test_completed_snapshot_does_not_duplicate_existing_deltas():
    result, _, _ = await run_translation([
        {"type": "response.output_text.delta", "delta": "Hello"},
        {"type": "response.completed", "response": {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "Hello"}]},
        ]}},
    ])
    assert result == "Hello"


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    {"type": "response.incomplete"},
    {"type": "response.refusal.done"},
    {"type": "response.completed", "response": {"status": "failed"}},
    {"type": "response.failed", "response": {"error": {"code": "server_error", "message": "temporary failure"}}},
])
async def test_failed_or_incomplete_response_never_looks_like_final_success(event):
    with pytest.raises(ApiError) as caught:
        await run_translation([{"type": "response.output_text.delta", "delta": "part"}, event])
    assert not caught.value.fatal


@pytest.mark.asyncio
async def test_eof_before_completed_is_a_retryable_interruption():
    with pytest.raises(ApiError, match="before completion") as caught:
        await run_translation([{"type": "response.output_text.delta", "delta": "half"}])
    assert not caught.value.fatal


@pytest.mark.asyncio
async def test_http_error_is_redacted_and_configuration_errors_stop():
    with pytest.raises(ApiError) as caught:
        await run_translation([], status=401, payload={"error": {"message": "wrong sample-api-key"}})
    assert caught.value.fatal
    assert "sample-api-key" not in str(caught.value)


@pytest.mark.asyncio
async def test_server_http_failure_is_retryable():
    with pytest.raises(ApiError) as caught:
        await run_translation([], status=503, payload={"error": {"message": "unavailable"}})
    assert not caught.value.fatal


@pytest.mark.asyncio
async def test_excessive_delta_is_rejected():
    with pytest.raises(ApiError, match="size"):
        await run_translation([{"type": "response.output_text.delta", "delta": "x" * 16_001}])
