"""Intake OCR ladder: Indra tiers, the page-health gate, vision escalation, `Local OCR only`. A fake runner stands in for Indra, so these run anywhere.
The real engine is exercised by test_intake_ocr_integration.py (Docker)."""
import subprocess
import sys
from pathlib import Path

import pytest

from app.intake import IntakeInput, build_source_pack
from app.intake.ocr import LOCAL_ONLY_REASON, IndraRunner, LadderExtractor, OcrError, indra_available, local_only_default

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval" / "indra"))
from make_fixtures import make_pdf, text_pdf_pages  # noqa: E402
from test_intake_pack import INDRA_PAGE2, scan_pdf  # noqa: E402

GOOD = "A binary search halves the range at each step, so it needs only a logarithmic number of comparisons."
PLAIN2 = "\n".join(["1. Arrays and memory", "An array stores items in contiguous memory, so the item at index i is found in constant time.", "Cost of common operations",
                    "Operation Array Linked list", "Access by index O(1) O(n)", "Insert at front O(n) O(1)"])   # what Indra's plain text looks like beside its HTML table
GARBAGE = "Wl SDuLL ul wD @uasSDe. for DDMLO while @)JOTL, GUSSGt."


def page(n, text, source="ocr", conf=0.98, markdown=None, note=""):
    return {"page": n, "text": text, "markdown": markdown, "source": source, "note": note, "rotation": 0, "conf_mean": conf, "blocks": []}


class FakeRunner:
    def __init__(self, pages=None, error=None):
        self.pages, self.error, self.calls = pages or [], error, []

    def run(self, data, suffix, mode):
        self.calls.append((suffix, mode))
        if self.error:
            raise OcrError(self.error)
        return {"version": "0.3.2", "seconds": 0.1, "pages": self.pages}


class Vision:
    def __init__(self, text=GOOD, empty=False, fail=False):
        self.text, self.empty, self.fail, self.n = text, empty, fail, 0

    def __call__(self, png, lang, label):
        self.n += 1
        if self.fail:
            raise RuntimeError("down")
        return {"text": "" if self.empty else self.text, "empty": self.empty}


def ladder(runner, vision=None, local_only=False, **kw):
    return LadderExtractor(vision, local_only=local_only, runner=runner, use_indra=True, **kw)


def pack_of(extractor, data=None, kind="pdf", language="en"):
    n = len(getattr(getattr(extractor, "runner", None), "pages", None) or [1])
    data = data if data is not None else scan_pdf(n)                                         # a real file, so pages can be rendered for the vision tier
    return build_source_pack([IntakeInput(kind, "doc." + ("pdf" if kind == "pdf" else "png"), data=data)], language=language, extractor=extractor)


# ------------------------------------------------------------------------------------------------ the ladder
def test_digital_pages_are_tier_a_and_scans_tier_b_with_structure(tmp_path):
    intro = "## Introduction\n\nBinary search needs sorted data to work correctly."
    runner = FakeRunner([page(1, intro, "text", 1.0, markdown=intro), page(2, PLAIN2, "ocr", 0.98, markdown=INDRA_PAGE2)])
    p = pack_of(ladder(runner))
    assert [x.tier for x in p.pages] == ["indra-auto", "indra-ocr"] and runner.calls == [(".pdf", "auto")]
    assert p.sources[0].status == "readable" and p.tables and p.tables[0].rows[0][0] == "Access by index"
    assert [h.text for h in p.outline_hints][:2] == ["Introduction", "1. Arrays and memory"]


def test_a_confident_scan_never_touches_the_vision_model():
    v = Vision()
    p = pack_of(ladder(FakeRunner([page(1, GOOD)]), v))
    assert p.sources[0].status == "readable" and v.n == 0 and p.pages[0].escalated_because == ""


def test_a_low_confidence_page_is_escalated_with_the_reason_recorded():
    v = Vision()
    p = pack_of(ladder(FakeRunner([page(1, GARBAGE, conf=0.549)]), v))
    assert v.n == 1 and p.pages[0].tier == "vision" and "not sure" in p.pages[0].escalated_because and p.sources[0].status == "readable"
    assert "binary search" in p.chunks[0].text.lower()


def test_wrong_characters_are_never_shipped_when_the_vision_model_is_dead_or_absent():
    for vision, expect in ((None, "No stronger reader"), (Vision(fail=True), "could not make it out")):
        p = pack_of(ladder(FakeRunner([page(1, GARBAGE, conf=0.549)]), vision))
        s = p.sources[0]
        assert s.status == "unreadable" and not p.chunks and expect in s.quality_report[0].reason
        assert "@uasSDe" not in p.markdown["s1"]


def test_the_learners_script_decides_when_ocr_cannot_read_it():
    v = Vision()
    p = pack_of(ladder(FakeRunner([page(1, GARBAGE, conf=0.95)]), v), language="ta")           # a Tamil learner, Latin-looking output
    assert v.n == 1 and "Tamil" in p.pages[0].escalated_because and p.pages[0].tier == "vision"


def test_local_ocr_only_keeps_every_page_on_the_server():
    v = Vision()
    p = pack_of(ladder(FakeRunner([page(1, GARBAGE, conf=0.4), page(2, GOOD)]), v, local_only=True))
    assert v.n == 0                                                                           # nothing was sent anywhere
    assert [q.reason for q in p.sources[0].quality_report][0] == LOCAL_ONLY_REASON and p.sources[0].status == "partly_readable"
    assert [x.tier for x in p.pages] == ["none", "indra-ocr"] and len(p.chunks) >= 1


def test_local_only_also_covers_the_no_indra_fallback():
    ex = LadderExtractor(Vision(), local_only=True, use_indra=False)
    assert ex.basic.vision is None                                                            # the basic tiers get no vision callable either
    p = build_source_pack([IntakeInput("pdf", "scan.pdf", data=scan_pdf(1))], extractor=ex)
    assert p.sources[0].status == "unreadable"


def test_empty_pages_are_told_apart_scan_drawing_blank():
    v = Vision(empty=True)
    runner = FakeRunner([page(1, ""), page(2, "")])
    p = pack_of(ladder(runner, v), data=scan_pdf(2))                                          # both pages are image-only scans
    assert v.n == 2 and all("empty" in q.reason.lower() or "scan" in q.reason.lower() for q in p.sources[0].quality_report)
    blank = pack_of(ladder(FakeRunner([page(1, "")]), v), data=make_pdf([[("t", 72, 700, "F1", 1, " ")]]))
    assert blank.sources[0].status == "unreadable" and "blank" in blank.sources[0].quality_report[0].reason.lower()


def test_an_engine_failure_falls_back_and_says_so():
    p = pack_of(ladder(FakeRunner(error="the reader took longer than 300 seconds")), data=make_pdf(text_pdf_pages()))
    assert p.sources[0].status == "partly_readable" and any("offline reader failed" in q.reason for q in p.sources[0].quality_report)
    assert p.chunks and {x.tier for x in p.pages} == {"text-layer"}                           # the PDF text layer still produced a pack


def test_the_page_limit_is_reported_for_big_files():
    pages = [page(i, f"{GOOD} Section {i}.", "text", 1.0) for i in range(1, 4)]
    data = make_pdf(text_pdf_pages())                                                         # 3 pages in the file
    p = pack_of(ladder(FakeRunner(pages[:2]), max_pages=2), data=data)
    assert p.sources[0].pages == 3 and any(q.pages == [3] and "not read" in q.reason for q in p.sources[0].quality_report)


def test_photos_use_indra_ocr_then_the_gate():
    runner = FakeRunner([page(1, GOOD, conf=0.97)])
    p = pack_of(ladder(runner, Vision()), data=b"\x89PNG....", kind="image")
    assert runner.calls == [(".png", "ocr")] and p.sources[0].kind == "image" and p.figures and p.pages[0].tier == "indra-ocr"
    bad = pack_of(ladder(FakeRunner([page(1, GARBAGE, conf=0.5)]), None), data=b"\xff\xd8\xff", kind="image")
    assert bad.sources[0].status == "unreadable" and not bad.chunks


# ------------------------------------------------------------------------------------------------ the process boundary
def test_the_runner_reports_timeouts_crashes_and_bad_output_in_plain_words(monkeypatch):
    def boom(kind):
        def run(cmd, **kw):
            if kind == "timeout":
                raise subprocess.TimeoutExpired(cmd, 1)
            if kind == "crash":
                return subprocess.CompletedProcess(cmd, 1, b"", b"Traceback\nModelError: models missing")
            return subprocess.CompletedProcess(cmd, 0, b"not json", b"")
        return run
    for kind, expect in (("timeout", "took longer"), ("crash", "models missing"), ("junk", "unreadable")):
        monkeypatch.setattr(subprocess, "run", boom(kind))
        with pytest.raises(OcrError, match=expect):
            IndraRunner(timeout_s=1).run(b"x", ".pdf", "auto")


def test_the_runner_runs_the_worker_as_a_child_process_with_a_page_cap(monkeypatch):
    seen = {}

    def run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return subprocess.CompletedProcess(cmd, 0, b'{"pages": [], "seconds": 0, "version": "x"}', b"")
    monkeypatch.setattr(subprocess, "run", run)
    IndraRunner(cores=2, timeout_s=9, max_pages=50).run(b"x", ".pdf", "ocr")
    assert seen["cmd"][1:3] == ["-m", "app.intake.indra_worker"] and seen["cmd"][3] == "ocr" and seen["cmd"][5:] == ["2", "50"]
    assert seen["kw"]["timeout"] == 9


def test_indra_is_optional_and_switchable(monkeypatch):
    monkeypatch.setenv("AASAAN_INDRA", "off")
    assert indra_available() is False
    monkeypatch.setenv("AASAAN_INDRA", "on")
    monkeypatch.delenv("INDRA_OCR_MODEL_DIR", raising=False)
    assert indra_available() is False                                                         # no models on disk: never a first-run download
    monkeypatch.setenv("AASAAN_LOCAL_OCR_ONLY", "yes")
    assert local_only_default() is True
