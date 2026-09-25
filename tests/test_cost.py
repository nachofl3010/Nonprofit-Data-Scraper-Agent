from src.cost import CostTracker, prices, usd


def test_prices_match_dated_variants_and_longest_name(monkeypatch):
    monkeypatch.delenv("LLM_PRICE_IN", raising=False)
    monkeypatch.delenv("LLM_PRICE_OUT", raising=False)
    assert prices("gpt-4.1-mini-2025-04-14") == prices("gpt-4.1-mini") == (0.40, 0.10, 1.60)
    assert prices("gpt-4.1-nano") == (0.10, 0.025, 0.40)
    assert prices("llama3.1") is None


def test_usd_formula(monkeypatch):
    monkeypatch.delenv("LLM_PRICE_IN", raising=False)
    monkeypatch.delenv("LLM_PRICE_OUT", raising=False)
    # 15k input + 3k output on Haiku 4.5: 15000*1 + 3000*5 = $0.03
    assert usd("claude-haiku-4-5", {"input_tokens": 15000, "output_tokens": 3000}) == 0.03
    # cached input is billed at the cached rate
    assert usd("gpt-4.1-mini", {"input_tokens": 0, "cache_read_tokens": 1_000_000, "output_tokens": 0}) == 0.10


def test_env_price_override_for_unlisted_models(monkeypatch):
    monkeypatch.setenv("LLM_PRICE_IN", "0.5")
    monkeypatch.setenv("LLM_PRICE_OUT", "1.5")
    assert usd("llama3.1", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}) == 2.0


def test_unknown_price_is_none_not_zero(monkeypatch):
    monkeypatch.delenv("LLM_PRICE_IN", raising=False)
    monkeypatch.delenv("LLM_PRICE_OUT", raising=False)
    t = CostTracker()
    call = t.add("extract", "some-local-model", {"input_tokens": 10, "output_tokens": 5})
    assert call.usd is None and call.usd_list is None
