"""LLM provider abstraction.

The gateway speaks one shape internally — :class:`LLMRequest` /
:class:`LLMResponse` — and routes to a pluggable :class:`LLMBackend`.
"""

from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass
from typing import Protocol


@dataclass
class LLMRequest:
    model: str
    messages: list[dict]
    max_tokens: int = 1024


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    cost_cents: int
    model: str


class LLMBackend(Protocol):
    async def complete(self, req: LLMRequest) -> LLMResponse: ...


class MockBackend:
    """Deterministic backend for tests + the benchmark."""

    def __init__(
        self,
        *,
        latency_ms: int = 0,
        jitter_ms: int = 0,
        cost_per_1k_in: int = 25,
        cost_per_1k_out: int = 125,
    ) -> None:
        self.latency_ms = latency_ms
        self.jitter_ms = jitter_ms
        self.cost_per_1k_in = cost_per_1k_in
        self.cost_per_1k_out = cost_per_1k_out
        self.calls = 0

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.latency_ms or self.jitter_ms:
            sleep_ms = self.latency_ms + (
                random.randint(0, self.jitter_ms) if self.jitter_ms else 0
            )
            await asyncio.sleep(sleep_ms / 1000.0)
        prompt = " ".join(m.get("content", "") for m in req.messages)
        in_tokens = max(1, len(prompt) // 4)
        out_tokens = min(req.max_tokens, in_tokens)
        cost = (
            (in_tokens * self.cost_per_1k_in) // 1000
            + (out_tokens * self.cost_per_1k_out) // 1000
        )
        return LLMResponse(
            text=f"mock-reply-to[{prompt[:30]}]",
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cost_cents=cost,
            model=req.model,
        )


class AnthropicBackend:
    """Wraps :class:`anthropic.AsyncAnthropic`."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        cost_per_1k_in: int = 25,
        cost_per_1k_out: int = 125,
    ) -> None:
        from anthropic import AsyncAnthropic  # noqa: F401

        self.model = model or os.environ.get("LLM_GATEWAY_MODEL", "claude-haiku-4-5-20251001")
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.cost_per_1k_in = cost_per_1k_in
        self.cost_per_1k_out = cost_per_1k_out

    async def complete(self, req: LLMRequest) -> LLMResponse:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=self._api_key)
        resp = await client.messages.create(
            model=req.model or self.model,
            max_tokens=req.max_tokens,
            messages=req.messages,
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        in_tokens = resp.usage.input_tokens
        out_tokens = resp.usage.output_tokens
        cost = (
            (in_tokens * self.cost_per_1k_in) // 1000
            + (out_tokens * self.cost_per_1k_out) // 1000
        )
        return LLMResponse(
            text=text,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cost_cents=cost,
            model=resp.model,
        )
