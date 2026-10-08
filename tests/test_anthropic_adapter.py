import json

import httpx
from anthropic import AsyncAnthropic

from agentic_turn.models import ModelReply, Turn
from agentic_turn.providers import AnthropicProvider


def sdk_client(handler):
    return AsyncAnthropic(
        api_key="synthetic-test-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_sdk_stream_parses_tool_json_and_preserves_replay_blocks():
    usage = {"input_tokens": 12, "output_tokens": 0}
    frames = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_demo",
                "type": "message",
                "role": "assistant",
                "model": "test-model",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": usage,
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Inspecting source."},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "tool_demo",
                "name": "read_source",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"source_id":"mail-001"}'},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 8},
        },
        {"type": "message_stop"},
    ]
    body = "".join(f"event: {f['type']}\ndata: {json.dumps(f)}\n\n" for f in frames)

    def handler(request):
        sent = json.loads(request.content)
        assert sent["stream"] is True
        assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=body)

    provider = AnthropicProvider("unused", "test-model", sdk_client(handler))
    parts = [
        part
        async for part in provider.stream(
            messages=[{"role": "user", "content": "Read the email"}],
            tools=[],
            turn=Turn(conversation_id="demo", prompt="Read"),
            step=0,
            pending=[],
        )
    ]
    assert parts[0] == "Inspecting source."
    reply = parts[-1]
    assert isinstance(reply, ModelReply) and reply.stop_reason == "tool_use"
    assert reply.calls[0].arguments == {"source_id": "mail-001"}
    assert reply.content[1]["input"] == {"source_id": "mail-001"}
    assert reply.usage["input_tokens"] == 12 and reply.usage["output_tokens"] == 8
    await provider.close()


async def test_sdk_finalization_uses_typed_output_and_returns_call_local_usage():
    def handler(request):
        sent = json.loads(request.content)
        assert sent["output_config"]["format"]["type"] == "json_schema"
        result = {
            "outcome": "answered",
            "citations": [{"source_id": "mail-001", "reason": "Email"}],
        }
        return httpx.Response(
            200,
            json={
                "id": "msg_demo",
                "type": "message",
                "role": "assistant",
                "model": "test-model",
                "content": [{"type": "text", "text": json.dumps(result)}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 20, "output_tokens": 10},
            },
        )

    provider = AnthropicProvider("unused", "test-model", sdk_client(handler))
    result = await provider.finalize(turn=Turn(conversation_id="demo", prompt="Read"), repair=False)
    assert result.answer.citations[0].source_id == "mail-001"
    assert result.usage["output_tokens"] == 10
    await provider.close()
