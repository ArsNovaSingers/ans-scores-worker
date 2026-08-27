"""
PDF optimisation - build a smaller candidate, prove it is still the same
music, and let a person decide.

WHY THIS EXISTS, in one number: the eleven published Chamber scores total
271 MB, and 271 MB is what lands on fifty personal tablets. Optimised they
come to 33 MB. Measured on the real files 2026-08-27, not estimated - see
claude/portal/PDF_Optimisation_Measured_2026-08-27.md.

WHAT IT DOES NOT DO

It does not publish. Per Jonathan's 2026-08-25 ruling and Device_Sync_Spec
3.5, optimisation is offered and never applied: a candidate is built, measured
and parked, and Tom says yes or no per file. This module builds and measures.
Publishing an approved candidate is librarian.publish_optimised(), which goes
through the ordinary version machinery so the original stays retrievable (R3)
and the published name never changes (R2).

WHY LOSSLESS IS NOT ENOUGH, AND WHY THAT MATTERS

The safe pass - clean, deflate, garbage-collect, no pixel touched, no approval
needed - saves 0.0% on these files. Measured. Every recoverable byte is inside
the images, so every real saving is lossy, so every real saving needs a human.
There is no version of this that is both worthwhile and automatic. That is the
whole reason the approval queue exists rather than a cron job.

WHY THE VERIFICATION IS NOT OPTIONAL

The first measurement of this reported 88.6% saved with every candidate
passing page-count and geometry checks. The candidates were corrupt:
update_stream() re-deflates by default, so a JPEG went in, a zlib stream came
out, and the image dictionary still said /DCTDecode. The files were smaller,
opened without complaint, reported the right page count, reported the right
page size - and rendered blank.

A blank score discovered by a singer at a rehearsal is the exact failure this
whole system exists to prevent. So verification here renders sample pages and
compares ink coverage, and a candidate that loses ink is discarded no matter
how good the number looks. **A compression ratio is not a verification.**
"""

from __future__ import annotations

import io
import os
import tempfile

import fitz
from PIL import Image

# 200 dpi greyscale is the same standard the SOP asks Tom to scan at
# (Operations > SOP Library > Prepare a Sheet-Music PDF for the Singers Hub).
# Matching them is deliberate: a file produced correctly at source should come
# out of this pass essentially untouched, and a file that shrinks 90% here is
# telling you the SOP was not followed.
TARGET_DPI = 200
JPEG_QUALITY = 72

# Leave an image alone unless it is meaningfully above target. Re-encoding
# something already at 205 dpi spends quality to save nothing.
DPI_SLACK = 1.1

# A candidate's ink must land in a BAND around the original's, not merely
# above a floor.
#
# The floor was the obvious design and it was wrong, caught by deliberately
# reintroducing the corruption this gate exists for: a PDF whose image streams
# will not decode does not render blank. MuPDF paints the undecodable region
# dark, and the corrupt Dellaira candidate measured **520% of the original's
# ink**. A floor of 0.80 waves that straight through.
#
# Healthy candidates measure 90-100% on the real set. Anything that renders
# half the ink has lost content; anything that renders more than a fifth extra
# has gained something that is not music.
MIN_INK = 0.80
MAX_INK = 1.20

# Thresholds from Device_Sync_Spec 3.5. A candidate below EITHER bar is
# discarded silently and never reaches Tom - he is not shown a review that
# would save four megabytes. Verified against the real set: sends four files,
# discards seven.
MIN_SAVING_BYTES = 5 * 1024 * 1024
MIN_SAVING_RATIO = 0.40


class Unchanged(Exception):
    """Nothing to do - no image was above target."""


def inspect(path: str) -> dict:
    """What is actually inside this PDF."""
    doc = fitz.open(path)
    images = 0
    image_bytes = 0
    colour = 0
    max_dpi = 0.0
    text_chars = 0

    for pno in range(doc.page_count):
        page = doc[pno]
        text_chars += len(page.get_text().strip())
        width_in = page.rect.width / 72.0 if page.rect.width else 0
        for info in page.get_images(full=True):
            try:
                meta = doc.extract_image(info[0])
            except Exception:
                continue
            images += 1
            image_bytes += len(meta["image"])
            if meta.get("colorspace", 1) and meta["colorspace"] > 1:
                colour += 1
            if width_in:
                max_dpi = max(max_dpi, meta["width"] / width_in)

    out = {
        "pages": doc.page_count,
        "images": images,
        "image_bytes": image_bytes,
        "colour_images": colour,
        "max_dpi": round(max_dpi),
        "text_chars": text_chars,
        "looks_like_scan": images >= doc.page_count and text_chars < 40 * doc.page_count,
    }
    doc.close()
    return out


def build_candidate(path: str, out_path: str) -> dict:
    """
    Downsample every oversized image to greyscale at TARGET_DPI.

    Raises Unchanged when there is nothing above target, so the caller can
    tell "already fine" apart from "we tried and it did not help" - those look
    the same in a size comparison and mean different things to Tom.
    """
    doc = fitz.open(path)
    touched = 0
    skipped = 0

    for pno in range(doc.page_count):
        page = doc[pno]
        width_in = page.rect.width / 72.0 if page.rect.width else 0
        if not width_in:
            continue
        for info in page.get_images(full=True):
            xref = info[0]
            try:
                meta = doc.extract_image(xref)
            except Exception:
                skipped += 1
                continue

            width, height = meta["width"], meta["height"]
            dpi = width / width_in
            if dpi <= TARGET_DPI * DPI_SLACK:
                continue

            scale = TARGET_DPI / dpi
            new_w = max(1, int(width * scale))
            new_h = max(1, int(height * scale))

            try:
                image = Image.open(io.BytesIO(meta["image"]))
                image = image.convert("L").resize((new_w, new_h), Image.LANCZOS)
                buf = io.BytesIO()
                image.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)

                # compress=False is load-bearing, not a tidy default. The
                # default re-deflates these bytes, so a JPEG goes in and a
                # zlib stream comes out while the dictionary below still
                # declares /DCTDecode - a file that is smaller, opens cleanly,
                # and renders nothing.
                doc.update_stream(xref, buf.getvalue(), new=True, compress=False)

                doc.xref_set_key(xref, "Width", str(new_w))
                doc.xref_set_key(xref, "Height", str(new_h))
                doc.xref_set_key(xref, "ColorSpace", "/DeviceGray")
                doc.xref_set_key(xref, "BitsPerComponent", "8")
                doc.xref_set_key(xref, "Filter", "/DCTDecode")
                doc.xref_set_key(xref, "DecodeParms", "null")
                touched += 1
            except Exception:
                skipped += 1

    if touched == 0:
        doc.close()
        raise Unchanged("no image above %d dpi" % TARGET_DPI)

    doc.save(out_path, garbage=4, deflate=True, clean=True)
    doc.close()
    return {"images_reencoded": touched, "images_skipped": skipped}


def verify(original: str, candidate: str) -> dict:
    """
    Is the candidate still the same music?

    Three questions, and the third is the one that catches real corruption.

    Page count and page geometry are R2/3.4 - annotations are positioned per
    page, so a change in either slides every singer's markings off their bars.
    Those are hard gates and they are also cheap.

    Ink coverage is the gate that exists because the other two passed on files
    that rendered wrongly. Sample pages are rasterised from both documents and
    the not-white pixels counted. A healthy candidate scores 90-100%.

    It is a BAND, not a floor - see MIN_INK/MAX_INK. A corrupt candidate does
    not render blank; it renders DARK, and measured 520%.

    MuPDF's own decoder complaints are collected too. When a JPEG stream will
    not decode, MuPDF says so on the way past and then draws something anyway,
    so the warning is available earlier and more directly than any pixel
    measurement - it just has to be asked for rather than left on stderr.
    """
    a = fitz.open(original)
    b = fitz.open(candidate)

    result = {
        "pages_before": a.page_count,
        "pages_after": b.page_count,
        "same_page_count": a.page_count == b.page_count,
        "same_geometry": True,
        "ink_ratio": 0.0,
        "pages_sampled": 0,
        "decoder_complaints": "",
    }

    for i in range(min(a.page_count, b.page_count)):
        ra, rb = a[i].rect, b[i].rect
        if abs(ra.width - rb.width) > 1 or abs(ra.height - rb.height) > 1:
            result["same_geometry"] = False
            break

    matrix = fitz.Matrix(1, 1)  # 72 dpi - enough to see ink, cheap to render
    ratios = []
    step = max(1, a.page_count // 4)

    try:
        fitz.TOOLS.reset_mupdf_warnings()
    except Exception:
        pass

    for i in range(0, a.page_count, step):
        if i >= b.page_count:
            break
        try:
            pa = a[i].get_pixmap(matrix=matrix, colorspace=fitz.csGRAY)
            pb = b[i].get_pixmap(matrix=matrix, colorspace=fitz.csGRAY)
        except Exception:
            # A page that will not render at all is the worst possible answer,
            # and 0.0 is outside the band in the direction that fails.
            ratios.append(0.0)
            continue
        ink_a = sum(1 for v in pa.samples if v < 200)
        ink_b = sum(1 for v in pb.samples if v < 200)
        if ink_a == 0:
            continue  # a blank page in the original proves nothing either way
        ratios.append(ink_b / float(ink_a))

    try:
        result["decoder_complaints"] = (fitz.TOOLS.mupdf_warnings() or "").strip()
    except Exception:
        result["decoder_complaints"] = ""

    a.close()
    b.close()

    result["pages_sampled"] = len(ratios)

    # The WORST page decides, and "worst" means furthest from 1.0 in either
    # direction. Averaging would let one unreadable page hide behind
    # twenty-five good ones, which on a 26-page score is exactly the shape of
    # the failure worth catching.
    if ratios:
        worst = max(ratios, key=lambda r: abs(r - 1.0))
        result["ink_ratio"] = round(worst, 4)
    else:
        result["ink_ratio"] = 0.0

    result["ink_in_band"] = MIN_INK <= result["ink_ratio"] <= MAX_INK
    result["renders_clean"] = result["decoder_complaints"] == ""
    result["ok"] = bool(
        result["same_page_count"]
        and result["same_geometry"]
        and result["ink_in_band"]
        and result["renders_clean"]
    )
    return result


def assess(path: str, out_path: str) -> dict:
    """
    Build a candidate and decide whether it is worth showing anyone.

    Returns a dict describing the outcome. `worth_showing` is the only field
    that decides whether Tom is bothered; everything else is there so a human
    reading the queue can tell WHY.
    """
    before = os.path.getsize(path)
    report = {
        "bytes_before": before,
        "bytes_after": None,
        "saved_bytes": 0,
        "saved_ratio": 0.0,
        "worth_showing": False,
        "outcome": "",
        "source": inspect(path),
    }

    try:
        build = build_candidate(path, out_path)
    except Unchanged as exc:
        report["outcome"] = "already at or below target (%s)" % exc
        return report

    report.update(build)
    after = os.path.getsize(out_path)
    report["bytes_after"] = after
    report["saved_bytes"] = before - after
    report["saved_ratio"] = round((before - after) / float(before), 4) if before else 0.0

    check = verify(path, out_path)
    report["verify"] = check

    if not check["ok"]:
        # Deliberately explicit about which gate failed. "Rejected" with no
        # reason is how a real regression gets waved through next time.
        why = []
        if not check["same_page_count"]:
            why.append("page count changed (%d -> %d)" % (check["pages_before"], check["pages_after"]))
        if not check["same_geometry"]:
            why.append("page geometry changed")
        if not check["ink_in_band"]:
            why.append("renders %.0f%% of the original's ink" % (check["ink_ratio"] * 100))
        if not check["renders_clean"]:
            first = check["decoder_complaints"].splitlines()[0] if check["decoder_complaints"] else ""
            why.append("the renderer complained: %s" % first)
        report["outcome"] = "REJECTED: " + "; ".join(why)
        return report

    if report["saved_bytes"] < MIN_SAVING_BYTES or report["saved_ratio"] < MIN_SAVING_RATIO:
        report["outcome"] = "below the review bar (needs >=40%% and >=5 MB; saved %.0f%%, %.1f MB)" % (
            report["saved_ratio"] * 100,
            report["saved_bytes"] / 1048576.0,
        )
        return report

    report["worth_showing"] = True
    report["outcome"] = "ready for review: %.0f%% smaller, %.1f MB saved" % (
        report["saved_ratio"] * 100,
        report["saved_bytes"] / 1048576.0,
    )
    return report


def scratch_path(name: str) -> str:
    """A temp path for a candidate. Cloud Run's /tmp is memory-backed, so
    these are deleted by the caller as soon as they are uploaded or rejected."""
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in name)
    return os.path.join(tempfile.gettempdir(), "ansopt-" + safe)
