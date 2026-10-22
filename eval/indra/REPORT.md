# Indra OCR spike: report

Run on 2026-10-18, Windows 11 host for the current path and Docker (python:3.12-slim, 4 cores, `--network none`) for Indra OCR 0.3.2 (Apache-2.0 code; models
`datalab-to/indra-ocr` revision v3.1.1 baked into the image). Same fixtures and the same scoring code (`scoring.py`) for both. One run each: treat the timings as
indicative. The current path's vision tier is live Gemma 4 (31B, falling back to 26B), so its seconds include network time and rate-limit retries.

Fixtures (all generated, `make_fixtures.py`): `text.pdf` (digital: contents list, headings, a table, code), `scan.pdf` (the same pages as skewed, noisy images),
`notes_photo.png` (photo-like notes), `hindi_scan.png` (Devanagari), `tamil_scan.pdf` (Tamil, a script Indra's OCR does not support), `cloud_de.pdf` (the real syllabus, 75 pages).
Not covered by the spike: handwriting, complex merged-cell tables, math-heavy pages (the `--math` option exists; not measured), pasted text and links (no OCR involved).


## What the numbers say

| Rule from the brief | Result |
|---|---|
| Page coverage at least as good as the current path | **Met.** Equal on every fixture. On `cloud_de.pdf` Indra opens all **75** pages; the current path stops at its **20**-page cap. Both find the same 8 pages with text, and Indra's OCR confirms the rest are empty grids (so the earlier "unreadable pages" are genuinely blank, not misses). |
| Median time per page lower | **Met for scans and photos** (0.45 s vs 6.6 s, up to 90x faster: 0.49 s vs 42.6 s on the Hindi page). **Not met for digital pages**: reading the text layer with pypdf is ~0.01 s/page; Indra `auto` is ~0.25 s/page (the 75-page syllabus takes 12 s). |
| Heading and contents detection not worse | **Met, and better.** The current path returns flat text (no heading structure); Indra returns `Section-Header`, `List-Group`, `Table`, page headers and a markdown/HTML rendering. Contents rows are recovered equally (1.00). Tables: both keep each row on one line, Indra emits a real `<table>`. |
| Indian scripts | **Mixed.** Devanagari (Hindi): Indra reads it at 0.49 s/page, but with low line confidence (0.675). Tamil: Indra returns **wrong Latin characters** (confidence 0.549, recall 0.00); the vision model recovered 1 of 3 phrases. So Tamil/Kannada/Telugu/Malayalam scans must go to the vision tier, and a low-confidence Indra page must never be trusted silently. |
| No test regresses | Not applicable to the spike (no production code changed). |

## Decision (by the stated rule)

1. **Adopt Indra as tier B** (scans and photos in Latin, Cyrillic, Greek, Devanagari, Bengali, Chinese): it meets every rule and is an order of magnitude faster, offline, and CPU-only.
2. **Tier A (digital PDFs): adopt as a structure pass, not as the text reader.** It fails the speed rule against the raw text layer, but it is the only source of headings, tables and the contents checklist
   and costs ~0.25 s/page in a background worker. Plan: keep pypdf for the plain text, run Indra `auto` for structure; if the structure pass is skipped or fails, the pypdf text is still a valid pack.
3. **Tier C stays mandatory** for unsupported scripts on scans and for any page whose Indra confidence is low. The page-health gate escalates when: script detection says the page is Tamil/Kannada/Telugu/Malayalam/Gujarati
   (detect from the text layer, the image, or a mismatch such as Latin letters in the wrong shapes), mean line confidence is below about 0.7, or the page text is mostly punctuation/letters with no words.
   Under the `Local OCR only` setting such pages are reported unreadable with that reason.

## Operating notes

- The image pins `indra-ocr==0.3.2` and bakes the models (`INDRA_OCR_MODEL_DIR=/opt/indra-models`); it ran with `--network none`. A production image must do the same.
- Windows is not a supported host: run it in Docker. OCR must live in a separate worker process or subprocess (its multi-process helper needs a `__main__` guard), limited to 1 or 2 workers with per-file timeouts and a page cap.
- Licence: code Apache-2.0; the model weights carry a modified OpenRAIL-M licence (free for research, personal use and startups under the stated revenue/funding limit; not for competing with Datalab's API). Record it in the notices and re-check before any commercial launch.
  Whether this counts as open-weight for the hackathon's open-source track is a question for the organisers.
- The repository showed a single commit when the brief was written; the PyPI package (0.3.2) is versioned and installs cleanly, but treat it as young: pin the version and keep every fallback.

## Tables
Same fixtures, same scoring code (`scoring.py`) for the current path and for Indra OCR 0.3.2 (CPU, in Docker, no network at run time).
Numbers are from one run on this machine; `seconds_per_page` for the current path's vision tier includes live model latency.

| fixture | tool | pages read | coverage | s/page | recall | table rows on one line | headings | contents rows |
|---|---|---|---|---|---|---|---|---|
| text.pdf | current | 3 | 1.00 | 0.00 | 1.00 | 1.00 | n/a | 1.00 |
| text.pdf | indra_auto | 3 | 1.00 | 0.33 | 1.00 | 1.00 | 1.00 | 1.00 |
| text.pdf | indra_ocr | 3 | 1.00 | 0.48 | 1.00 | 1.00 | 1.00 | 1.00 |
| scan.pdf | current | 3 | 1.00 | 7.36 | 1.00 | 1.00 | n/a | 1.00 |
| scan.pdf | indra_auto | 3 | 1.00 | 0.71 | 1.00 | 1.00 | 1.00 | 1.00 |
| scan.pdf | indra_ocr | 3 | 1.00 | 0.57 | 1.00 | 1.00 | 1.00 | 1.00 |
| notes_photo.png | current | 1 | 1.00 | 5.85 | 1.00 | n/a | n/a | n/a |
| notes_photo.png | indra_ocr | 1 | 1.00 | 0.40 | 1.00 | n/a | n/a | n/a |
| hindi_scan.png | current | 1 | 1.00 | 42.55 | 1.00 | n/a | n/a | n/a |
| hindi_scan.png | indra_ocr | 1 | 1.00 | 0.49 | 1.00 | n/a | n/a | n/a |
| tamil_scan.pdf | current | 1 | 1.00 | 5.49 | 0.33 | n/a | n/a | n/a |
| tamil_scan.pdf | indra_auto | 1 | 1.00 | 0.43 | 0.00 | n/a | n/a | n/a |
| tamil_scan.pdf | indra_ocr | 1 | 1.00 | 0.37 | 0.00 | n/a | n/a | n/a |
| cloud_de.pdf | current | 20 | 0.40 | 0.02 | n/a | n/a | n/a | n/a |
| cloud_de.pdf | indra_auto | 75 | 0.40 | 0.16 | n/a | n/a | n/a | n/a |
| cloud_de.pdf | indra_ocr | 75 | 0.40 | 0.23 | n/a | n/a | n/a | n/a |

## Summary

| group | tool | median s/page | mean recall | mean coverage |
|---|---|---|---|---|
| digital PDFs | current | 0.01 | 1.00 | 0.70 |
| digital PDFs | indra_auto | 0.25 | 1.00 | 0.70 |
| digital PDFs | indra_ocr | 0.35 | 1.00 | 0.70 |
| scans and photos | current | 6.60 | 0.83 | 1.00 |
| scans and photos | indra_auto | 0.57 | 0.50 | 1.00 |
| scans and photos | indra_ocr | 0.45 | 0.75 | 1.00 |
