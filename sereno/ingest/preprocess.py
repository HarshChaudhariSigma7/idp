"""Cheap, conservative auto-corrections. Every correction is recorded so the review screen and
eval slices know exactly what was done to a page. The original pixels are kept too: the second
extraction pass reads the original, so an enhancement artefact shows up as a disagreement."""
from __future__ import annotations

import cv2
import numpy as np

from sereno.ingest.quality import PageQuality, estimate_skew

TARGET_CHAR_HEIGHT = 20.0


def rotate_quarter(img: np.ndarray, degrees: int) -> np.ndarray:
    d = degrees % 360
    if d == 90:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if d == 180:
        return cv2.rotate(img, cv2.ROTATE_180)
    if d == 270:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img


def deskew(img: np.ndarray, angle: float) -> np.ndarray:
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), -angle, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    m[0, 2] += nw / 2 - w / 2
    m[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(img, m, (nw, nh), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))


def enhance(img: np.ndarray, q: PageQuality, max_long_edge: int = 4000) -> tuple[np.ndarray, list[str]]:
    applied: list[str] = []
    if q.native_text:
        return img, applied
    out = img

    if abs(q.skew_deg) >= 0.4:
        out = deskew(out, q.skew_deg)
        applied.append(f"deskew:{q.skew_deg:+.1f}")
        # verify: a wrong skew estimate must not make things worse
        gray = cv2.cvtColor(out, cv2.COLOR_RGB2GRAY)
        if abs(estimate_skew(gray)) > abs(q.skew_deg):
            out = img
            applied[-1] = "deskew:reverted"

    if 0 < q.char_height_px < 14:
        factor = min(TARGET_CHAR_HEIGHT / q.char_height_px, 3.0)
        h, w = out.shape[:2]
        if max(h, w) * factor > max_long_edge:
            factor = max_long_edge / max(h, w)
        if factor > 1.15:
            out = cv2.resize(out, (int(w * factor), int(h * factor)), interpolation=cv2.INTER_CUBIC)
            applied.append(f"upscale:{factor:.2f}x")

    if q.noise > 6:
        out = cv2.fastNlMeansDenoisingColored(out, None, 5, 5, 7, 21)
        applied.append("denoise")

    if q.text_contrast < 0.45 or q.rms_contrast < 0.18:
        lab = cv2.cvtColor(out, cv2.COLOR_RGB2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        l = clahe.apply(l)
        # stretch faded ink (carbon copies) towards black while keeping paper white
        lo, hi = np.percentile(l, 1), np.percentile(l, 95)
        if hi - lo > 10:
            l = np.clip((l.astype(np.float32) - lo) * 255.0 / (hi - lo), 0, 255).astype(np.uint8)
        out = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2RGB)
        applied.append("contrast")

    if q.sharpness_norm < 100:
        blur = cv2.GaussianBlur(out, (0, 0), 1.2)
        out = cv2.addWeighted(out, 1.6, blur, -0.6, 0)
        applied.append("sharpen")

    return out, applied


def fit_long_edge(img: np.ndarray, long_edge: int) -> np.ndarray:
    h, w = img.shape[:2]
    if max(h, w) <= long_edge:
        return img
    s = long_edge / max(h, w)
    return cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)


def content_frame(img: np.ndarray, pad: float = 0.02, min_saving: float = 0.08) -> tuple[float, float, float, float]:
    """Fractional (x0, y0, x1, y1) of the inked area, padded. Blank scanner margins cost image tokens
    and dilute resolution; trimming them gives the text more pixels for the same token budget.
    Returns the full frame when trimming would save under min_saving of the area (or on dark
    backgrounds such as phone photos on a desk, where everything looks like ink)."""
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if img.ndim == 3 else img
    small = fit_long_edge(gray, 800)
    ink = small < min(200, int(np.median(small)) - 40)
    rows, cols = np.where(ink.mean(axis=1) > 0.003)[0], np.where(ink.mean(axis=0) > 0.003)[0]
    full = (0.0, 0.0, 1.0, 1.0)
    if len(rows) < 2 or len(cols) < 2:
        return full
    h, w = ink.shape
    x0, x1 = max(0.0, cols[0] / w - pad), min(1.0, (cols[-1] + 1) / w + pad)
    y0, y1 = max(0.0, rows[0] / h - pad), min(1.0, (rows[-1] + 1) / h + pad)
    if (x1 - x0) * (y1 - y0) > 1 - min_saving:
        return full
    return (float(x0), float(y0), float(x1), float(y1))


def cut(img: np.ndarray, frame: tuple[float, float, float, float]) -> np.ndarray:
    h, w = img.shape[:2]
    x0, y0, x1, y1 = frame
    return img[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]