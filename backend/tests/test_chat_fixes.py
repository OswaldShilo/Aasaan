"""Chat bugs: complex symbols, conversation memory, outside-the-notebook lookup with numbered citations."""
import shutil
import subprocess
from pathlib import Path

import pytest

from app.agents import doubt
from app.agents.common import overlap, tokens
from app.schemas import DoubtOut

ROOT = Path(__file__).resolve().parents[2]


def test_matching_works_in_any_script_and_keeps_math_symbols():
    ta = "இந்த கேள்விக்கு பதில்"
    assert tokens(ta) and overlap(tokens(ta), ta + " more") >= 3                       # the old \w{3,} found no Tamil word at all
    assert {"∑", "∫"} <= tokens("what is ∑ and ∫ x² dx?") and "α" in tokens("α and β are angles", 1)
    assert "the" not in tokens("the and for") and tokens("O(n log n) <= O(n^2)")


def _chat(engine, worker, monkeypatch, covered=True):
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    seen = {}

    def fake_run(router, q, chunks, prof, notes="", style="", history=None, external=False, guide=False):
        seen.setdefault("calls", []).append({"history": history, "external": external, "ids": [c["chunk_id"] for c in chunks]})
        from types import SimpleNamespace
        return SimpleNamespace(value=DoubtOut(answer="It is " + ("online [1]." if external else "in the notes [1]."), source_refs=[chunks[0]["chunk_id"]] if chunks else [],
                                              covered=covered or external))
    monkeypatch.setattr(doubt, "run", fake_run)
    return nid, seen


def test_the_last_turns_reach_the_model_but_not_the_question_itself(engine, worker, store, monkeypatch):
    nid, seen = _chat(engine, worker, monkeypatch)
    store.add_message(nid, "user", "What is mid?", kind="chat")
    store.add_message(nid, "assistant", "mid is the middle index.", kind="chat")
    store.add_message(nid, "user", "explain that again", kind="chat")                  # the stored copy of the current question
    engine.chat_reply(nid, "L1", "explain that again")
    h = seen["calls"][0]["history"]
    assert [t["text"] for t in h] == ["What is mid?", "mid is the middle index."] and [t["role"] for t in h] == ["user", "assistant"]


def test_an_unanswerable_question_is_looked_up_and_cited_with_a_link(engine, worker, monkeypatch):
    from app import web
    nid, seen = _chat(engine, worker, monkeypatch, covered=False)
    cand = web.Candidate("w0", "link", "https://example.org/zebra", "Zebra", "example.org")
    monkeypatch.setattr(web, "discover_wikipedia", lambda q, n=3: [cand])
    monkeypatch.setattr(web, "fetch_page", lambda url, store=None: web.PageResult(url=url, ok=True, title="Zebra", text="Zebras have stripes. " * 20))
    out = engine.chat_reply(nid, "L1", "why do zebras have stripes?")
    assert out["answer"].startswith("That isn't in your notebook, so I looked it up online:") and "[1]" in out["answer"]
    assert seen["calls"][1]["external"] is True
    assert out["refs"][0]["url"] == "https://example.org/zebra" and out["refs"][0]["n"] == 1      # [1] in the text maps to this link


def test_when_the_lookup_fails_the_answer_says_so(engine, worker, monkeypatch):
    from app import web
    nid, _ = _chat(engine, worker, monkeypatch, covered=False)
    monkeypatch.setattr(web, "discover_wikipedia", lambda q, n=3: [])
    out = engine.chat_reply(nid, "L1", "why do zebras have stripes?")
    assert "couldn't look it up online" in out["answer"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_math_typesetting_is_safe_and_correct(tmp_path):
    src = (ROOT / "frontend" / "math.js").read_text(encoding="utf-8")
    (tmp_path / "math.mjs").write_text(src, encoding="utf-8")
    (tmp_path / "t.mjs").write_text('import { typeset } from "./math.mjs";\n'
                                    'console.log(typeset(String.raw`$\\sum_{i=1}^{n} i^2 \\leq \\frac{a}{b}$ and O(n^2) &lt;script&gt; <code>x^2</code>`));', encoding="utf-8")
    out = subprocess.run(["node", str(tmp_path / "t.mjs")], capture_output=True, text=True, encoding="utf-8", timeout=30).stdout
    assert "∑<sub>i=1</sub><sup>n</sup>" in out and "≤" in out and "(a)/(b)" in out and "O(n<sup>2</sup>)" in out
    assert "&lt;script&gt;" in out and "<code>x^2</code>" in out                          # escaped text stays escaped; code is untouched
