"""Turn an upload (PDF / JPEG / PNG / TIFF / WebP) into page images plus any native text layer."""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageOps

A4_LONG_IN = 11.69


class UnsupportedDocument(ValueError):
    """Raised with a plain-language message the uploader can act on."""


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float  # normalised 0..1 page coordinates


@dataclass
class LoadedPage:
    page_no: int
    image: np.ndarray  # RGB uint8
    text: str = ""
    words: list[Word] = field(default_factory=list)
    native_text: bool = False       # real digital text layer (not an OCR overlay on a scan)
    source_dpi: float | None = None  # effective resolution of the original pixels


@dataclass
class LoadedDocument:
    pages: list[LoadedPage]
    mime: str

    @property
    def is_digital(self) -> bool:
        return bool(self.pages) and all(p.native_text for p in self.pages)


def sniff_mime(data: bytes, filename: str = "") -> str:
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        raise UnsupportedDocument("iPhone HEIC photos aren't supported yet. Please share as JPEG "
                                  "(Settings > Camera > Formats > Most Compatible) or PDF.")
    raise UnsupportedDocument("This file type isn't supported. Please upload a PDF, JPEG, PNG or TIFF.")


def load(data: bytes, filename: str = "", max_pages: int = 12, render_dpi: int = 200) -> LoadedDocument:
    mime = sniff_mime(data, filename)
    if mime == "application/pdf":
        return LoadedDocument(_load_pdf(data, max_pages, render_dpi), mime)
    return LoadedDocument(_load_image(data, max_pages), mime)


def _load_image(data: bytes, max_pages: int) -> list[LoadedPage]:
    img = Image.open(io.BytesIO(data))
    pages = []
    idx = 0
    while True:
        frame = ImageOps.exif_transpose(img.copy()).convert("RGB")
        dpi = None
        info_dpi = img.info.get("dpi")
        if info_dpi and info_dpi[0] and float(info_dpi[0]) > 50:
            dpi = float(info_dpi[0])
        else:
            dpi = max(frame.size) / A4_LONG_IN  # assume an A4-ish document fills the frame
        pages.append(LoadedPage(page_no=idx + 1, image=np.asarray(frame), source_dpi=dpi))
        idx += 1
        if idx >= max_pages:
            break
        try:
            img.seek(idx)
        except EOFError:
            break
    return pages


def _load_pdf(data: bytes, max_pages: int, render_dpi: int) -> list[LoadedPage]:
    import pdfplumber
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except Exception as e:  # encrypted / corrupt
        raise UnsupportedDocument("This PDF couldn't be opened. It may be password-protected or damaged. "
                                  "Please upload an unlocked copy.") from e
    n = min(len(pdf), max_pages)
    pages: list[LoadedPage] = []
    with pdfplumber.open(io.BytesIO(data)) as plumb:
        for i in range(n):
            pp = plumb.pages[i]
            w_pt, h_pt = float(pp.width), float(pp.height)
            words_raw = pp.extract_words(keep_blank_chars=False, use_text_flow=True) or []
            words = [Word(w["text"], w["x0"] / w_pt, w["top"] / h_pt, w["x1"] / w_pt, w["bottom"] / h_pt)
                     for w in words_raw]
            text = pp.extract_text() or ""
            # A page that is essentially one big image is a scan, even if an OCR text layer exists.
            big_images = [im for im in pp.images
                          if (im["x1"] - im["x0"]) * (im["bottom"] - im["top"]) > 0.6 * w_pt * h_pt]
            printable = sum(ch.isprintable() and not ch.isspace() for ch in text)
            garbage = sum(ch == "�" for ch in text)
            native = printable >= 40 and garbage < 0.02 * max(printable, 1) and not big_images
            source_dpi = None
            if big_images:
                im = big_images[0]
                src_w = (im.get("srcsize") or (0, 0))[0]
                width_in = (im["x1"] - im["x0"]) / 72.0
                if src_w and width_in:
                    source_dpi = src_w / width_in
            dpi = render_dpi
            if source_dpi:
                dpi = int(min(max(source_dpi, 150), 300))
            bitmap = pdf[i].render(scale=dpi / 72.0)
            img = np.asarray(bitmap.to_pil().convert("RGB"))
            pages.append(LoadedPage(page_no=i + 1, image=img, text=text if native else "",
                                    words=words if native else [], native_text=native,
                                    source_dpi=source_dpi if source_dpi else float(dpi)))
    pdf.close()
    return pages


def encode_png(img: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def encode_jpeg(img: np.ndarray, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()
