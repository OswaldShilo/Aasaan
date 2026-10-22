"""Runs Indra OCR on ONE file in its own process and prints the pages as JSON. Never imported by the web server.

    python -m app.intake.indra_worker <mode: auto|ocr> <path> <cores> <max_pages>

OCR happens in a child process (Indra uses worker processes itself and needs a __main__ guard), so a crash, a hang or a memory spike
cannot take the web server down; the parent enforces a timeout and a page cap. Models come from INDRA_OCR_MODEL_DIR (baked into the image):
nothing is downloaded at run time.
"""
from __future__ import annotations

import json
import statistics
import sys
import time


def main(argv: list[str]) -> int:
    mode, path, cores, max_pages = argv[1], argv[2], int(argv[3]), int(argv[4])
    import indra_ocr
    conv = indra_ocr.load(threads=max(1, cores), mode=mode)
    t0 = time.perf_counter()
    pages = []
    for i, p in conv.convert(path, max_pages=max_pages):
        confs = [ln.conf for ln in p.lines]
        pages.append({"page": i + 1, "text": p.text, "markdown": p.markdown, "source": p.source, "note": p.note, "rotation": p.rotation,
                      "conf_mean": round(statistics.mean(confs), 3) if confs else None,
                      "blocks": [{"label": b.get("label"), "text": b.get("text", "")} for b in p.blocks]})
    json.dump({"version": indra_ocr.__version__, "seconds": round(time.perf_counter() - t0, 3), "pages": pages}, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
