"""
Reading native Google Sheets out of a project's Drive folder (HUB-68, v0.11.0).

The worker only hands back each sheet's cells. Which column is the link, what
a row means and what the Hub shows are all decided in the portal
(ANSP_Sheet_Links), so a change to those rules ships through the plugin
pipeline with a Staging gate and never needs a worker deploy.

A sheet that cannot be read comes back with `error` instead of rows. The caller
must keep whatever it already had: an empty answer means "the sheet is empty",
a failed read means "nobody knows", and a Drive hiccup must not wipe a concert's
videos off the Hub.

Nothing here is stored or published. Links are not music: no staging, no bucket,
no R4.
"""

import csv
import io

from . import drive

SHEET_MIME = "application/vnd.google-apps.spreadsheet"

# A folder holding more than this is not a folder of link sheets. Named, not
# dropped silently: they come back as errors, so their rows are kept.
MAX_SHEETS = 25
MAX_ROWS = 2000


def is_sheet(item: dict) -> bool:
    return item.get("mimeType") == SHEET_MIME


def parse_csv(text: str) -> tuple[list[list[str]], bool]:
    """Rows of cells, and whether the sheet ran past MAX_ROWS and was cut."""
    rows: list[list[str]] = []
    for row in csv.reader(io.StringIO(text, newline="")):
        if len(rows) >= MAX_ROWS:
            return rows, True
        rows.append(row)
    return rows, False


def read(files: list[dict], svc=None) -> list[dict]:
    """One record per native Sheet in a walked folder tree, in name order."""
    found = sorted((f for f in files if is_sheet(f)), key=lambda f: (f.get("name") or "").lower())
    out = []
    for n, f in enumerate(found):
        record = {
            "file_id": f["id"],
            "name": f.get("name") or "",
            "folder": "/".join(f.get("rel_path") or []),
            "modified": f.get("modifiedTime") or "",
        }
        if n >= MAX_SHEETS:
            record["error"] = "more than {} sheets in this folder".format(MAX_SHEETS)
        else:
            try:
                record["rows"], truncated = parse_csv(drive.export_csv(f["id"], svc))
                if truncated:
                    record["truncated"] = True
            except Exception as exc:  # noqa: BLE001 - one bad sheet must not lose the scan
                record["error"] = str(exc)[:300]
        out.append(record)
    return out
