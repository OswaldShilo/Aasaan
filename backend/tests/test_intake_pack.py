"""Layer 1 Intake: the source pack. Plain code and generated fixtures: no model, no network, no OCR engine needed."""
import ast
import io
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.db import Store
from app.intake import BasicExtractor, IntakeInput, RawPage, RawSource, SourcePack, build_source_pack
from app.intake.detect import dominant_script, language_of, ocr_supports, page_health, script_profile
from app.intake.structure import clean_item, dedupe_pages, normalize_text, parse_html_tables, parse_page, strip_furniture
from app.orchestrator import DecisionEmitter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval" / "indra"))
from make_fixtures import make_pdf, text_pdf_pages  # noqa: E402

TAMIL = "பைத்தானில் லூப் குறியீட்டை பல முறை"
INDRA_PAGE2 = """## 1. Arrays and memory

An array stores items in contiguous memory, so the item at index i is found in constant time.

## Cost of common operations

<table>
<tr><th>Operation</th><th>Array</th><th>Linked list</th></tr>
<tr><td>Access by index</td><td>O(1)</td><td>O(n)</td></tr>
<tr><td>Insert at front</td><td>O(n)</td><td>O(1)</td></tr>
</table>
"""


@pytest.fixture(scope="module")
def text_pdf():
    return make_pdf(text_pdf_pages())


def scan_pdf(pages=2) -> bytes:
    imgs = []
    for i in range(pages):
        im = Image.new("RGB", (800, 1000), "white")
        ImageDraw.Draw(im).text((60, 80), f"scanned page {i + 1}", fill="black")
        imgs.append(im)
    buf = io.BytesIO()
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


class FakeOcr:
    """An extractor standing in for an OCR tier: returns the pages it is given."""
    def __init__(self, pages, kind="pdf"):
        self.pages, self.kind = pages, kind

    def pdf(self, data, name, language):
        return RawSource("pdf", name, self.pages, total_pages=len(self.pages))

    def image(self, data, name, language):
        return RawSource("image", name, self.pages, total_pages=len(self.pages))


# ------------------------------------------------------------------------------------------------ the pack
def test_a_text_pdf_gives_a_valid_pack_with_ordered_checklist_and_headings(text_pdf):
    pack = build_source_pack([IntakeInput("pdf", "course.pdf", data=text_pdf)], goal="Teach me data structures", language="en")
    SourcePack.model_validate(pack.model_dump())                                        # a valid, closed schema
    assert pack.sources[0].status == "readable" and pack.sources[0].pages == 3
    assert [c.text for c in pack.checklist] == ["Arrays and memory", "Linked lists", "Stacks and queues", "Trees", "Hash tables"]
    assert [c.order for c in pack.checklist] == [1, 2, 3, 4, 5]                          # contents rows are an ordered checklist, not prose
    heads = [h.text for h in pack.outline_hints]
    assert "Table of Contents" in heads and "2. Linked lists" in heads
    assert pack.brief.looks_like_syllabus and pack.brief.level_hint == "beginner"
    assert all(c.id and c.page >= 1 and c.source_id == "s1" for c in pack.chunks)
    assert any(c.kind == "code" and "class Node" in c.text for c in pack.chunks)
    assert {p.tier for p in pack.pages} == {"text-layer"}


def test_the_same_input_gives_the_same_ids(text_pdf):
    inputs = [IntakeInput("pdf", "a.pdf", data=text_pdf), IntakeInput("text", "notes", text="Binary search halves the range.\n\nIt needs sorted data.")]
    a, b = build_source_pack(inputs, "goal"), build_source_pack(inputs, "goal")
    assert [c.id for c in a.chunks] == [c.id for c in b.chunks] and len({c.id for c in a.chunks}) == len(a.chunks)
    assert {c.source_id for c in a.chunks} == {"s1", "s2"}


def test_ocr_markdown_tables_come_out_as_rows_not_one_long_line():
    text, tables = parse_html_tables(INDRA_PAGE2)
    assert tables == [(["Operation", "Array", "Linked list"], [["Access by index", "O(1)", "O(n)"], ["Insert at front", "O(n)", "O(1)"]])]
    pack = build_source_pack([IntakeInput("pdf", "x.pdf")], extractor=FakeOcr([RawPage(text=INDRA_PAGE2, markdown=INDRA_PAGE2, tier="indra-auto", confidence=0.99)]))
    t = pack.tables[0]
    assert t.columns == ["Operation", "Array", "Linked list"] and t.rows[0] == ["Access by index", "O(1)", "O(n)"] and t.caption == "Cost of common operations"
    assert any(c.kind == "table_ref" and t.id in c.text for c in pack.chunks)
    assert [c.heading_path for c in pack.chunks][0] == ["1. Arrays and memory"]          # the heading tree follows the Markdown


def test_markdown_pipe_tables_and_code_fences():
    blocks = parse_page("| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n\n```python\nprint('hi')\n```\n", markdown=True)
    assert blocks[0].type == "table" and blocks[0].columns == ["a", "b"] and blocks[0].rows == [["1", "2"], ["3", "4"]]
    assert blocks[1].type == "code" and "print" in blocks[1].text


def test_contents_rows_lose_their_numbers_leaders_and_page_numbers():
    assert clean_item("1. Arrays and memory ........ 3") == "Arrays and memory"
    assert clean_item("Chapter 4: Trees …… 21") == "Trees"
    assert clean_item("- Hash tables") == "Hash tables"


def test_running_headers_footers_and_repeated_pages_are_removed():
    pages = [f"Course Notes\n\nSection {i} has its own real content about topic number {i}.\n\nPage {i} of 6" for i in range(1, 7)]
    cleaned, removed = strip_furniture(pages)
    assert all("Course Notes" not in p and "Page " not in p for p in cleaned) and "Course Notes" in removed
    assert "topic number 3" in cleaned[2]
    assert dedupe_pages(["Same long page text repeated twice here, enough to count as a real page.", "Other text entirely, also long enough to count here.",
                         "Same long page text repeated twice here, enough to count as a real page."]) == [None, None, 0]


def test_hyphenated_line_breaks_are_repaired():
    assert normalize_text("An exam-\nple of a long word and a line\nbreak.") == "An example of a long word and a line\nbreak."


def test_pasted_text_markdown_links_and_goal():
    md = "# Graphs\n\nRead [the guide](https://example.com/graphs) and https://example.org/more.\n\n- nodes\n- edges\n- paths"
    pack = build_source_pack([IntakeInput("markdown", "notes.md", text=md)], goal="Learn graphs", language="en")
    assert pack.goal.text == "Learn graphs" and pack.brief.subject == "Learn graphs"
    assert {(l.url, l.anchor_text) for l in pack.links_found} == {("https://example.com/graphs", "the guide"), ("https://example.org/more", "")}
    assert [h.text for h in pack.outline_hints][:1] == ["Graphs"] and any(c.kind == "list" for c in pack.chunks)
    assert pack.markdown["s1"]


def test_a_fetched_link_becomes_a_source_and_a_failed_one_is_reported():
    ok = lambda u: {"ok": True, "title": "Binary search", "text": "Binary search finds an item in a sorted list.\n\nIt halves the range each step."}
    bad = lambda u: {"ok": False, "error": "The page needs a login."}
    p1 = build_source_pack([IntakeInput("url", "x", url="https://example.com/bs")], fetch=ok)
    p2 = build_source_pack([IntakeInput("url", "x", url="https://example.com/bs")], fetch=bad)
    assert p1.sources[0].kind == "url" and p1.sources[0].name == "Binary search" and p1.chunks
    assert p2.sources[0].status == "unreadable" and p2.sources[0].quality_report[0].reason == "The page needs a login." and not p2.chunks


# ------------------------------------------------------------------------------------------------ quality: never silent, never wrong
def test_a_scan_with_no_reader_is_reported_with_a_plain_reason():
    pack = build_source_pack([IntakeInput("pdf", "scan.pdf", data=scan_pdf(2))])
    s = pack.sources[0]
    assert s.status == "unreadable" and not pack.chunks
    assert "scan" in s.quality_report[0].reason and s.quality_report[0].pages == [1, 2]


def test_vision_reads_scans_and_a_dead_vision_model_still_gives_a_pack():
    seen = []
    good = lambda png, lang, label: (seen.append(label) or {"text": "A binary search halves the range each step on sorted data.", "empty": False})
    ok = build_source_pack([IntakeInput("pdf", "scan.pdf", data=scan_pdf(2))], extractor=BasicExtractor(vision=good))
    assert ok.sources[0].status == "readable" and {p.tier for p in ok.pages} == {"vision"} and len(seen) == 2
    assert all("scan" in p.escalated_because for p in ok.pages)                         # why each page left the cheap tier is recorded

    def dead(png, lang, label):
        raise RuntimeError("model is down")
    ko = build_source_pack([IntakeInput("pdf", "scan.pdf", data=scan_pdf(2))], extractor=BasicExtractor(vision=dead))
    assert ko.sources[0].status == "unreadable" and "could not make out" in ko.sources[0].quality_report[0].reason     # still a pack, with the reason


def test_wrong_characters_are_caught_by_the_page_health_gate():
    junk = "Wl SDuLL ul wD @uasSDe. for DDMLO while @)JOTL, GUSSGt."
    h = page_health(junk, 0.549, from_ocr=True)
    assert not h.ok and h.code == "low_confidence"                                       # the Tamil scan that Indra read as Latin garbage
    assert page_health(junk, 0.95, from_ocr=True, expected_script="Tamil").code == "unsupported_script"
    assert page_health("A normal English sentence about arrays and memory.", 0.97, from_ocr=True).ok
    assert page_health(TAMIL, 0.95, from_ocr=True).ok                                    # Indic vowel signs are combining marks: real Tamil must not look like stray characters
    assert page_health("12 ,, ;; ..", None).code in ("empty", "not_words")
    pack = build_source_pack([IntakeInput("image", "tamil.png")], extractor=FakeOcr([RawPage(text=junk, tier="indra-ocr", confidence=0.549)]))
    assert pack.sources[0].status == "unreadable" and not pack.chunks                    # never ships the garbage as content
    assert "not sure" in pack.sources[0].quality_report[0].reason


def test_scripts_and_languages_are_detected():
    assert dominant_script(TAMIL) == "Tamil" and language_of(TAMIL) == "ta" and not ocr_supports("Tamil")
    hi = "पाइथन में लूप"
    assert dominant_script(hi) == "Devanagari" and language_of(hi) == "hi" and ocr_supports("Devanagari")
    assert script_profile("abc குற") == {"Latin": 0.5, "Tamil": 0.5} or set(script_profile("abc குற")) == {"Latin", "Tamil"}


def test_a_page_limit_is_reported_not_silent(text_pdf):
    pack = build_source_pack([IntakeInput("pdf", "course.pdf", data=text_pdf)], extractor=BasicExtractor(max_pages=2))
    s = pack.sources[0]
    assert s.pages == 3 and s.status == "partly_readable"
    assert any(q.pages == [3] and "not read" in q.reason for q in s.quality_report)


def test_unsupported_kinds_and_empty_inputs_do_not_crash():
    pack = build_source_pack([IntakeInput("docx", "x.docx"), IntakeInput("text", "empty", text="")])
    assert [s.status for s in pack.sources] == ["unreadable", "unreadable"] and pack.chunks == []


# ------------------------------------------------------------------------------------------------ brief, decisions, boundaries
def test_brief_hints_are_cheap_and_conservative():
    jee = build_source_pack([IntakeInput("text", "n", text="JEE advanced mechanics problem set.\n\nProjectile motion basics.")], goal="Prepare me for JEE")
    assert jee.brief.track_hint == "jee" and jee.brief.size == "small"
    plain = build_source_pack([IntakeInput("text", "n", text="Photosynthesis turns light into sugar.")], goal="Photosynthesis")
    assert plain.brief.track_hint == "general" and plain.brief.level_hint == "unknown" and not plain.brief.looks_like_syllabus


def test_one_plain_language_decision_per_source():
    store = Store(":memory:")
    em = DecisionEmitter(store)
    build_source_pack([IntakeInput("pdf", "scan.pdf", data=scan_pdf(2)), IntakeInput("text", "notes", text="Arrays store items contiguously in memory.")],
                      emitter=em, notebook_id="nb1")
    recs = em.recent("nb1")
    assert [r["agent"] for r in recs] == ["intake", "intake"] and recs[0]["kind"] == "rejected" and recs[1]["kind"] == "accepted"
    assert recs[1]["counts"] == {"read": 1, "pages": 1} and "path" not in recs[0]["summary"].lower()


def test_intake_does_no_planning_searching_or_teaching():
    banned = {"architect", "curator", "teacher", "critic", "quizzer", "illustrator", "web", "tools", "engine", "router"}
    for f in (ROOT / "backend" / "app" / "intake").glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for m in mods:
                assert not set(m.split(".")) & banned, f"{f.name} imports {m}"
