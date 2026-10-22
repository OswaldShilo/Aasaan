"""Builds the Intake fixture set for the Indra OCR spike (and later the Intake tests). Run on the host:  python eval/indra/make_fixtures.py

Fixtures (all generated, so nothing copyrighted is committed; the output folder is git-ignored):
  text.pdf          a digital PDF with a real text layer: contents list, headings, a table, a code block (3 pages, Latin)
  scan.pdf          the same pages rendered to images and saved image-only, lightly skewed and noisy (a scanned book)
  notes_photo.png   a photo-like image of study notes (paper colour, skew, blur, shadow gradient)
  hindi_scan.png    Devanagari text as an image (a script Indra's OCR supports)
  tamil_scan.pdf    Tamil text as an image-only PDF (a script Indra's OCR does NOT support on scans)
  cloud_de.pdf      the Cloud Data Engineering syllabus (copied from eval/data): partly drawn, partly text
"""
from __future__ import annotations

import io
import random
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "eval" / "indra" / "fixtures"
FONTS = Path("C:/Windows/Fonts")


# ------------------------------------------------------------------------------------------- a tiny PDF writer (base-14 fonts only)
def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[tuple]]) -> bytes:
    """pages: list of draw ops. ("t", x, y, font, size, text) text; ("l", x1, y1, x2, y2) line. Origin bottom-left, A4 in points."""
    objs: list[bytes] = []

    def add(b: bytes) -> int:
        objs.append(b)
        return len(objs)
    add(b"")                                   # 1: catalog (filled below)
    add(b"")                                   # 2: pages  (filled below)
    fonts = {"F1": "Helvetica", "F2": "Helvetica-Bold", "F3": "Courier"}
    fids = {k: add(f"<< /Type /Font /Subtype /Type1 /BaseFont /{v} >>".encode()) for k, v in fonts.items()}
    kids = []
    for ops in pages:
        parts = []
        for op in ops:
            if op[0] == "t":
                _, x, y, f, size, text = op
                parts.append(f"BT /{f} {size} Tf {x} {y} Td ({_esc(text)}) Tj ET")
            else:
                _, x1, y1, x2, y2 = op
                parts.append(f"0.5 w {x1} {y1} m {x2} {y2} l S")
        body = "\n".join(parts).encode("latin-1")
        cid = add(b"<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream")
        res = "<< /Font << " + " ".join(f"/{k} {v} 0 R" for k, v in fids.items()) + " >> >>"
        kids.append(add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources {res} /Contents {cid} 0 R >>".encode()))
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = ("<< /Type /Pages /Count %d /Kids [%s] >>" % (len(kids), " ".join(f"{k} 0 R" for k in kids))).encode()
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, start=1):
        offs.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for o in offs:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def text_pdf_pages() -> list[list[tuple]]:
    p1 = [("t", 72, 760, "F2", 22, "Introduction to Data Structures"), ("t", 72, 735, "F1", 11, "A short course book (generated test fixture)"),
          ("t", 72, 690, "F2", 14, "Table of Contents"),
          *[("t", 72, 660 - i * 22, "F1", 12, f"{n}. {t}") for i, (n, t) in enumerate([(1, "Arrays and memory ........ 3"), (2, "Linked lists ........ 9"), (3, "Stacks and queues ........ 14"),
                                                                                   (4, "Trees ........ 21"), (5, "Hash tables ........ 30")])]]
    p2 = [("t", 72, 760, "F2", 16, "1. Arrays and memory"), ("t", 72, 735, "F1", 11, "An array stores items in contiguous memory, so the item at index i is found in constant time."),
          ("t", 72, 720, "F1", 11, "Insertion in the middle is slow because later items must be shifted one place to the right."),
          ("t", 72, 685, "F2", 12, "Cost of common operations"),
          ("l", 72, 675, 460, 675), ("t", 72, 660, "F2", 11, "Operation"), ("t", 230, 660, "F2", 11, "Array"), ("t", 330, 660, "F2", 11, "Linked list"), ("l", 72, 652, 460, 652),
          ("t", 72, 636, "F1", 11, "Access by index"), ("t", 230, 636, "F1", 11, "O(1)"), ("t", 330, 636, "F1", 11, "O(n)"),
          ("t", 72, 616, "F1", 11, "Insert at front"), ("t", 230, 616, "F1", 11, "O(n)"), ("t", 330, 616, "F1", 11, "O(1)"),
          ("t", 72, 596, "F1", 11, "Search"), ("t", 230, 596, "F1", 11, "O(n)"), ("t", 330, 596, "F1", 11, "O(n)"), ("l", 72, 588, 460, 588)]
    p3 = [("t", 72, 760, "F2", 16, "2. Linked lists"), ("t", 72, 735, "F1", 11, "Each node holds a value and a pointer to the next node."),
          ("t", 72, 700, "F2", 12, "Example"),
          ("t", 80, 680, "F3", 10, "class Node:"), ("t", 80, 666, "F3", 10, "    def __init__(self, value):"), ("t", 80, 652, "F3", 10, "        self.value = value"),
          ("t", 80, 638, "F3", 10, "        self.next = None")]
    return [p1, p2, p3]


# ------------------------------------------------------------------------------------------- scan and photo effects
def _font(name: str, size: int, index: int = 0) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size, index=index)


def scanify(img: Image.Image, skew: float = 0.8, seed: int = 1) -> Image.Image:
    rnd = random.Random(seed)
    img = img.convert("L").rotate(skew, expand=True, fillcolor=255, resample=Image.BICUBIC)
    px = img.load()
    for _ in range(img.width * img.height // 60):                 # light speckle noise
        x, y = rnd.randrange(img.width), rnd.randrange(img.height)
        px[x, y] = max(0, px[x, y] - rnd.randrange(40, 120))
    return img.filter(ImageFilter.GaussianBlur(0.6)).convert("RGB")


def save_image_pdf(images: list[Image.Image], path: Path) -> None:
    images[0].save(path, "PDF", save_all=True, append_images=images[1:], resolution=150)


def render_text_page(lines: list[tuple[str, int, ImageFont.FreeTypeFont]], size=(1240, 1754)) -> Image.Image:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    y = 140
    for text, gap, font in lines:
        d.text((120, y), text, fill="black", font=font)
        y += gap
    return img


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    pdf = make_pdf(text_pdf_pages())
    (OUT / "text.pdf").write_bytes(pdf)
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf)
    pages = [scanify(doc[i].render(scale=2.0).to_pil(), skew=0.6 + i * 0.3, seed=i) for i in range(len(doc))]
    save_image_pdf(pages, OUT / "scan.pdf")
    # photo of notes: paper colour, perspective-ish skew, shadow gradient, blur
    n = Image.new("RGB", (1600, 1100), (244, 238, 222))
    d = ImageDraw.Draw(n)
    f1, f2 = _font("arial.ttf", 44), _font("timesi.ttf", 38)
    notes = ["Binary search - notes", "1. Sorted array only", "2. low = 0, high = n - 1", "3. mid = (low + high) // 2", "4. if a[mid] == x: found", "   else shrink the half  -> O(log n)"]
    y = 90
    for i, t in enumerate(notes):
        d.text((110, y), t, fill=(30, 30, 60), font=f1 if i == 0 else f2)
        y += 110 if i == 0 else 90
    shade = Image.linear_gradient("L").resize(n.size).rotate(90)
    n = Image.composite(n, Image.new("RGB", n.size, (150, 140, 120)), shade.point(lambda v: 150 + v // 3)).rotate(-2.2, expand=True, fillcolor=(90, 80, 70)).filter(ImageFilter.GaussianBlur(1.1))
    n.save(OUT / "notes_photo.png")
    # Devanagari (supported by Indra OCR) and Tamil (not) as images
    nir = FONTS / "Nirmala.ttc"
    hi = render_text_page([("\u092a\u093e\u0907\u0925\u0928 \u092e\u0947\u0902 \u0932\u0942\u092a", 120, ImageFont.truetype(str(nir), 64)),
                           ("\u0932\u0942\u092a \u0915\u0908 \u092c\u093e\u0930 \u0915\u094b\u0921 \u0926\u094b\u0939\u0930\u093e\u0924\u093e \u0939\u0948\u0964", 90, ImageFont.truetype(str(nir), 44)),
                           ("for \u0914\u0930 while \u0926\u094b \u092a\u094d\u0930\u0915\u093e\u0930 \u0939\u0948\u0902\u0964", 90, ImageFont.truetype(str(nir), 44))], size=(1240, 700))
    scanify(hi, 0.5, 7).save(OUT / "hindi_scan.png")
    ta = render_text_page([("\u0baa\u0bc8\u0ba4\u0bcd\u0ba4\u0bbe\u0ba9\u0bbf\u0bb2\u0bcd \u0bb2\u0bc2\u0baa\u0bcd", 120, ImageFont.truetype(str(nir), 64, index=1) if False else ImageFont.truetype(str(nir), 64)),
                           ("\u0bb2\u0bc2\u0baa\u0bcd \u0b95\u0bc1\u0bb1\u0bbf\u0baf\u0bc0\u0b9f\u0bcd\u0b9f\u0bc8 \u0baa\u0bb2 \u0bae\u0bc1\u0bb1\u0bc8 \u0b87\u0baf\u0b95\u0bcd\u0b95\u0bc1\u0b95\u0bbf\u0bb1\u0ba4\u0bc1.", 90, ImageFont.truetype(str(nir), 44)),
                           ("for \u0bae\u0bb1\u0bcd\u0bb1\u0bc1\u0bae\u0bcd while \u0b87\u0bb0\u0ba3\u0bcd\u0b9f\u0bc1 \u0bb5\u0b95\u0bc8\u0b95\u0bb3\u0bcd.", 90, ImageFont.truetype(str(nir), 44))], size=(1240, 700))
    save_image_pdf([scanify(ta, 0.4, 9)], OUT / "tamil_scan.pdf")
    shutil.copy(ROOT / "eval" / "data" / "TOC-Cloud_Data_Engineer.pdf", OUT / "cloud_de.pdf")
    for f in sorted(OUT.iterdir()):
        print(f"{f.name:18s} {f.stat().st_size // 1024:6d} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
