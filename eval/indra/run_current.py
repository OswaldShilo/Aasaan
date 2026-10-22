"""The CURRENT Intake extraction on each fixture, timed. Writes eval/indra/out/current_<fixture>.json.

PDFs: pypdf text layer (analyze_pdf); pages with no text then go to the vision model (agents.intake.ocr_page), like the live path. Images: the vision model.
The vision model is live (Gemma 4 via the router), so these numbers include network time. Use --no-vision for the code-only part.
    python eval/indra/run_current.py [--no-vision] [--only name]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
FIX, OUT = ROOT / "eval" / "indra" / "fixtures", ROOT / "eval" / "indra" / "out"

from app.agents import intake  # noqa: E402
from app.db import Store  # noqa: E402
from app.main import load_env  # noqa: E402
from app.router import ModelRouter  # noqa: E402


def png_of_image(path: Path) -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.open(path).convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def run(path: Path, router, vision: bool) -> dict:
    t0 = time.perf_counter()
    pages: list[dict] = []
    tiers: list[str] = []
    if path.suffix == ".pdf":
        data = path.read_bytes()
        texts, empties = intake.analyze_pdf(data)
        by = {e["page"]: e for e in empties}
        for n, t in enumerate(texts, start=1):
            if t or not vision or n not in by or (by[n]["images"] == 0 and by[n]["content_bytes"] < intake.VECTOR_TEXT_BYTES):
                pages.append({"text": t})
                tiers.append("text-layer" if t else "no-text")
                continue
            png = intake.render_page_png(data, n)
            r = intake.ocr_page(router, png, "en", path.name) if png else {"text": "", "failed": True}
            pages.append({"text": r.get("text", "")})
            tiers.append("vision")
    elif vision:
        r = intake.ocr_page(router, png_of_image(path), "en", path.name)
        pages.append({"text": r.get("text", "")})
        tiers.append("vision")
    else:
        pages.append({"text": ""})
        tiers.append("not-attempted")
    return {"tool": "current", "pages": pages, "tiers": tiers, "seconds": round(time.perf_counter() - t0, 3)}


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-vision", action="store_true")
    ap.add_argument("--only")
    a = ap.parse_args()
    router = ModelRouter(store=Store(":memory:"))
    OUT.mkdir(parents=True, exist_ok=True)
    for f in sorted(FIX.iterdir()):
        if a.only and a.only != f.name:
            continue
        print(f"[current] {f.name} ...", flush=True)
        (OUT / f"current_{f.name}.json").write_text(json.dumps(run(f, router, not a.no_vision), ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
