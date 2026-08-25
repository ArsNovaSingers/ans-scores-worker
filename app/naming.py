"""
Filename handling - for PROPOSING names and for MEASURING similarity.

Read this alongside Device_Sync_Spec.md section 3.1. Nothing here decides what
a file is. Names propose; bytes and structure decide. The similarity score
below exists so the Librarian can put a sensible question in front of Tom
("is this a new edition of X?"), never so it can answer that question itself.
"""

import re
import unicodedata

# Trailing date stamps appended on export: -0811, -08-11, -20260811, and the
# same separated by spaces, because not every upload comes from Tom.
_DATE_TAIL = re.compile(r"[-_\s](?:\d{4}|\d{2}[-_\s]?\d{2}|\d{8})$")
_PREFIX = re.compile(r"^ans[-_\s]+", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def strip_tags(stem: str) -> tuple[str, list[str]]:
    """
    Split 'ANS-Parry-ThereIsAnOldBelief_CS' into its name part and its tags.

    Underscore is reserved for tags by the house convention. A file with no
    underscore simply has no tags, which is not an error.
    """
    parts = stem.split("_")
    return parts[0], [p for p in parts[1:] if p]


def normalise(stem: str) -> str:
    """
    Reduce a filename stem to the thing worth comparing: lowercase, no ANS-
    prefix, no tags, no trailing export date, no punctuation.

        ANS-Margutti-Rivers-0811   ->  marguttirivers
        ANS-Margutti-Rivrs-0818    ->  marguttirivrs

    Those two are one edit apart, which is the entire point.
    """
    name, _tags = strip_tags(stem)
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = _PREFIX.sub("", name)
    while True:
        stripped = _DATE_TAIL.sub("", name)
        if stripped == name:
            break
        name = stripped
    return _NON_ALNUM.sub("", name.lower())


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (ca != cb),
                )
            )
        previous = current
    return previous[-1]


def similarity(a: str, b: str) -> float:
    """
    0.0 to 1.0 over normalised stems. A single dropped letter in a
    twenty-character name scores about 0.95; two unrelated pieces by the same
    composer score far lower because the title dominates the string.
    """
    a, b = normalise(a), normalise(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    longest = max(len(a), len(b))
    return 1.0 - (_levenshtein(a, b) / longest)


def propose_canonical(stem: str) -> str:
    """
    The name we suggest publishing under, given whatever was uploaded.

    Conservative on purpose - it strips the export date and normalises the
    prefix, and otherwise leaves Tom's words alone. It is a suggestion in a
    text box, not a rewrite.
    """
    name, tags = strip_tags(stem)
    while True:
        stripped = _DATE_TAIL.sub("", name)
        if stripped == name:
            break
        name = stripped
    name = _PREFIX.sub("", name).strip("-_ ")
    canonical = "ANS-{}".format(name) if name else "ANS-Untitled"
    if tags:
        canonical = "{}_{}".format(canonical, "_".join(tags))
    return canonical
