"""
HUB-78 (v0.12.0): Drive push and score-folder auto-publish, against an
in-memory bucket and a fake Drive. Nothing here talks to Google.

    python tests/test_hub78_push.py
"""
import os
import shutil
import sys
import tempfile
import types

WORKER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WORKER)
os.environ["ANS_SCORES_TOKEN"] = "t"

OBJ = {}


class Blob:
    md5_hash = "x"

    def __init__(self, path):
        self.name = path
        self.metadata = None
        self.chunk_size = None

    @property
    def generation(self):
        return OBJ[self.name]["gen"] if self.name in OBJ else 0

    def exists(self):
        return self.name in OBJ

    def download_as_bytes(self):
        if self.name not in OBJ:
            raise Exception("404")
        return OBJ[self.name]["data"]

    def download_to_filename(self, local):
        with open(local, "wb") as fh:
            fh.write(OBJ[self.name]["data"])

    def upload_from_string(self, payload, content_type=None, if_generation_match=None):
        cur = self.generation
        if if_generation_match is not None and if_generation_match != cur:
            raise Exception("412 conditionNotMet")
        OBJ[self.name] = {"data": payload, "gen": cur + 1, "meta": self.metadata or {}}

    def upload_from_filename(self, local, content_type=None, timeout=None):
        with open(local, "rb") as fh:
            OBJ[self.name] = {"data": fh.read(), "gen": self.generation + 1, "meta": self.metadata or {}}

    def reload(self):
        pass

    def patch(self):
        pass


class Bucket:
    def blob(self, path):
        return Blob(path)

    def copy_blob(self, src, _bucket, dst):
        OBJ[dst] = {"data": OBJ[src.name]["data"], "gen": 1, "meta": {}}
        return Blob(dst)


class Client:
    def bucket(self, _name):
        return Bucket()


google = types.ModuleType("google")
cloud = types.ModuleType("google.cloud")
storage = types.ModuleType("google.cloud.storage")
storage.Client = Client
google.cloud = cloud
cloud.storage = storage
sys.modules.update({"google": google, "google.cloud": cloud, "google.cloud.storage": storage})

# ---------------------------------------------------------------- fake Drive
FILES = {}
CALLS = {"watch": [], "stop": [], "list_tokens": []}
CHANGES = []  # what the next changes.list returns
PARENTS = {"fld-scores": ["fld-dl"], "fld-mp3": ["fld-dl"], "fld-dl": ["drive-root"], "drive-root": []}


def _drive_mod():
    m = types.ModuleType("app.drive")
    m.service = lambda: None

    def walk(folder_id, svc):
        out = []
        for fid, f in FILES.items():
            if f["root"] != folder_id:
                continue
            mime = "audio/mpeg" if f["name"].endswith(".mp3") else "application/pdf"
            out.append({"id": fid, "name": f["name"], "mimeType": mime,
                        "rel_path": f["rel_path"], "size": str(os.path.getsize(f["path"])),
                        "md5Checksum": str(os.path.getsize(f["path"])), "modifiedTime": "t"})
        return out

    def download(fid, local, svc):
        shutil.copy(FILES[fid]["path"], local)

    def start_page_token(drive_id, svc=None):
        return "tok-1"

    def watch_changes(drive_id, page_token, channel_id, address, token, expiration_ms, svc=None):
        CALLS["watch"].append({"id": channel_id, "page_token": page_token, "address": address, "token": token})
        return {"resourceId": "res-" + channel_id[-4:], "expiration": str(expiration_ms)}

    def stop_channel(channel_id, resource_id, svc=None):
        CALLS["stop"].append(channel_id)

    def list_changes(drive_id, page_token, svc=None, max_pages=20):
        CALLS["list_tokens"].append(page_token)
        out = list(CHANGES)
        CHANGES.clear()
        return out, "tok-" + str(len(CALLS["list_tokens"]) + 1), True

    def ancestors(parents, svc=None, cache=None, limit=12):
        out, frontier = [], list(parents)
        while frontier:
            cur = frontier.pop(0)
            if cur in out:
                continue
            out.append(cur)
            frontier.extend(PARENTS.get(cur, []))
        return out

    for fn in (walk, download, start_page_token, watch_changes, stop_channel, list_changes, ancestors):
        setattr(m, fn.__name__, fn)
    return m


for name in ("dav", "takes"):
    mod = types.ModuleType("app." + name)
    mod.register = lambda *a, **k: None
    sys.modules["app." + name] = mod
sys.modules["app.drive"] = _drive_mod()

import app  # noqa: E402
app.drive = sys.modules["app.drive"]
from app import main  # noqa: E402

client = main.app.test_client()
H = {"Authorization": "Bearer t"}
ok = 0


def check(cond, what):
    global ok
    if not cond:
        print("FAIL:", what)
        sys.exit(1)
    ok += 1
    print("ok  ", what)


def post(path, body):
    r = client.post(path, json=body, headers=H)
    return r.status_code, r.get_json()


# ---------------------------------------------------------------- score folders
check(main._is_score_folder("Darkness & Light/01 Scores"), "'01 Scores' is a score folder")
check(main._is_score_folder("Darkness & Light/PDFs"), "'PDFs' is a score folder")
check(main._is_score_folder("X/02-Sheet Music"), "'02-Sheet Music' is a score folder")
check(not main._is_score_folder("Darkness & Light/Rehearsal MP3s"), "'Rehearsal MP3s' is not")
check(not main._is_score_folder("Darkness & Light"), "the project root is not")
check(not main._is_score_folder("X/Scores Archive"), "'Scores Archive' is not")

# ---------------------------------------------------------------- auto-publish
import fitz  # noqa: E402

TMP = tempfile.mkdtemp()


def pdf(path, label, pages=2):
    doc = fitz.open()
    for p in range(pages):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), "{} page {} - unique text {}".format(label, p + 1, label * 3), fontsize=14)
    doc.save(path)
    doc.close()


pdf(os.path.join(TMP, "a.pdf"), "ThisDayDawes")
pdf(os.path.join(TMP, "b.pdf"), "LooseScore")
with open(os.path.join(TMP, "c.mp3"), "wb") as fh:
    fh.write(b"ID3" + os.urandom(4000))

FILES["f-score"] = {"root": "DL", "name": "This-Day-Dawes.pdf", "rel_path": ["01 Scores"], "path": os.path.join(TMP, "a.pdf")}
FILES["f-loose"] = {"root": "DL", "name": "Loose-Score.pdf", "rel_path": [], "path": os.path.join(TMP, "b.pdf")}
FILES["f-mp3"] = {"root": "DL", "name": "Home-SOP-click.mp3", "rel_path": ["Rehearsal MP3s"], "path": os.path.join(TMP, "c.mp3")}

body = {"group": "ans", "folder_id": "DL", "project_prefix": "Darkness & Light",
        "auto_publish": {"audio": True, "new_work_projects": []}}
code, res = post("/scan", body)
by = {r["source_name"]: r for r in res["results"]}
check(code == 200, "scan without the new flag answers")
check(by["This-Day-Dawes.pdf"]["outcome"] == "staged", "without the flag a new score still waits (old behaviour)")
check(by["Home-SOP-click.mp3"]["outcome"] == "published", "audio auto-publishes as before")

# A fresh registry, then the same folder with the flag on.
OBJ.clear()
body["auto_publish"]["new_work_score_folders"] = True
code, res = post("/scan", body)
by = {r["source_name"]: r for r in res["results"]}
check(by["This-Day-Dawes.pdf"]["outcome"] == "published", "a NEW PDF in '01 Scores' publishes with the flag")
check(by["This-Day-Dawes.pdf"]["published"]["canonical"], "it has a canonical name")
check(by["Loose-Score.pdf"]["outcome"] == "staged", "a new PDF loose in the project folder still waits")

# Same pages, different file, in the score folder: an edition question, never automatic.
shutil.copy(os.path.join(TMP, "a.pdf"), os.path.join(TMP, "a2.pdf"))
doc = fitz.open(os.path.join(TMP, "a2.pdf"))
doc.set_metadata({"title": "re-exported"})
doc.save(os.path.join(TMP, "a3.pdf"))
doc.close()
FILES["f-score-v2"] = {"root": "DL", "name": "This-Day-Dawes-v2.pdf", "rel_path": ["01 Scores"], "path": os.path.join(TMP, "a3.pdf")}
code, res = post("/scan", body)
by = {r["source_name"]: r for r in res["results"]}
row = by.get("This-Day-Dawes-v2.pdf")
check(row is not None and row["outcome"] in ("staged", "duplicate"), "a second file with the same pages is not auto-published")
if row and row["outcome"] == "staged":
    check(row["proposal"]["decision"] in ("new_edition", "ask_edition"), "it is proposed as an edition: " + row["proposal"]["decision"])

# ---------------------------------------------------------------- watch
code, res = post("/drive/watch", {"drive_id": "D", "address": "http://insecure", "token": "s"})
check(code == 400, "an http address is refused")

code, res = post("/drive/watch", {"drive_id": "D", "address": "https://site/wp-json/x", "token": "secret"})
check(code == 200 and res["renewed"] is True, "first watch opens a channel")
check(CALLS["watch"][-1]["page_token"] == "tok-1" and CALLS["watch"][-1]["token"] == "secret", "it starts from a fresh page token and carries the token")
first_id = res["channel"]["id"]
check(res["channel"]["expires_in_hours"] > 160, "it lasts about 7 days")
check("token" not in str(res["channel"]), "the token is never echoed back")

code, res = post("/drive/watch", {"drive_id": "D", "address": "https://site/wp-json/x", "token": "secret"})
check(res["renewed"] is False and len(CALLS["watch"]) == 1, "a second call within the window does nothing")

code, res = post("/drive/watch", {"drive_id": "D", "address": "https://site/wp-json/x", "token": "secret", "renew_within_hours": 200})
check(res["renewed"] is True and CALLS["stop"] == [first_id], "inside the renewal window: a new channel, then the old one stopped")

code, res = post("/drive/watch", {"drive_id": "D", "address": "https://other/wp-json/x", "token": "secret"})
check(res["renewed"] is True, "a new address always opens a new channel")

# ---------------------------------------------------------------- changes
CHANGES.extend([
    {"fileId": "n1", "removed": False, "file": {"id": "n1", "name": "Some-Children.pdf", "mimeType": "application/pdf", "parents": ["fld-scores"]}},
    {"fileId": "n2", "removed": True},
])
code, res = post("/drive/changes", {"drive_id": "D"})
check(code == 200 and res["count"] == 2, "changes come back")
check(res["changes"][0]["ancestors"] == ["fld-scores", "fld-dl", "drive-root"], "with every folder above the file")
check(res["changes"][1]["removed"] and res["changes"][1]["ancestors"] == [], "a removal has no ancestors")
check(CALLS["list_tokens"] == ["tok-1"], "the first call reads from the stored token")
code, res = post("/drive/changes", {"drive_id": "D"})
check(res["count"] == 0 and CALLS["list_tokens"][-1] == "tok-2", "the token advanced, so nothing is returned twice")

code, res = post("/drive/changes", {"drive_id": "NEW"})
check(res["changes"] == [] and "no page token" in res["note"], "an unknown drive starts counting from now")

r = client.post("/drive/changes", json={"drive_id": "D"})
check(r.status_code == 401, "no token, no changes")

print("\nOK test_hub78_push ({} checks)".format(ok))
