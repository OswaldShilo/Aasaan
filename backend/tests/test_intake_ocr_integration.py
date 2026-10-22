"""The real Indra engine through the Intake ladder. Needs `indra-ocr` and its models (INDRA_OCR_MODEL_DIR): run inside Docker, e.g.

    docker run --rm --network none --entrypoint sh -v "$PWD:/repo" aasaan-indra-spike -c \
      "pip install -q -r /repo/backend/requirements.txt && cd /repo/backend && python -m pytest -q -m integration tests/test_intake_ocr_integration.py"

Skipped everywhere else (no engine, or a Windows host).
"""
import sys
from pathlib import Path

import pytest

pytest.importorskip("indra_ocr")
from app.intake import IntakeInput, build_source_pack  # noqa: E402
from app.intake.ocr import LOCAL_ONLY_REASON, LadderExtractor, indra_available  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.integration, pytest.mark.skipif(not indra_available(), reason="Indra models not on disk (INDRA_OCR_MODEL_DIR)")]

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval" / "indra"))
from make_fixtures import make_pdf, text_pdf_pages, save_image_pdf, scanify  # noqa: E402

CHECKLIST = ["Arrays and memory", "Linked lists", "Stacks and queues", "Trees", "Hash tables"]


@pytest.fixture(scope="module")
def pdfs(tmp_path_factory):
    import pypdfium2 as pdfium
    data = make_pdf(text_pdf_pages())
    doc = pdfium.PdfDocument(data)
    out = tmp_path_factory.mktemp("scan") / "scan.pdf"
    save_image_pdf([scanify(doc[i].render(scale=2.0).to_pil(), 0.6 + i * 0.3, i) for i in range(len(doc))], out)
    return data, out.read_bytes()


def test_a_digital_pdf_gets_structure_from_indra_auto(pdfs):
    pack = build_source_pack([IntakeInput("pdf", "text.pdf", data=pdfs[0])], extractor=LadderExtractor(None, local_only=True))
    assert pack.sources[0].status == "readable" and {p.tier for p in pack.pages} == {"indra-auto"}
    assert [c.text for c in pack.checklist] == CHECKLIST
    assert pack.tables and pack.tables[0].columns == ["Operation", "Array", "Linked list"] and ["Access by index", "O(1)", "O(n)"] in pack.tables[0].rows
    assert {"Table of Contents", "1. Arrays and memory", "Cost of common operations"} <= {h.text for h in pack.outline_hints}


def test_a_scanned_book_is_read_offline_with_headings_tables_and_contents(pdfs):
    pack = build_source_pack([IntakeInput("pdf", "scan.pdf", data=pdfs[1])], extractor=LadderExtractor(None, local_only=True))
    assert pack.sources[0].status == "readable" and {p.tier for p in pack.pages} == {"indra-ocr"} and all(p.confidence and p.confidence > 0.9 for p in pack.pages)
    assert [c.text for c in pack.checklist] == CHECKLIST
    assert pack.tables and ["Insert at front", "O(n)", "O(1)"] in pack.tables[0].rows
    assert any(c.kind == "code" for c in pack.chunks) or "class Node" in pack.markdown["s1"]


def test_an_unreadable_script_is_never_shipped_as_wrong_characters():
    f = ROOT / "eval" / "indra" / "fixtures" / "tamil_scan.pdf"
    if not f.exists():
        pytest.skip("run eval/indra/make_fixtures.py on the host first (needs a Tamil font)")
    local = build_source_pack([IntakeInput("pdf", "tamil.pdf", data=f.read_bytes())], extractor=LadderExtractor(None, local_only=True))
    assert local.sources[0].status == "unreadable" and not local.chunks and local.sources[0].quality_report[0].reason == LOCAL_ONLY_REASON
    seen = []
    vision = lambda png, lang, label: (seen.append(label) or {"text": "பைத்தானில் லூப் குறியீட்டை பல முறை", "empty": False})
    with_vision = build_source_pack([IntakeInput("pdf", "tamil.pdf", data=f.read_bytes())], extractor=LadderExtractor(vision, local_only=False))
    assert seen and with_vision.pages[0].tier == "vision" and with_vision.pages[0].escalated_because          # escalated, with the reason on record
