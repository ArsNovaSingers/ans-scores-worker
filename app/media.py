"""
What kind of file is this, as far as the mirror is concerned.

Until v0.6.0 the worker published PDFs and nothing else: /scan threw away every
other file it walked, silently. Rivers & Streams showed what that costs. Tom's
rehearsal doc links 29 recordings across three folders (click tracks per voice
part, the Tedesco movements, a rehearsal take) and not one of them reached a
singer through the Hub, because the only thing that could carry them never
looked at them.

TWO MEDIA, TWO IDENTITY RULES. A score carries fifty singers' annotations
positioned per page, which is why the Librarian decides a PDF's identity from
its bytes and its page structure and never from its name. A recording carries
no annotation layer at all. What identifies it is where it came from:

  * the same bytes           -> duplicate, nothing to do
  * the same Drive file id   -> a new version of that recording (Tom replaced
                                the file in place)
  * anything else            -> a new recording

Name similarity is deliberately NOT used for audio. `Mvt6-SOPclick` and
`Mvt1-SOPclick` are one character apart and are different recordings; the
0.82 threshold the PDF path uses to *ask* would propose one as an edition of
the other, over and over, for every part set Tom ever uploads.

The published name of a recording is Tom's own filename stem, verbatim apart
from characters that cannot live in an object path. The PDF path strips a
trailing date because a re-export date is noise on a score; on a recording
(`Prolog-ANS-0829`) the date says which rehearsal it is, so it stays.
"""

import re

PDF = "pdf"
AUDIO = "audio"

# Extension -> content type. Extension rather than Drive's mimeType alone,
# because Drive reports some uploads as application/octet-stream.
AUDIO_TYPES = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "wav": "audio/wav",
    "aif": "audio/aiff",
    "aiff": "audio/aiff",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
}

# When the same recording sits in a folder twice, once compressed and once
# not, publish the compressed one. A rehearsal WAV is ten times the size and
# no singer needs it on a phone.
LOSSLESS = {"wav", "aif", "aiff", "flac"}

CONTENT_TYPES = {PDF: "application/pdf", **AUDIO_TYPES}

_UNSAFE = re.compile(r"[\\/\x00-\x1f\x7f]+")


def ext_of(name: str) -> str:
    """Lowercase extension without the dot, '' when there is none."""
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[1].strip().lower()


def stem_of(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def classify(item: dict) -> str | None:
    """
    PDF, AUDIO, or None for anything the mirror does not carry.

    None covers Google Docs, images, and the junk a DAW leaves behind
    (`.asd` analysis files), which must never reach a singer.
    """
    mime = str(item.get("mimeType") or "")
    ext = ext_of(str(item.get("name") or ""))
    if mime == "application/pdf" or ext == "pdf":
        return PDF
    if ext in AUDIO_TYPES:
        return AUDIO
    if mime.startswith("audio/") and ext:
        # An audio type Drive recognises under an extension we have not listed.
        # Refuse rather than guess a content type for it.
        return None
    return None


def content_type(ext: str) -> str:
    return CONTENT_TYPES.get((ext or PDF).lower(), "application/octet-stream")


def audio_canonical(stem: str) -> str:
    """Tom's filename, made safe to be an object name and nothing more."""
    name = _UNSAFE.sub("-", stem).strip(" .-_")
    return name or "Untitled recording"


def prefer_compressed(files: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Drop a lossless file when a compressed file with the same stem sits in the
    same folder. Returns (kept, skipped).
    """
    compressed = {
        (tuple(f.get("rel_path", [])), stem_of(f["name"]).lower())
        for f in files
        if ext_of(f["name"]) not in LOSSLESS
    }
    kept, skipped = [], []
    for f in files:
        key = (tuple(f.get("rel_path", [])), stem_of(f["name"]).lower())
        if ext_of(f["name"]) in LOSSLESS and key in compressed:
            skipped.append(f)
        else:
            kept.append(f)
    return kept, skipped


def inspected_for_audio(ext: str, size: int, content_sha: str) -> dict:
    """
    The `inspected` block a staged recording carries.

    Shaped like the PDF one so every consumer that reads page_count or
    edition_key keeps working: a recording has no pages, and its edition is
    its bytes.
    """
    return {
        "media": AUDIO,
        "ext": ext,
        "mime": content_type(ext),
        "size": int(size or 0),
        "page_count": 0,
        "page_dims": [],
        "edition_key": "sha256:" + content_sha,
        "edition_method": "bytes",
    }
