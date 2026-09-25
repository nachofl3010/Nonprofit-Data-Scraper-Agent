"""Token accounting and cost estimates for every LLM call, plus a persistent spend log."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from src.schema import CallCost, RunCost

# USD per million tokens: (input, cached input, output). List prices checked 2026-09-25 on
# the Anthropic and OpenAI pricing pages. Dated variants (gpt-4.1-mini-2025-04-14) match
# their base name. For anything else set LLM_PRICE_IN / LLM_PRICE_OUT, or cost shows as unknown.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-haiku-4-5": (1.00, 0.10, 5.00),
    "claude-sonnet-5": (2.00, 0.20, 10.00),
    "claude-sonnet-4-6": (3.00, 0.30, 15.00),
    "claude-opus-5": (5.00, 0.50, 25.00),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4.1-nano": (0.10, 0.025, 0.40),
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5-nano": (0.05, 0.005, 0.40),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-6-luna": (0.10, 0.01, 0.50),
}
CACHE_WRITE_MULT = 1.25  # Anthropic prompt-cache writes (5-minute TTL); OpenAI has no write premium
COST_LOG = Path("output/cost_log.jsonl")


def prices(model: str) -> tuple[float, float, float] | None:
    p_in, p_out = os.getenv("LLM_PRICE_IN"), os.getenv("LLM_PRICE_OUT")
    if p_in and p_out:
        return float(p_in), float(p_in), float(p_out)  # cached price unknown: assume full price
    match = max((m for m in PRICES if model == m or model.startswith(m + "-")), key=len, default=None)
    return PRICES[match] if match else None


def usd(model: str, usage: dict) -> float | None:
    p = prices(model)
    if p is None:
        return None
    p_in, p_cached, p_out = p
    cost = (
        usage.get("input_tokens", 0) * p_in
        + usage.get("cache_write_tokens", 0) * p_in * CACHE_WRITE_MULT
        + usage.get("cache_read_tokens", 0) * p_cached
        + usage.get("output_tokens", 0) * p_out
    )
    return round(cost / 1_000_000, 6)


class CostTracker:
    def __init__(self) -> None:
        self.calls: list[CallCost] = []

    def add(self, purpose: str, model: str, usage: dict, cached_locally: bool = False) -> CallCost:
        list_price = usd(model, usage)
        call = CallCost(
            purpose=purpose,
            model=model,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            cache_write_tokens=usage.get("cache_write_tokens", 0),
            cached_locally=cached_locally,
            usd=0.0 if cached_locally else list_price,
            usd_list=list_price,
        )
        self.calls.append(call)
        return call

    def summary(self) -> RunCost:
        return RunCost(
            calls=self.calls,
            input_tokens=sum(c.input_tokens + c.cache_read_tokens + c.cache_write_tokens for c in self.calls),
            output_tokens=sum(c.output_tokens for c in self.calls),
            usd=round(sum(c.usd or 0 for c in self.calls), 6),
            usd_list=round(sum(c.usd_list or 0 for c in self.calls), 6),
        )


def log_run(input_: str, run: RunCost, path: Path = COST_LOG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input": input_,
        "calls": len(run.calls),
        "cached_calls": sum(c.cached_locally for c in run.calls),
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "usd": run.usd,
        "usd_list": run.usd_list,
    }
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


def total_spend(path: Path = COST_LOG) -> dict:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    return {
        "runs": len(rows),
        "llm_calls": sum(r["calls"] - r["cached_calls"] for r in rows),
        "input_tokens": sum(r["input_tokens"] for r in rows if r["usd"] > 0),
        "output_tokens": sum(r["output_tokens"] for r in rows if r["usd"] > 0),
        "usd": round(sum(r["usd"] for r in rows), 4),
    }
