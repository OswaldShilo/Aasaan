"""Scores the saved raw outputs (current path and Indra) with the same code and writes eval/indra/REPORT.md.

    python eval/indra/compare.py
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scoring import GROUND_TRUTH, score  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
DIGITAL = ("text.pdf", "cloud_de.pdf")
SCANS = ("scan.pdf", "notes_photo.png", "hindi_scan.png", "tamil_scan.pdf")
TOOLS = ("current", "indra_auto", "indra_ocr")


def load(tool: str, fixture: str):
    p = OUT / f"{tool}_{fixture}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def cell(v):
    return "n/a" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))


def main() -> int:
    rows, scored = [], {}
    for fx in GROUND_TRUTH:
        for tool in TOOLS:
            raw = load(tool, fx)
            if raw is None:
                continue
            s = score(fx, raw)
            scored[(fx, tool)] = s
            rows.append(f"| {fx} | {tool} | {s['pages']} | {cell(s.get('coverage'))} | {cell(s.get('seconds_per_page'))} | {cell(s.get('recall'))} | "
                        f"{cell(s.get('table_rows'))} | {cell(s.get('headings'))} | {cell(s.get('checklist'))} |")
    md = ["# Indra OCR spike: report", "",
          "Same fixtures, same scoring code (`scoring.py`) for the current path and for Indra OCR 0.3.2 (CPU, in Docker, no network at run time).",
          "Numbers are from one run on this machine; `seconds_per_page` for the current path's vision tier includes live model latency.", "",
          "| fixture | tool | pages read | coverage | s/page | recall | table rows on one line | headings | contents rows |", "|---|---|---|---|---|---|---|---|---|", *rows, ""]

    def med(tool: str, group: tuple[str, ...]):
        v = [scored[(f, tool)]["seconds_per_page"] for f in group if (f, tool) in scored and scored[(f, tool)].get("seconds_per_page") is not None]
        return round(statistics.median(v), 3) if v else None

    def mean(tool: str, group: tuple[str, ...], key: str):
        v = [scored[(f, tool)][key] for f in group if (f, tool) in scored and scored[(f, tool)].get(key) is not None]
        return round(statistics.mean(v), 2) if v else None
    md += ["## Summary", "", "| group | tool | median s/page | mean recall | mean coverage |", "|---|---|---|---|---|"]
    for name, group in (("digital PDFs", DIGITAL), ("scans and photos", SCANS)):
        for tool in TOOLS:
            if any((f, tool) in scored for f in group):
                md.append(f"| {name} | {tool} | {cell(med(tool, group))} | {cell(mean(tool, group, 'recall'))} | {cell(mean(tool, group, 'coverage'))} |")
    p = OUT / "REPORT_tables.md"
    p.write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
