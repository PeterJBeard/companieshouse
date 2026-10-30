"""Text layer extraction, rasterisation and the OCR worker pool.

Tokens are plain dicts so they cache and pickle cheaply:
    {"b": [x0, y0, x1, y1], "t": text, "c": confidence, "l": line_id}
with coordinates in PDF points on the original page.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import threading
import time
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol

import numpy as np
import pymupdf

log = logging.getLogger(__name__)

TRIAGE_DPI = 100  # detection resolution for the heading strip
FULL_DPI = 200
REREAD_DPI = 300
DET_DPI = 110
TRIAGE_TOP_FRAC = 0.35
TRIAGE_BOTTOM_FRAC = 0.08
TRIAGE_MAX_WH = 30.0  # about 60 characters: enough for any heading

MIN_TEXT_LAYER_WORDS = 15


@dataclass
class OCRLine:
    box: tuple[float, float, float, float]  # pixels in the recognition image
    text: str
    conf: float
    words: list[tuple[float, float, str, float]]  # (x0, x1, text, conf) in pixels


class OCREngine(Protocol):
    name: str
    model_version: str

    def recognize(self, rec_img: np.ndarray, det_img: np.ndarray, max_wh_ratio: float | None = None, stop: Callable[[list[OCRLine]], bool] | None = None) -> list[OCRLine]:
        """rec_img and det_img are the same region rendered at different resolutions (grey, uint8).

        max_wh_ratio truncates long lines to their left part, and stop() is checked after each small batch
        of lines read top to bottom; both are used by triage, which only needs the first heading."""


def _to_rgb(gray: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(np.repeat(gray[:, :, None], 3, axis=2))


class RapidOCREngine:
    """PP-OCRv4 detection and recognition on ONNX Runtime (CPU).

    Detection runs on a low-resolution render; recognition runs on crops from the
    full-resolution render. The angle classifier is skipped: filed accounts are upright.
    """

    name = "rapidocr"

    def __init__(self, threads: int = 1, device: str = "cpu"):
        from rapidocr_onnxruntime import RapidOCR
        import rapidocr_onnxruntime

        kw: dict[str, Any] = {"intra_op_num_threads": threads, "inter_op_num_threads": 1}
        if device == "cuda":
            kw.update({"det_use_cuda": True, "rec_use_cuda": True})
        self._engine = RapidOCR(**kw)
        from importlib.metadata import PackageNotFoundError, version

        try:
            ver = version("rapidocr-onnxruntime") + " onnxruntime " + version("onnxruntime")
        except PackageNotFoundError:
            ver = getattr(rapidocr_onnxruntime, "__version__", "unknown")
        self.model_version = f"rapidocr_onnxruntime {ver} / PP-OCRv4 det+rec (ch)"

    def recognize(self, rec_img: np.ndarray, det_img: np.ndarray, max_wh_ratio: float | None = None, stop: Callable[[list[OCRLine]], bool] | None = None) -> list[OCRLine]:
        eng = self._engine
        # detect at the size we rendered; the default "min side >= 736" rule upscales thin strips enormously
        eng.text_det.limit_type = "max"
        eng.text_det.limit_side_len = max(det_img.shape[:2])
        boxes, _ = eng.text_det(_to_rgb(det_img))
        if boxes is None or len(boxes) == 0:
            return []
        sy = rec_img.shape[0] / det_img.shape[0]
        sx = rec_img.shape[1] / det_img.shape[1]
        H, W = rec_img.shape[:2]
        rects, crops = [], []
        rgb = _to_rgb(rec_img)
        for q in boxes:
            q = np.asarray(q, dtype=np.float64)
            x0, y0 = q[:, 0].min() * sx, q[:, 1].min() * sy
            x1, y1 = q[:, 0].max() * sx, q[:, 1].max() * sy
            x0, y0 = max(0, int(x0)), max(0, int(y0))
            x1, y1 = min(W, int(np.ceil(x1))), min(H, int(np.ceil(y1)))
            if x1 - x0 < 3 or y1 - y0 < 3:
                continue
            if max_wh_ratio and (x1 - x0) > (y1 - y0) * max_wh_ratio:
                x1 = x0 + int((y1 - y0) * max_wh_ratio)
            rects.append((x0, y0, x1, y1))
            crops.append(rgb[y0:y1, x0:x1])
        if not crops:
            return []
        order = sorted(range(len(rects)), key=lambda i: (rects[i][1], rects[i][0]))
        rects = [rects[i] for i in order]
        crops = [crops[i] for i in order]
        chunk = 4 if stop else len(crops)
        out: list[OCRLine] = []
        for start in range(0, len(crops), chunk):
            rec, _ = eng.text_rec(crops[start : start + chunk], True)
            for (x0, y0, x1, y1), r in zip(rects[start : start + chunk], rec):
                text, conf = r[0], float(r[1])
                if not text.strip() or conf < 0.5:
                    continue
                words = _words_from_ctc(text, r[2] if len(r) > 2 else None, x0, x1)
                out.append(OCRLine((x0, y0, x1, y1), text, conf, words))
            if stop and stop(out):
                break
        return out


def _words_from_ctc(text: str, info: Any, x0: float, x1: float) -> list[tuple[float, float, str, float]]:
    """Split a recognised line into words using CTC column positions of each character."""
    width = x1 - x0
    chars: list[str] = []
    cols: list[int] = []
    confs: list[float] = []
    col_num = 0
    if info:
        try:
            col_num, word_list, word_col_list, _state, conf_list = info
            for wl, cl in zip(word_list, word_col_list):
                chars.extend(wl)
                cols.extend(cl)
            confs = list(conf_list)
        except (ValueError, TypeError):
            chars = []
    if not chars or len(chars) != len(cols) or "".join(chars) != text or not col_num:
        # proportional fallback
        n = max(1, len(text))
        chars = list(text)
        cols = [int((i + 0.5) * 100 / n) for i in range(n)]
        col_num = 100
        confs = []
    cw = width / col_num
    words = []
    cur: list[int] = []

    def flush():
        if not cur:
            return
        s = "".join(chars[i] for i in cur)
        a = x0 + max(0.0, cols[cur[0]] - 0.6) * cw
        b = x0 + min(col_num, cols[cur[-1]] + 1.6) * cw
        if cur[0] == first_nonspace:
            a = x0
        if cur[-1] == last_nonspace:
            b = x1
        c = min((confs[i] for i in cur if i < len(confs)), default=1.0)
        words.append((a, b, s, float(c)))

    nonspace = [i for i, ch in enumerate(chars) if not ch.isspace()]
    if not nonspace:
        return []
    first_nonspace, last_nonspace = nonspace[0], nonspace[-1]
    for i, ch in enumerate(chars):
        if ch.isspace():
            flush()
            cur = []
        else:
            cur.append(i)
    flush()
    return words


class PaddleOCREngine:
    """PaddleOCR on CUDA. Drop-in for production GPU hosts; requires `paddleocr` and `paddlepaddle-gpu`."""

    name = "paddleocr"

    def __init__(self, threads: int = 1, device: str = "cuda"):
        from paddleocr import PaddleOCR  # type: ignore
        import paddleocr  # type: ignore

        self._ocr = PaddleOCR(use_angle_cls=False, lang="en", use_gpu=(device == "cuda"), show_log=False)
        self.model_version = f"paddleocr {getattr(paddleocr, '__version__', 'unknown')}"

    def recognize(self, rec_img: np.ndarray, det_img: np.ndarray, max_wh_ratio: float | None = None, stop: Callable[[list[OCRLine]], bool] | None = None) -> list[OCRLine]:
        res = self._ocr.ocr(_to_rgb(rec_img), cls=False) or []
        out = []
        for page in res:
            for quad, (text, conf) in page or []:
                q = np.asarray(quad)
                x0, y0, x1, y1 = q[:, 0].min(), q[:, 1].min(), q[:, 0].max(), q[:, 1].max()
                out.append(OCRLine((x0, y0, x1, y1), text, float(conf), _words_from_ctc(text, None, x0, x1)))
        return out


ENGINES = {"rapidocr": RapidOCREngine, "paddleocr": PaddleOCREngine}


def make_engine(name: str = "rapidocr", device: str = "cpu", threads: int = 1) -> OCREngine:
    if name not in ENGINES:
        raise ValueError(f"unknown OCR engine {name!r}; choose from {sorted(ENGINES)}")
    return ENGINES[name](threads=threads, device=device)


# --------------------------------------------------------------------------- rendering


def render_gray(page: pymupdf.Page, dpi: int, clip: pymupdf.Rect | None = None) -> np.ndarray:
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, clip=clip, alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w).copy()


DASH_DPI = 300


def flat_dash_in(page: pymupdf.Page, rect: list[float], dpi: int = DASH_DPI) -> list[float] | None:
    """Bbox (PDF points) of a lone dash inside `rect`, or None.

    OCR detectors often skip a bare '—' altogether, which loses nil comparatives. Accept only a
    single short, flat, solid bar that does not touch the sides of the window (rules do).
    """
    import cv2

    clip = pymupdf.Rect(rect) & page.rect
    if clip.is_empty:
        return None
    g = render_gray(page, dpi, clip)
    ink = (g < 140).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    s = dpi / 72
    comps = [stats[i] for i in range(1, n) if stats[i][cv2.CC_STAT_WIDTH] >= s or stats[i][cv2.CC_STAT_HEIGHT] >= s]
    if len(comps) != 1:
        return None
    x, y, w, h, area = (int(v) for v in comps[0])
    wpt, hpt = w / s, h / s
    if not (2.5 <= wpt <= 14 and hpt <= 2.2 and wpt >= 2.5 * hpt and area >= 0.5 * w * h):
        return None
    if x <= 0 or x + w >= ink.shape[1]:
        return None
    return [round(clip.x0 + x / s, 2), round(clip.y0 + y / s, 2), round(clip.x0 + (x + w) / s, 2), round(clip.y0 + (y + h) / s, 2)]


def render_png(page: pymupdf.Page, dpi: int, clip: pymupdf.Rect | None = None) -> bytes:
    return page.get_pixmap(dpi=dpi, clip=clip, alpha=False).tobytes("png")


def _snap_to_ink(ink: np.ndarray, x0: float, y0: float, x1: float, y1: float, neighbours: tuple[float, float]) -> tuple[float, float, float, float] | None:
    """Tighten a word box (pixels) to the dark pixels it covers.

    `neighbours` are the right edge of the previous word and left edge of the next one, so the
    search window never swallows an adjacent word.
    """
    H, W = ink.shape
    pad = max(2.0, (y1 - y0) * 0.35)
    lo = int(max(0, x0 - pad, neighbours[0]))
    hi = int(min(W, x1 + pad, neighbours[1]))
    ya, yb = int(max(0, y0)), int(min(H, y1))
    if hi - lo < 2 or yb - ya < 2:
        return None
    region = ink[ya:yb, lo:hi]
    colsum = region.sum(axis=0)
    inkcols = np.nonzero(colsum > 0)[0]
    if inkcols.size == 0:
        return None
    # group ink columns into runs separated by gaps wider than a space
    gap = max(3, int((y1 - y0) * 0.22))
    runs = []
    start = prev = int(inkcols[0])
    for c in inkcols[1:]:
        c = int(c)
        if c - prev > gap:
            runs.append((start, prev))
            start = c
        prev = c
    runs.append((start, prev))
    ex0, ex1 = x0 - lo, x1 - lo
    keep = [r for r in runs if min(r[1] + 1, ex1) - max(r[0], ex0) > 0.4 * (r[1] + 1 - r[0])]
    if not keep:
        return None
    a, b = keep[0][0], keep[-1][1] + 1
    rows = np.nonzero(ink[ya:yb, lo + a : lo + b].sum(axis=1) > 0)[0]
    if rows.size == 0:
        return None
    return (lo + a, ya + int(rows[0]), lo + b, ya + int(rows[-1]) + 1)


def lines_to_tokens(lines: list[OCRLine], rec_img: np.ndarray, dpi: int, origin: tuple[float, float], line_offset: int = 0) -> list[dict]:
    """Convert OCR lines (pixels) into word tokens in PDF points, snapped to ink."""
    scale = 72.0 / dpi
    ox, oy = origin
    ink = rec_img < 140
    tokens = []
    for li, ln in enumerate(lines):
        lx0, ly0, lx1, ly1 = ln.box
        ws = ln.words
        for wi, (a, b, text, conf) in enumerate(ws):
            left_n = ws[wi - 1][1] if wi > 0 else -1e9
            right_n = ws[wi + 1][0] if wi + 1 < len(ws) else 1e9
            # neighbours limit: midpoint between this word and its neighbours
            nb = ((left_n + a) / 2 if wi > 0 else lx0 - 4, (b + right_n) / 2 if wi + 1 < len(ws) else lx1 + 4)
            snapped = _snap_to_ink(ink, a, ly0, b, ly1, nb)
            x0, y0, x1, y1 = snapped if snapped else (a, ly0, b, ly1)
            tokens.append(
                {
                    "b": [round(ox + x0 * scale, 2), round(oy + y0 * scale, 2), round(ox + x1 * scale, 2), round(oy + y1 * scale, 2)],
                    "t": text,
                    "c": round(min(conf, ln.conf) if conf < 1.0 else ln.conf, 4),
                    "l": line_offset + li,
                }
            )
    return tokens


# --------------------------------------------------------------------------- text layer


def text_layer_tokens(page: pymupdf.Page) -> list[dict] | None:
    """Return word tokens from the PDF text layer, or None if the page has no usable text."""
    words = page.get_text("words")
    if len(words) < MIN_TEXT_LAYER_WORDS:
        return None
    alnum = sum(sum(ch.isalnum() for ch in w[4]) for w in words)
    total = sum(len(w[4]) for w in words) or 1
    if alnum / total < 0.5:
        return None
    line_ids: dict[tuple[int, int], int] = {}
    out = []
    for x0, y0, x1, y1, text, block, line, _wno in words:
        lid = line_ids.setdefault((block, line), len(line_ids))
        out.append({"b": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)], "t": text, "c": 1.0, "l": lid})
    return out


def page_has_text_layer(page: pymupdf.Page) -> bool:
    return text_layer_tokens(page) is not None


# --------------------------------------------------------------------------- worker side

_worker_engine: OCREngine | None = None
_worker_docs: dict[str, pymupdf.Document] = {}


def _init_worker(engine_name: str, device: str, threads: int) -> None:
    global _worker_engine
    # under the stdio transport stdout carries the MCP protocol; anything a model library prints must not reach it
    os.dup2(2, 1)
    os.environ.setdefault("OMP_NUM_THREADS", str(threads))
    logging.getLogger("RapidOCR").setLevel(logging.ERROR)
    _worker_engine = make_engine(engine_name, device, threads)


def _doc(path: str) -> pymupdf.Document:
    d = _worker_docs.get(path)
    if d is None:
        if len(_worker_docs) > 8:
            for k in list(_worker_docs)[:4]:
                _worker_docs.pop(k).close()
        d = _worker_docs[path] = pymupdf.open(path)
    return d


def ocr_region(page: pymupdf.Page, engine: OCREngine, dpi: int, clip: pymupdf.Rect | None = None, line_offset: int = 0, max_wh_ratio: float | None = None, stop: Callable[[list[OCRLine]], bool] | None = None, det_dpi: int = DET_DPI) -> list[dict]:
    """OCR a page region: detect text lines at det_dpi, recognise them from a dpi render."""
    rect = clip or page.rect
    rec_img = render_gray(page, dpi, rect)
    det_img = rec_img if dpi <= det_dpi else render_gray(page, det_dpi, rect)
    lines = engine.recognize(rec_img, det_img, max_wh_ratio, stop)
    return lines_to_tokens(lines, rec_img, dpi, (rect.x0, rect.y0), line_offset)


def _task_ocr_page(path: str, index: int, dpi: int) -> dict:
    t = time.perf_counter()
    page = _doc(path)[index]
    toks = ocr_region(page, _worker_engine, dpi)  # type: ignore[arg-type]
    return {"tokens": toks, "dpi": dpi, "ms": round((time.perf_counter() - t) * 1000, 1), "engine": _worker_engine.model_version}  # type: ignore[union-attr]


def short_ink_tokens(page: pymupdf.Page, engine: OCREngine, clip: pymupdf.Rect, dpi: int, line_offset: int = 0) -> list[dict]:
    """Recognise short isolated ink blobs (page numbers) without the text detector.

    The detector misses a lone "13" in a thin strip; a projection profile finds it reliably.
    """
    img = render_gray(page, dpi, clip)
    ink = img < 140
    rows = np.nonzero(ink.sum(axis=1))[0]
    if rows.size == 0:
        return []
    bands: list[tuple[int, int]] = []
    start = prev = int(rows[0])
    for r in rows[1:]:
        r = int(r)
        if r - prev > 4:
            bands.append((start, prev + 1))
            start = r
        prev = r
    bands.append((start, prev + 1))
    lines: list[OCRLine] = []
    for y0, y1 in bands:
        h = y1 - y0
        if h < 8 or h > 80:
            continue
        cols = np.nonzero(ink[y0:y1].sum(axis=0))[0]
        segs: list[tuple[int, int]] = []
        a = b = int(cols[0])
        for c in cols[1:]:
            c = int(c)
            if c - b > max(14, h):
                segs.append((a, b + 1))
                a = c
            b = c
        segs.append((a, b + 1))
        for x0, x1 in segs:
            if x1 - x0 > h * 6:
                continue
            pad = 6
            ya, yb = max(0, y0 - pad), min(img.shape[0], y1 + pad)
            xa, xb = max(0, x0 - pad), min(img.shape[1], x1 + pad)
            crop = _to_rgb(img[ya:yb, xa:xb])
            if isinstance(engine, RapidOCREngine):
                rec, _ = engine._engine.text_rec([crop], False)
                text, conf = rec[0][0], float(rec[0][1])
            else:
                got = engine.recognize(img[ya:yb, xa:xb], img[ya:yb, xa:xb])
                if not got:
                    continue
                text, conf = got[0].text, got[0].conf
            if text.strip() and conf >= 0.5:
                lines.append(OCRLine((x0, y0, x1, y1), text.strip(), conf, [(x0, x1, text.strip(), conf)]))
    return lines_to_tokens(lines, img, dpi, (clip.x0, clip.y0), line_offset)


def _heading_found(lines: list[OCRLine]) -> bool:
    from .triage import classify_line

    rows: list[tuple[float, list[OCRLine]]] = []
    for ln in sorted(lines, key=lambda l: l.box[1]):
        if rows and abs(rows[-1][0] - ln.box[1]) < 6:
            rows[-1][1].append(ln)
        else:
            rows.append((ln.box[1], [ln]))
    return any(classify_line(" ".join(l.text for l in sorted(g, key=lambda l: l.box[0]))) for _, g in rows)


def _task_triage_page(path: str, index: int) -> dict:
    t = time.perf_counter()
    page = _doc(path)[index]
    r = page.rect
    top = pymupdf.Rect(r.x0, r.y0, r.x1, r.y0 + r.height * TRIAGE_TOP_FRAC)
    bottom = pymupdf.Rect(r.x0, r.y1 - r.height * TRIAGE_BOTTOM_FRAC, r.x1, r.y1)
    toks = ocr_region(page, _worker_engine, FULL_DPI, top, max_wh_ratio=TRIAGE_MAX_WH, stop=_heading_found, det_dpi=TRIAGE_DPI)  # type: ignore[arg-type]
    btoks = short_ink_tokens(page, _worker_engine, bottom, FULL_DPI, line_offset=10000)  # type: ignore[arg-type]
    return {"tokens": toks, "bottom_tokens": btoks, "dpi": FULL_DPI, "det_dpi": TRIAGE_DPI, "ms": round((time.perf_counter() - t) * 1000, 1)}


class OCRPool:
    """Persistent process pool; each worker loads one OCR engine once at start-up."""

    def __init__(self, workers: int, engine: str = "rapidocr", device: str = "cpu"):
        self.workers = workers
        self.engine = engine
        self.device = device
        ctx = mp.get_context("forkserver" if "forkserver" in mp.get_all_start_methods() else "spawn")
        threads = 1 if device == "cpu" else 1
        self._ex = ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=_init_worker, initargs=(engine, device, threads))
        self.model_version: str | None = None

    def warm(self) -> None:
        list(self._ex.map(_noop, range(self.workers)))

    def ocr_pages(self, path: str, indices: Iterable[int], dpi: int) -> dict[int, Future]:
        return {i: self._ex.submit(_task_ocr_page, path, i, dpi) for i in indices}

    def triage_pages(self, path: str, indices: Iterable[int]) -> dict[int, Future]:
        return {i: self._ex.submit(_task_triage_page, path, i) for i in indices}

    def shutdown(self) -> None:
        self._ex.shutdown(cancel_futures=True)


def _noop(_: int) -> int:
    return os.getpid()


_pool: OCRPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> OCRPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            from .config import settings

            s = settings()
            _pool = OCRPool(s.ocr_workers, s.ocr_engine, s.ocr_device)
        return _pool
