"""M1: real provider request shapes (checked against the official docs), Jev decisions, visible fallbacks,
model self-test. No network: httpx.post is replaced by a recorder."""
import copy

import pytest

from app.agents import diagnoser
from app.providers import gemini as gem_mod
from app.providers.base import ProviderError, ProviderNotConfigured
from app.providers.gemini import GeminiProvider
from app.providers.sarvam import SarvamClient
from app.providers.sarvam_chat import SarvamChatProvider
from app.providers.typesafe import TypeSafeClient
from app.router import NON_MODEL_STEPS, ModelRouter
from app.schemas import Question
from fake_provider import FakeProvider


class Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text or str(data)

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


@pytest.fixture
def http(monkeypatch):
    """Record httpx.post calls and replay queued responses."""
    calls, queue = [], []

    def fake_post(url, **kw):
        calls.append({"url": url, **copy.deepcopy(kw)})  # snapshot: providers may mutate the body afterwards
        return queue.pop(0)

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    return calls, queue


# ------------------------------------------------------------------ config sanity
def test_models_yaml_is_consistent(router):
    cfg = router.cfg
    for role, rc in cfg["roles"].items():
        for step in rc["chain"]:
            assert step in cfg["models"] or step in NON_MODEL_STEPS, f"{role}: unknown step {step}"
    gemini_ids = {m["id"] for m in cfg["models"].values() if m["provider"] == "gemini_api"}
    assert gemini_ids == {"gemma-4-31b-it", "gemma-4-26b-a4b-it", "gemini-3.8-flash"}  # the Gemma 4 ids plus the Gemini Flash used for resource pulling
    assert {m["id"] for m in cfg["models"].values() if m["provider"] == "openrouter" and m.get("kind", "chat") == "chat"} == {
        "google/gemma-4-31b-it", "google/gemma-4-26b-a4b-it", "xingchen-agi/xing4.0-29b-a4b:free", "poolside/laguna-s-2.1:free"}
    assert {m["id"] for m in cfg["models"].values() if m["provider"] == "openrouter" and m.get("kind") == "decision"} == {
        "typesafe/jev-1.13", "inception/mercury-decide:free"}
    assert cfg["roles"]["decisions"]["chain"] == ["jev", "jev_or", "mercury_decide", "gemma_26b"]
    assert not any("e4b" in m["id"] for m in cfg["models"].values() if m["provider"] == "gemini_api")
    # Jev is never in a text chain position that could be called as chat: decide() handles it
    assert cfg["models"]["jev"]["kind"] == "decision"


def test_indian_language_uses_indic_chain(router):
    assert router.role_name("teacher", "kn") == "teacher_indic"
    assert router.role_name("teacher", "en") == "teacher"
    assert router.role_name("critic", "kn") == "critic"  # no _indic variant


# ----------------------------------------------------------------------- Gemini
def test_gemini_requires_key_and_names_it(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ProviderNotConfigured, match="GEMINI_API_KEY"):
        GeminiProvider().generate("gemma-4-31b-it", "hi")


def test_gemini_json_mode_rejected_retries_without_it(monkeypatch, http):
    calls, queue = http
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    queue += [Resp(400, text="responseMimeType is not supported for this model"),
              Resp(200, {"candidates": [{"content": {"parts": [{"text": '{"a":1}'}]}}]})]
    p = GeminiProvider()
    assert p.generate("gemma-4-26b-a4b-it", "hi") == '{"a":1}'
    assert calls[0]["json"]["generationConfig"]["responseMimeType"] == "application/json"
    assert "responseMimeType" not in calls[1]["json"]["generationConfig"]
    assert p.json_mode_supported is False


def test_gemini_error_carries_status_and_body(monkeypatch, http):
    calls, queue = http
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    queue.append(Resp(404, text="models/gemma-4-e4b-it is not found"))
    with pytest.raises(ProviderError, match="404.*gemma-4-e4b-it"):
        GeminiProvider().generate("gemma-4-e4b-it", "hi", json_mode=False)


# ------------------------------------------------------------------ Sarvam chat
def test_sarvam_chat_shape(monkeypatch, http):
    calls, queue = http
    monkeypatch.setenv("SARVAM_API_KEY", "sk")
    queue.append(Resp(200, {"choices": [{"message": {"content": "namaste"}}]}))
    assert SarvamChatProvider().generate("sarvam-105b", "hello") == "namaste"
    c = calls[0]
    assert c["url"] == "https://api.sarvam.ai/v2/chat/completions"
    assert c["headers"] == {"api-subscription-key": "sk"}
    assert c["json"]["model"] == "sarvam-105b" and c["json"]["messages"][0] == {"role": "user", "content": "hello"}


def test_sarvam_chat_refuses_undocumented_image_input(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "sk")
    with pytest.raises(ProviderNotConfigured):
        SarvamChatProvider().generate("gemma4", "x", images=[("image/png", b"1")])


# ----------------------------------------------------------------- Sarvam speech
def test_sarvam_stt_tts_translate_shapes(monkeypatch, http):
    calls, queue = http
    monkeypatch.setenv("SARVAM_API_KEY", "sk")
    s = SarvamClient(tts_speaker="")
    queue.append(Resp(200, {"transcript": "vanakkam", "language_code": "ta-IN"}))
    assert s.stt("saaras:v4", b"wav", "ta", mode="codemix") == "vanakkam"
    assert calls[0]["url"].endswith("/speech-to-text")
    assert calls[0]["data"] == {"model": "saaras:v4", "mode": "codemix"} and "file" in calls[0]["files"]

    import base64
    queue.append(Resp(200, {"audios": [base64.b64encode(b"RIFFxx").decode()]}))
    assert s.tts("bulbul:v4-flash", "Hello", "hi") == b"RIFFxx"
    body = calls[1]["json"]
    assert calls[1]["url"].endswith("/text-to-speech")
    assert body["text"] == "Hello" and body["language_code"] == "hi-IN" and body["model"] == "bulbul:v4-flash"
    assert "inputs" not in body and "speaker" not in body  # speaker only sent when configured

    queue.append(Resp(200, {"translated_text": "नमस्ते"}))
    assert s.translate("mayura:v1", "Hello", "en", "hi") == "नमस्ते"
    assert calls[2]["json"] == {"input": "Hello", "source_language_code": "en-IN", "target_language_code": "hi-IN",
                                "model": "mayura:v1"}


def test_sarvam_http_error_is_visible(monkeypatch, http):
    calls, queue = http
    monkeypatch.setenv("SARVAM_API_KEY", "sk")
    queue.append(Resp(422, text='{"detail":"bad speaker"}'))
    with pytest.raises(ProviderError, match="422.*bad speaker"):
        SarvamClient().tts("bulbul:v3", "x", "en")


# --------------------------------------------------------------------------- Jev
def test_jev_request_and_errors(monkeypatch, http):
    calls, queue = http
    monkeypatch.setenv("TYPESAFE_API_KEY", "tk")
    j = TypeSafeClient()
    queue.append(Resp(200, {"answers": {"q": {"type": "noul", "noul": 0.9}}}))
    assert j.systemone("jev-latest", "state", {"q": {"type": "noul", "instructions": "?"}}) == {"q": {"type": "noul", "noul": 0.9}}
    assert calls[0]["url"] == "https://api.typesafe.ai/v1/systemone"
    assert calls[0]["headers"] == {"Authorization": "Bearer tk"}
    assert set(calls[0]["json"]) == {"state", "model", "questions"} and calls[0]["json"]["model"] == "jev-latest"
    queue.append(Resp(429, text="slow down"))
    with pytest.raises(ProviderError, match="429"):
        j.systemone("jev-latest", "s", {})
    queue.append(Resp(401, text="bad key"))
    with pytest.raises(ProviderNotConfigured):
        j.systemone("jev-latest", "s", {})


def test_decide_logs_why_a_decision_model_was_skipped(store, monkeypatch):
    r = ModelRouter(store=store)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert r.decide("diagnoser", "s", {"q": {"type": "noul", "instructions": "?"}}) is None
    skipped = {e["model_used"]: e["error"] for e in store.events() if e["agent"] == "decisions"}
    assert "TYPESAFE_API_KEY" in skipped["jev"] and "OPENROUTER_API_KEY" in skipped["jev_or"] and "OPENROUTER_API_KEY" in skipped["mercury_decide"]
    n = len(store.events())
    r.decide("diagnoser", "s", {"q": {"type": "noul", "instructions": "?"}})
    assert not any(e["agent"] == "decisions" for e in store.events()[: len(store.events()) - n])  # said once, not on every call


def test_decide_is_silent_noop_with_the_test_provider(router, store):
    assert router.decide("diagnoser", "s", {}) is None
    assert store.events() == []


def _question():
    return Question(id="q1", text="?", options=["a", "b", "c"], answer_key="a", concept="speed", hint="think direction")


def test_diagnoser_uses_jev_label_when_known(store, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test-key-123456")
    r = ModelRouter(store=store)
    monkeypatch.setattr(r.jev, "systemone", lambda *a, **k: {
        "correct": {"type": "noul", "noul": 0.02},
        "misconception": {"type": "choice", "choice": "confuses speed and velocity", "confidence": 0.9, "probabilities": {}},
        "completeness": {"type": "score", "score": 0.4}})
    res = diagnoser.run(r, _question(), "b", ["confuses speed and velocity"])
    assert res.model_used == "jev" and res.value.misconception == "confuses speed and velocity"
    assert res.value.hint == "think direction" and res.value.result == "wrong"


def test_diagnoser_falls_to_gemma_when_jev_says_other(store, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test-key-123456")
    r = ModelRouter(store=store)
    monkeypatch.setattr(r.jev, "systemone", lambda *a, **k: {
        "correct": {"noul": 0.1}, "misconception": {"choice": "other", "confidence": 0.9}, "completeness": {"score": 0.2}})
    monkeypatch.setattr(r.providers["gemini_api"], "generate", lambda *a, **k: (
        '{"question_id":"q1","result":"wrong","misconception":"new label","confidence":0.8,"hint":"h"}'))
    res = diagnoser.run(r, _question(), "b", ["confuses speed and velocity"])
    assert res.model_used == "gemma_26b" and res.value.misconception == "new label"


# ------------------------------------------------------- visible fallbacks / events
def test_fallback_events_record_the_reason(router, store):
    router.kill("gemma_31b")
    router.kill("sarvam_gemma4")
    router.kill("or_gemma_31b")
    router.fake.script("critic", [ProviderError("HTTP 429 quota"), ProviderError("HTTP 429 quota")])
    router.call("critic", "p", {}, lambda raw: raw, last_resort=lambda c: {"ok": 1})
    errors = [e["error"] for e in store.events()]
    assert any("model killed" in x for x in errors)
    assert any("429 quota" in x for x in errors)
    assert any("all models in the chain failed" in x for x in errors)  # last resort is explained too


def test_missing_key_is_logged_not_silent(store, monkeypatch, caplog):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    r = ModelRouter(store=store)
    with caplog.at_level("WARNING", logger="aasaan.router"):
        res = r.call("critic", "p", {}, lambda raw: raw, last_resort=lambda c: {"ok": 1})
    assert res.used_last_resort
    assert "GEMINI_API_KEY is not set" in caplog.text


# ------------------------------------------------------------------- self test
def test_self_test_reports_each_model(store, monkeypatch):
    r = ModelRouter(store=store)

    def fake_generate(model_id, prompt, **kw):
        if model_id == "gemma-4-26b-a4b-it":
            raise ProviderError("gemini HTTP 404: model not found")
        return "OK"

    monkeypatch.setattr(r.providers["gemini_api"], "generate", fake_generate)
    monkeypatch.setattr(r.providers["sarvam"], "generate", lambda *a, **k: "OK")
    monkeypatch.setattr(r.providers["openrouter"], "generate", lambda *a, **k: "OK")
    monkeypatch.setattr(r.providers["ollama"], "generate", lambda *a, **k: (_ for _ in ()).throw(ProviderError("conn refused")))
    monkeypatch.setattr(r.jev, "systemone", lambda *a, **k: {"alive": {"noul": 0.9}})
    monkeypatch.setattr(r.or_decider, "systemone", lambda *a, **k: {"alive": {"noul": 0.9}})
    monkeypatch.setattr(r.sarvam, "tts", lambda *a, **k: b"wav")
    monkeypatch.setattr(r.sarvam, "translate", lambda *a, **k: "namaste")
    rep = r.self_test(force=True)
    assert rep["models"]["gemma_31b"]["status"] == "ok"
    assert rep["models"]["gemma_26b"]["status"] == "failed" and "404" in rep["models"]["gemma_26b"]["error"]
    assert rep["models"]["gemma_local"]["status"] == "failed" and rep["models"]["gemma_local"]["optional"]
    assert rep["models"]["sarvam_stt"]["status"] == "skipped"
    assert rep["failed"] == ["gemma_26b"] and rep["ok"] is False  # optional local model does not count


def test_health_models_endpoint_with_the_test_provider(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    c = TestClient(create_app(db_path=str(tmp_path / "h.db"), provider_override=FakeProvider()))
    rep = c.get("/api/health/models").json()
    assert rep["mode"] == "test" and rep["ok"] is True
    assert rep["models"]["gemma_31b"]["id"] == "gemma-4-31b-it"
    assert c.get("/health").json()["ok"] is True
