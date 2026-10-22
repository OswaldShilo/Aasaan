"""Runs Indra OCR over the fixtures inside the container and writes one JSON per fixture and mode (raw output, scored later on the host).

    docker run --rm -v <fixtures>:/in:ro -v <out>:/out aasaan-indra-spike [--only name]
No network is needed: the models are in the image (INDRA_OCR_MODEL_DIR).
"""
import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

import indra_ocr


def page_dict(i: int, page) -> dict:
    confs = [ln.conf for ln in page.lines]
    return {"page": i + 1, "text": page.text, "markdown": page.markdown, "blocks": [{"label": b.get("label"), "text": b.get("text", "")} for b in page.blocks],
            "source": page.source, "note": page.note, "rotation": page.rotation, "skew": round(page.skew, 2), "lines": len(page.lines),
            "conf_mean": round(statistics.mean(confs), 3) if confs else None, "conf_min": round(min(confs), 3) if confs else None,
            "timings_ms": {k: round(v, 1) for k, v in page.timings.items() if isinstance(v, (int, float))}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--math", action="store_true")
    a = ap.parse_args()
    src, out = Path("/in"), Path("/out")
    out.mkdir(parents=True, exist_ok=True)
    cores = indra_ocr.usable_cores()
    print("indra-ocr", indra_ocr.__version__, "cores", cores, "models", indra_ocr.model_dir(), flush=True)
    for f in sorted(src.iterdir()):
        if a.only and f.name != a.only:
            continue
        for mode in (("auto", "ocr") if f.suffix == ".pdf" else ("ocr",)):
            conv = indra_ocr.load(threads=cores, mode=mode, math=a.math)
            t0 = time.perf_counter()
            pages = [page_dict(i, p) for i, p in conv.convert(f)]
            secs = round(time.perf_counter() - t0, 3)
            name = f"indra_{mode}{'_math' if a.math else ''}_{f.name}.json"
            (out / name).write_text(json.dumps({"tool": f"indra-{mode}", "version": indra_ocr.__version__, "cores": cores, "pages": pages, "seconds": secs},
                                               ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"[indra {mode}] {f.name}: {len(pages)} page(s) in {secs}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
