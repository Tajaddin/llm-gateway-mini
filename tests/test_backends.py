"""AnthropicBackend client configuration, checked against a stub SDK module."""

from __future__ import annotations

import sys
import types

import pytest

from llm_gateway import AnthropicBackend, LLMRequest


class _StubStream:
    def __init__(self, model):
        self.model = model

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="hi there")],
            usage=types.SimpleNamespace(input_tokens=12, output_tokens=3),
            model=self.model,
        )


@pytest.fixture
def sdk_clients(monkeypatch):
    clients = []

    class AsyncAnthropic:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.stream_kwargs = None
            self.messages = types.SimpleNamespace(stream=self._stream)
            clients.append(self)

        def _stream(self, **kwargs):
            self.stream_kwargs = kwargs
            return _StubStream(kwargs["model"])

    module = types.ModuleType("anthropic")
    module.AsyncAnthropic = AsyncAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return clients


@pytest.mark.asyncio
async def test_anthropic_backend_streams_with_explicit_timeout_and_retries(sdk_clients):
    req = LLMRequest(model="claude-x", messages=[{"role": "user", "content": "hi"}], max_tokens=50)
    resp = await AnthropicBackend(api_key="test").complete(req)

    client = sdk_clients[0]
    assert client.kwargs["timeout"] == 60.0
    assert client.kwargs["max_retries"] == 2
    assert client.stream_kwargs == {
        "model": "claude-x",
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert (resp.text, resp.input_tokens, resp.output_tokens, resp.model) == (
        "hi there",
        12,
        3,
        "claude-x",
    )


@pytest.mark.asyncio
async def test_anthropic_timeout_and_retries_are_configurable(sdk_clients):
    req = LLMRequest(model="claude-x", messages=[{"role": "user", "content": "hi"}])
    await AnthropicBackend(api_key="test", timeout=5.0, max_retries=0).complete(req)

    assert sdk_clients[0].kwargs["timeout"] == 5.0
    assert sdk_clients[0].kwargs["max_retries"] == 0
