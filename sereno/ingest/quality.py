"""Pre-extraction quality signals. Cheap (tens of ms per page), computed before any model call,
and logged per document so accuracy can be sliced by quality bucket."""
from __future__ import annotations

from dataclasses import asdict, dataclass

import cv2
import numpy as np

NORM_WIDTH = 1600


@dataclass
class PageQuality:
    width: int
    height: int
    source_dpi: float | None
    sharpness: float        # Laplacian variance inside text regions, at a normalised scale
    sharpness_norm: float   # sharpness / text contrast^2: blur independent of how dark the ink is
    rms_contrast: float     # std(gray)/255
    text_contrast: float    # (paper - ink)/255, low for faded / carbon copies
    ink_ratio: float        # share of dark pixels; ~0 means blank page
    char_height_px: float   # median glyph height in original pixels
    skew_deg: float
    noise: float
    native_text: bool

    def as_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def _gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if img.ndim == 3 else img


def _normalise(gray: np.ndarray) -> tuple[np.ndarray, float]:
    h, w = gray.shape
    scale = NORM_WIDTH / w
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    return cv2.resize(gray, (NORM_WIDTH, max(1, int(h * scale))), interpolation=interp), scale


def binarize(gray: np.ndarray) -> np.ndarray:
    """Ink = 255. Adaptive threshold copes with uneven lighting in phone photos."""
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    return cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 15)


def estimate_skew(gray: np.ndarray, max_angle: float = 12.0) -> float:
    """Projection-profile skew: text lines give the sharpest row profile when horizontal."""
    small_w = 900
    scale = small_w / gray.shape[1]
    small = cv2.resize(gray, (small_w, max(1, int(gray.shape[0] * scale))), interpolation=cv2.INTER_AREA)
    bw = binarize(small)
    if bw.mean() < 0.5:  # almost no ink
        return 0.0
    h, w = bw.shape
    centre = (w / 2, h / 2)

    def score(angle: float) -> float:
        m = cv2.getRotationMatrix2D(centre, angle, 1.0)
        rot = cv2.warpAffine(bw, m, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
        prof = rot.sum(axis=1, dtype=np.float64)
        return float(np.var(np.diff(prof)))

    best = max(np.arange(-max_angle, max_angle + 0.01, 1.0), key=score)
    fine = max(np.arange(best - 1.0, best + 1.01, 0.1), key=score)
    return float(round(-fine, 2))  # positive = page rotated counter-clockwise


def _char_height(bw: np.ndarray) -> float:
    """Typical glyph height, counting only components that sit in a text line (have neighbours
    along a horizontal run) and weighting by ink area. Paper texture, speckle and dashed rules
    are isolated or thin, so they cannot outvote real characters. Found on a real phone photo,
    where a plain median reported 4px on readable 12px text and the page was wrongly refused."""
    bw = cv2.medianBlur(bw, 3)
    n, labels, stats, cents = cv2.connectedComponentsWithStats(bw, connectivity=8)
    if n <= 1:
        return 0.0
    hs, ws, area = stats[1:, cv2.CC_STAT_HEIGHT], stats[1:, cv2.CC_STAT_WIDTH], stats[1:, cv2.CC_STAT_AREA]
    img_h, img_w = bw.shape
    keep = (hs >= 3) & (hs < img_h * 0.08) & (ws < img_w * 0.2) & (area >= 6) & (ws / np.maximum(hs, 1) < 4)
    if keep.sum() < 20:
        return float(np.median(hs)) if len(hs) else 0.0
    rough = float(np.median(hs[keep]))
    k = max(3, int(round(max(rough, 6) * 0.8)))
    runs = cv2.dilate(bw, cv2.getStructuringElement(cv2.MORPH_RECT, (k, 1)))
    rn, rlabels, rstats, _ = cv2.connectedComponentsWithStats(runs, connectivity=8)
    run_ok = np.zeros(rn, bool)
    run_ok[1:] = rstats[1:, cv2.CC_STAT_WIDTH] > 2.5 * rstats[1:, cv2.CC_STAT_HEIGHT]
    cy = np.clip(cents[1:, 1].astype(int), 0, img_h - 1)
    cx = np.clip(cents[1:, 0].astype(int), 0, img_w - 1)
    in_line = run_ok[rlabels[cy, cx]]
    sel = keep & in_line
    if sel.sum() < 20:
        sel = keep
    h, a = hs[sel].astype(float), area[sel].astype(float)
    order = np.argsort(h)
    cum = np.cumsum(a[order])
    return float(h[order][np.searchsorted(cum, cum[-1] / 2)])


def assess(img: np.ndarray, source_dpi: float | None = None, native_text: bool = False) -> PageQuality:
    gray = _gray(img)
    h, w = gray.shape
    norm, _ = _normalise(gray)

    bw_full = binarize(gray)
    ink_ratio = float((bw_full > 0).mean())
    char_h = _char_height(bw_full)

    # Sharpness measured only where there is text, so sparse pages are not penalised.
    lap = cv2.Laplacian(norm, cv2.CV_64F)
    local = cv2.dilate(binarize(norm), np.ones((5, 5), np.uint8)) > 0
    sharp = float(lap[local].var()) if local.sum() > 500 else float(lap.var())

    rms = float(norm.std() / 255.0)
    paper = float(np.percentile(norm, 90))
    ink = float(np.percentile(norm, 1)) if ink_ratio < 0.01 else float(np.percentile(norm, max(0.5, min(ink_ratio * 50, 5))))
    text_contrast = max(0.0, (paper - ink) / 255.0)

    med = cv2.medianBlur(norm, 3)
    resid = np.abs(norm.astype(np.int16) - med.astype(np.int16))
    noise = float(np.mean(resid[~local])) if (~local).sum() > 500 else float(np.mean(resid))
    sharp_norm = sharp / ((255.0 * max(text_contrast, 0.01)) ** 2) * 1000.0

    skew = 0.0 if native_text else estimate_skew(gray)
    return PageQuality(width=w, height=h, source_dpi=source_dpi, sharpness=sharp, sharpness_norm=sharp_norm, rms_contrast=rms,
                       text_contrast=text_contrast, ink_ratio=ink_ratio, char_height_px=char_h,
                       skew_deg=skew, noise=noise, native_text=native_text)


# Bucket thresholds; recalibrate against the labelled eval set (docs/EVALUATION.md).
GOOD_SHARPNESS_NORM = 100.0
GOOD_TEXT_CONTRAST = 0.35  # grey fonts on clean digital renders sit around 0.40
GOOD_CHAR_HEIGHT = 12.0


def bucket(q: PageQuality) -> str:
    if q.native_text:
        return "digital"
    good = (q.sharpness_norm >= GOOD_SHARPNESS_NORM and q.text_contrast >= GOOD_TEXT_CONTRAST
            and q.char_height_px >= GOOD_CHAR_HEIGHT and abs(q.skew_deg) < 3)
    return "good_scan" if good else "poor_scan"


def document_bucket(page_buckets: list[str]) -> str:
    order = ["unreadable", "poor_scan", "good_scan", "digital"]
    return min(page_buckets, key=order.index) if page_buckets else "unreadable"


def unreadable_reason(q: PageQuality, blur_floor: float, contrast_floor: float,
                      min_char_h: float, min_ink: float) -> str | None:
    """Plain-language reason when a page cannot be read even after correction, else None."""
    if q.native_text:
        return None
    if q.ink_ratio < min_ink:
        return "The page looks blank. Please check the scan and upload again."
    if q.text_contrast < contrast_floor:
        return "The text is too faded to read, even after enhancement. Please rescan with higher contrast or share the original copy."
    if q.char_height_px and q.char_height_px < min_char_h:
        return "The scan resolution is too low to read the text. Please rescan at 300 DPI."
    if q.sharpness_norm < blur_floor:
        return "The image is too blurry to read reliably. Please rescan or retake the photo in good light."
    return None


def quarter_turn_hint(img) -> int:
    """Cheap sideways-page detector (no model): 90 if text lines run vertically, else 0.
    Ruling lines are removed first so table grids don't vote. Direction (90 vs 270) and 180° are
    left to the triage model; this hint only ensures a sideways scan is never read as-is."""
    gray = _gray(img)
    scale = 1000 / max(gray.shape)
    small = cv2.resize(gray, (max(1, int(gray.shape[1] * scale)), max(1, int(gray.shape[0] * scale))),
                       interpolation=cv2.INTER_AREA)
    bw = binarize(small)
    for k in ((40, 1), (1, 40)):  # strip long horizontal / vertical rules
        lines = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, k))
        bw = cv2.subtract(bw, lines)

    def elongated_mass(kernel) -> float:
        d = cv2.dilate(bw, cv2.getStructuringElement(cv2.MORPH_RECT, kernel))
        n, _, st, _ = cv2.connectedComponentsWithStats(d, connectivity=8)
        if n <= 1:
            return 0.0
        w, h, a = st[1:, cv2.CC_STAT_WIDTH], st[1:, cv2.CC_STAT_HEIGHT], st[1:, cv2.CC_STAT_AREA]
        long_axis, short_axis = (w, h) if kernel[0] > kernel[1] else (h, w)
        return float(a[(long_axis > 3 * short_axis) & (a > 60)].sum())

    horiz, vert = elongated_mass((9, 1)), elongated_mass((1, 9))
    return 90 if vert > 1.6 * max(horiz, 1.0) else 0
