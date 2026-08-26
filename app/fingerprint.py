"""
PDF identity, computed from the file itself rather than from its name.

Three things come out of here and they do different jobs:

  content_sha    the bytes. Identical means we already have this exact file.
  structure      page count and page dimensions. These are what annotation
                 layers are positioned against, so a change here is the one
                 change that slides a singer's markings off their bars.
  edition_key    a hash of what is ON the pages - extracted text where there
                 is a text layer, a visual fingerprint of the rendered pages
                 where there is not. This is what recognises the same edition
                 re-scanned or re-exported under a completely different name.

Nothing in this module looks at the filename. That is deliberate and it is the
whole point of the design: see Device_Sync_Spec.md section 3.1.
"""

import hashlib
import io

import fitz  # PyMuPDF
from PIL import Image

# Below this many characters of extracted text across the whole document we
# treat the PDF as a scan and fingerprint it visually instead. Choral scans
# often carry a few stray characters from a cover sheet or an OCR misfire,
# so the floor is not zero.
TEXT_LAYER_FLOOR = 200

# Pages rendered for the visual fingerprint. Enough to identify an edition
# without rendering a 300-page score.
VISUAL_SAMPLE_PAGES = 8
VISUAL_RENDER_DPI = 36


def content_sha(path: str) -> str:
    """SHA-256 of the file as it stands. Streamed - these run to hundreds of MB."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalise_text(raw: str) -> str:
    """
    Collapse whitespace and case so that a re-export with different line
    breaking still hashes the same. We are asking "is this the same edition",
    not "are these files identical" - content_sha already answers that.
    """
    return " ".join(raw.split()).lower()


def _dhash(image: Image.Image, size: int = 8) -> str:
    """
    Difference hash. Resize to (size+1) x size greyscale, then record whether
    each pixel is brighter than its right-hand neighbour. Robust to the
    resolution and compression changes a re-scan introduces, sensitive to the
    notes actually being different.
    """
    small = image.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = small.load()
    bits = 0
    for y in range(size):
        for x in range(size):
            bits = (bits << 1) | (1 if px[x, y] > px[x + 1, y] else 0)
    return format(bits, "0{}x".format(size * size // 4))


def inspect(path: str) -> dict:
    """
    Open a PDF once and return everything the Librarian needs to identify it.

    Raises ValueError on a file PyMuPDF cannot open. The caller reports that
    to a human rather than guessing - an unreadable score is a thing Tom needs
    to know about, not a thing to skip quietly.
    """
    try:
        doc = fitz.open(path)
    except Exception as exc:  # noqa: BLE001 - any failure here is "unreadable"
        raise ValueError("cannot open as PDF: {}".format(exc)) from exc

    with doc:
        if doc.is_encrypted and not doc.authenticate(""):
            raise ValueError("PDF is password protected")

        page_count = doc.page_count
        if page_count == 0:
            raise ValueError("PDF has no pages")

        # Dimensions rounded to the nearest point. Sub-point drift between
        # exports of the same score is normal and must not read as a change.
        dims = []
        text_parts = []
        for page in doc:
            rect = page.rect
            dims.append([round(rect.width), round(rect.height)])
            text_parts.append(page.get_text("text"))

        joined = _normalise_text("".join(text_parts))
        has_text_layer = len(joined) >= TEXT_LAYER_FLOOR

        if has_text_layer:
            edition_key = hashlib.sha256(joined.encode("utf-8")).hexdigest()
            edition_method = "text"
        else:
            step = max(1, page_count // VISUAL_SAMPLE_PAGES)
            sampled = list(range(0, page_count, step))[:VISUAL_SAMPLE_PAGES]
            hashes = []
            zoom = VISUAL_RENDER_DPI / 72.0
            matrix = fitz.Matrix(zoom, zoom)
            for index in sampled:
                pix = doc[index].get_pixmap(matrix=matrix, colorspace=fitz.csGRAY)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                hashes.append(_dhash(img))
            edition_key = hashlib.sha256("|".join(hashes).encode("ascii")).hexdigest()
            edition_method = "visual"

    return {
        "page_count": page_count,
        "page_dims": dims,
        "edition_key": edition_key,
        "edition_method": edition_method,
        "has_text_layer": has_text_layer,
    }


# How far a page may move before we call it a real size change.
#
# Tuned against actual Ars Nova scans rather than guessed. Hand-scanned choral
# PDFs are not dimensionally uniform - ANS-PureImagination_CS.pdf, scanned once
# and never edited, has pages from 606x792 to 612x773. Exact equality would
# therefore flag every re-scan of it as "your markings will not line up", and a
# warning that fires on healthy files is worse than no warning: people learn to
# click through the one gate that protects their annotations.
#
# The changes that DO matter are much larger. Letter to A4 moves the width 17pt
# and the height 50pt; letter to legal moves the height 144pt. So the threshold
# sits in the gap: 12 points, or 2% of the page for unusually large sheets,
# whichever is greater.
DIM_TOLERANCE_POINTS = 12
DIM_TOLERANCE_RATIO = 0.02


def _axis_moved(before: float, after: float) -> bool:
    allowed = max(DIM_TOLERANCE_POINTS, abs(before) * DIM_TOLERANCE_RATIO)
    return abs(before - after) > allowed


def dim_differences(before: list, after: list) -> list:
    """
    Which pages actually changed size, as [(page_number, before, after)].

    Returned rather than a bare boolean so the human being asked to decide can
    be told "page 7 went from 792pt to 700pt" instead of "structure differs".
    """
    out = []
    for page_no, (was, now) in enumerate(zip(before, after), start=1):
        if _axis_moved(was[0], now[0]) or _axis_moved(was[1], now[1]):
            out.append((page_no, list(was), list(now)))
    return out


def structure_matches(a: dict, b: dict) -> bool:
    """
    True when two files agree on the things annotations are positioned against.

    Page count is exact - a page added or removed shifts every marking after it,
    and there is no tolerance that makes that acceptable. Page size is compared
    with the tolerance above, because scan-to-scan jitter is not a layout change.
    """
    if a.get("page_count") != b.get("page_count"):
        return False
    return not dim_differences(a.get("page_dims") or [], b.get("page_dims") or [])
