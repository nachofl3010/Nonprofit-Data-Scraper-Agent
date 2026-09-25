from types import SimpleNamespace

import pytest

from src import extract
from src.cost import CostTracker
from src.parse import Document
from src.schema import Evidenced, Extraction, GeneralContact, LinkPick, LLMOpenRole, LLMPerson, Revenue

U = "https://www.riverbendfoodbank.org/about/"
DOCS = [Document(url=U, kind="html", text="Our team: Ana Ruiz, Executive Director (ana@riverbend.org). "
                                          "Total revenue for fiscal year 2025 was $4.6 million. Call (555) 123-4567.")]


def empty_extraction(**kw) -> Extraction:
    base = dict(name=None, legal_name=None, registration_id=None, country=None, hq_location=None, mission=None,
                programs=[], cause_area=None, geography_served=None, tax_status=None, annual_revenue=None,
                staff_count=None, impact_metrics=[], leadership=[], general_contact=None, open_rfps=[],
                open_roles=[], recent_news=[], funders_and_partners=[], looks_like_nonprofit=True,
                not_nonprofit_reason=None)
    return Extraction(**{**base, **kw})


def person(name, email=None, source=U):
    return LLMPerson(name=name, title="Executive Director", email=email, linkedin=None, source_url=source,
                     confidence="high", evidence=None)


def test_grounding_drops_invented_people_and_unverified_contacts():
    ex = empty_extraction(
        leadership=[person("Ana Ruiz", "ana@riverbend.org"), person("John Invented"), person("Ana Ruiz", "fake@x.org")],
        general_contact=GeneralContact(email="hello@riverbend.org", phone="555-123-4567", contact_page_url=None, source_url=U),
    )
    notes = extract.ground_check(ex, DOCS)
    assert [p.name for p in ex.leadership] == ["Ana Ruiz", "Ana Ruiz"]
    assert ex.leadership[0].email == "ana@riverbend.org" and ex.leadership[1].email is None
    assert ex.general_contact.email is None and ex.general_contact.phone == "555-123-4567"
    assert any("John Invented" in n for n in notes)


def test_grounding_removes_invented_urls():
    docs = [Document(url=U, kind="html", text="Contact us or see our jobs.",
                     raw_html='<a href="/contact-us/">Contact</a> <a href="https://boards.greenhouse.io/rfb/1">Jobs</a>')]
    ex = empty_extraction(
        general_contact=GeneralContact(email=None, phone=None, contact_page_url="https://www.riverbendfoodbank.org/contact/",
                                       source_url=U),
        open_roles=[LLMOpenRole(title="Grants Manager", url="https://boards.greenhouse.io/rfb/1", source_url=U,
                                confidence="high", evidence=None),
                    LLMOpenRole(title="CFO", url="https://www.riverbendfoodbank.org/jobs/cfo", source_url=U,
                                confidence="high", evidence=None)],
    )
    extract.ground_check(ex, docs)
    assert ex.general_contact.contact_page_url is None  # /contact/ never linked (/contact-us/ is)
    assert [r.url for r in ex.open_roles] == ["https://boards.greenhouse.io/rfb/1", None]


def test_grounding_downgrades_unsupported_evidence_and_unknown_sources():
    ex = empty_extraction(
        annual_revenue=Evidenced[Revenue](value=Revenue(amount=9e6, currency="USD", fiscal_year=2025), source_url=U,
                                          confidence="high", evidence="Revenue grew to $9 million in record year"),
        leadership=[person("Ana Ruiz", source="https://elsewhere.org/")],
    )
    extract.ground_check(ex, DOCS)
    assert ex.annual_revenue.confidence == "low"
    assert ex.leadership[0].confidence == "low"


class FakeAnthropic:
    def __init__(self, texts):
        self.texts, self.calls = list(texts), []

    def create(self, **kw):
        self.calls.append(kw)
        usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.texts.pop(0))],
                               usage=usage, stop_reason="end_turn")


class FakeOpenAI:
    def __init__(self, texts, finish_reason="stop"):
        self.texts, self.calls, self.finish_reason = list(texts), [], finish_reason

    def create(self, **kw):
        self.calls.append(kw)
        usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=50,
                                prompt_tokens_details=SimpleNamespace(cached_tokens=400))
        msg = SimpleNamespace(content=self.texts.pop(0), refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=self.finish_reason)], usage=usage)


@pytest.fixture
def anthropic_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_API_KEY", "sk-ant-test")
    for var in ("LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_PRICE_IN", "LLM_PRICE_OUT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(extract, "CACHE_DIR", tmp_path)


def test_call_llm_retries_once_with_validation_error(monkeypatch, anthropic_env):
    fake = FakeAnthropic(['{"urls": "not-a-list"}', '{"urls": ["https://x.org/team"], "reason": "team page"}'])
    monkeypatch.setattr(extract, "_anthropic", lambda: SimpleNamespace(messages=fake))
    tracker = CostTracker()
    pick, errors = extract.call_llm("sys", "user", LinkPick, "test", tracker)
    assert pick.urls == ["https://x.org/team"]
    assert len(fake.calls) == 2 and "failed validation" in fake.calls[1]["messages"][-1]["content"]
    assert len(errors) == 1 and "attempt 1" in errors[0]
    assert tracker.summary().usd > 0


def test_call_llm_cache_hit_costs_nothing(monkeypatch, anthropic_env):
    fake = FakeAnthropic(['{"urls": [], "reason": "none"}'])
    monkeypatch.setattr(extract, "_anthropic", lambda: SimpleNamespace(messages=fake))
    extract.call_llm("sys", "user", LinkPick, "test", CostTracker())
    tracker = CostTracker()
    pick, _ = extract.call_llm("sys", "user", LinkPick, "test", tracker)
    assert pick.urls == [] and len(fake.calls) == 1
    assert tracker.summary().usd == 0 and tracker.summary().usd_list > 0


def test_openai_path_sends_strict_schema_and_counts_cached_tokens(monkeypatch, anthropic_env):
    monkeypatch.setenv("LLM_API_KEY", "sk-proj-test")
    fake = FakeOpenAI(['{"urls": ["https://x.org/about"], "reason": "about"}'])
    monkeypatch.setattr(extract, "_openai", lambda: SimpleNamespace(chat=SimpleNamespace(completions=fake)))
    tracker = CostTracker()
    pick, errors = extract.call_llm("sys", "user", LinkPick, "test", tracker)
    assert pick.urls == ["https://x.org/about"] and errors == []
    sent = fake.calls[0]
    assert sent["model"] == "gpt-4.1-mini" and sent["max_completion_tokens"] == 8000
    assert sent["messages"][0] == {"role": "system", "content": "sys"}
    assert sent["response_format"]["json_schema"]["strict"] is True
    call = tracker.calls[0]
    assert (call.input_tokens, call.cache_read_tokens, call.output_tokens) == (600, 400, 50)


def test_openai_truncation_reported(monkeypatch, anthropic_env):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    fake = FakeOpenAI(['{"urls": [], "reason": "x"}'], finish_reason="length")
    monkeypatch.setattr(extract, "_openai", lambda: SimpleNamespace(chat=SimpleNamespace(completions=fake)))
    _, errors = extract.call_llm("sys", "user", LinkPick, "test", CostTracker())
    assert errors == ["test: stop_reason=max_tokens"]


def test_unknown_provider_fails_soft(monkeypatch, anthropic_env):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    parsed, errors = extract.call_llm("sys", "user", LinkPick, "test", CostTracker())
    assert parsed is None and "unknown LLM_PROVIDER" in errors[0]


@pytest.mark.parametrize("env,provider,model", [
    ({"LLM_API_KEY": "sk-ant-api03-x"}, "anthropic", "claude-haiku-4-5"),
    ({"LLM_API_KEY": "sk-proj-x"}, "openai", "gpt-4.1-mini"),
    ({"LLM_API_KEY": "sk-proj-x", "LLM_PROVIDER": "anthropic"}, "anthropic", "claude-haiku-4-5"),
    ({"LLM_BASE_URL": "http://localhost:11434/v1", "LLM_MODEL": "llama3.1"}, "openai", "llama3.1"),
    ({"OPENAI_API_KEY": "sk-x"}, "openai", "gpt-4.1-mini"),
    ({}, "anthropic", "claude-haiku-4-5"),
])
def test_provider_detection(monkeypatch, env, provider, model):
    for var in ("LLM_API_KEY", "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert (extract.llm_provider(), extract.llm_model()) == (provider, model)
