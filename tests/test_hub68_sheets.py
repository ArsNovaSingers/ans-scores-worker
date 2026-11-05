"""
HUB-68 (v0.11.0): /scan hands back native Sheets only when asked, a sheet that
cannot be read comes back as an error and not as "no rows", and the answer to a
caller that does not ask is unchanged. Nothing here talks to Google.
"""
import os
import sys

WORKER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WORKER)
os.environ["ANS_SCORES_TOKEN"] = "t"

from app import drive, librarian, main, sheets, store  # noqa: E402

GOOD = "good-sheet"
BAD = "bad-sheet"
SHEET = sheets.SHEET_MIME
FILES = [
    {"id": GOOD, "name": "Darkness & Light - Videos", "mimeType": SHEET, "rel_path": ["Links"], "modifiedTime": "m"},
    {"id": BAD, "name": "Broken", "mimeType": SHEET, "rel_path": []},
    {"id": "doc", "name": "A doc", "mimeType": "application/vnd.google-apps.document", "rel_path": []},
]


def fake_export(file_id, svc=None):
    if file_id == BAD:
        raise RuntimeError("exportSizeLimitExceeded")
    return 'Title,Link,Piece\r\n"O Jesulein süss, solo",https://youtu.be/x?t=13,"A, B"\r\n'


drive.service = lambda: object()
drive.walk = lambda folder_id, svc=None: list(FILES)
drive.export_csv = fake_export
store.read_json = lambda path, default=None: (default if default is not None else {}, 0)
store.write_json = lambda *a, **k: None
librarian.load_registry = lambda: ({"works": {}}, 0)
librarian.load_staging = lambda: ({"items": {}}, 0)
librarian.mark_missing_at_source = lambda *a, **k: []

client = main.app.test_client()
AUTH = {"Authorization": "Bearer t"}


def scan(**extra):
    res = client.post("/scan", json={"group": "claude-test", "folder_id": "f", **extra}, headers=AUTH)
    assert res.status_code == 200, res.data
    return res.get_json()


plain = scan()
assert "sheets" not in plain, "a caller that does not ask gets the old answer"
assert sorted(i["name"] for i in plain["ignored"]) == ["A doc", "Broken", "Darkness & Light - Videos"]

asked = scan(include_sheets=True)
by_id = {s["file_id"]: s for s in asked["sheets"]}
assert set(by_id) == {GOOD, BAD}
assert by_id[GOOD]["rows"] == [["Title", "Link", "Piece"], ["O Jesulein süss, solo", "https://youtu.be/x?t=13", "A, B"]]
assert by_id[GOOD]["folder"] == "Links"
assert "rows" not in by_id[BAD] and "exportSizeLimitExceeded" in by_id[BAD]["error"]
assert [i["name"] for i in asked["ignored"]] == ["A doc"], "a sheet that was read is not ignored"

many = [{"id": "s%d" % n, "name": "s%03d" % n, "mimeType": SHEET} for n in range(sheets.MAX_SHEETS + 1)]
read = sheets.read(many)
assert "error" in read[-1] and "rows" in read[0], "past the cap is an error, so rows are kept"

rows, cut = sheets.parse_csv("a\n" * (sheets.MAX_ROWS + 5))
assert cut and len(rows) == sheets.MAX_ROWS

print("OK test_hub68_sheets")
