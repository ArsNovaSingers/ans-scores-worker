"""
Local test of ans-scores-worker v0.9.0 against an in-memory bucket and a fake
Drive. Nothing here talks to Google. Run with the scratch venv's python.
"""
import io
import json
import os
import random
import shutil
import sys
import tempfile
import types

WORKER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WORKER)
os.environ["ANS_SCORES_TOKEN"] = "t"

# ---------------------------------------------------------------- fake GCS
OBJ = {}          # path -> {"data": bytes, "gen": int, "meta": dict}
FAIL_UPLOAD = set()


class Blob:
    def __init__(self, path):
        self.name = path
        self.metadata = None
        self.chunk_size = None

    @property
    def generation(self):
        return OBJ[self.name]["gen"] if self.name in OBJ else 0

    md5_hash = "x"

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
        cur = OBJ[self.name]["gen"] if self.name in OBJ else 0
        if if_generation_match is not None and if_generation_match != cur:
            raise Exception("412 conditionNotMet")
        OBJ[self.name] = {"data": payload, "gen": cur + 1, "meta": self.metadata or {}}

    def upload_from_filename(self, local, content_type=None, timeout=None):
        assert self.chunk_size == 8 * 1024 * 1024, "chunked upload expected"
        assert timeout == 300
        if any(self.name.endswith(x) for x in FAIL_UPLOAD):
            raise TimeoutError("The write operation timed out")
        with open(local, "rb") as fh:
            data = fh.read()
        cur = OBJ[self.name]["gen"] if self.name in OBJ else 0
        OBJ[self.name] = {"data": data, "gen": cur + 1, "meta": self.metadata or {}}

    def reload(self):
        pass

    def patch(self):
        OBJ[self.name]["meta"] = self.metadata or {}


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
FILES = {}  # id -> {"name", "rel_path", "path"}


def _drive_mod():
    m = types.ModuleType("app.drive")
    m.service = lambda: None

    def walk(folder_id, svc):
        out = []
        for fid, f in FILES.items():
            if f["root"] != folder_id:
                continue
            out.append({"id": fid, "name": f["name"], "mimeType": "application/pdf",
                        "rel_path": f["rel_path"], "size": str(os.path.getsize(f["path"])),
                        "md5Checksum": str(os.path.getmtime(f["path"])), "modifiedTime": "t"})
        return out

    def download(fid, local, svc):
        shutil.copy(FILES[fid]["path"], local)

    m.walk = walk
    m.download = download
    return m


for name in ("dav", "takes"):
    mod = types.ModuleType("app." + name)
    mod.register = lambda *a, **k: None
    sys.modules["app." + name] = mod
sys.modules["app.drive"] = _drive_mod()

import app  # noqa: E402
app.drive = sys.modules["app.drive"]
from app import main, librarian, store  # noqa: E402

client = main.app.test_client()
H = {"Authorization": "Bearer t"}


def post(path, body):
    r = client.post(path, json=body, headers=H)
    return r.status_code, r.get_json()


def get(path):
    r = client.get(path, headers=H)
    return r.status_code, r.get_json()


# ---------------------------------------------------------------- test PDFs
import fitz  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

TMP = tempfile.mkdtemp()


def score_pdf(path, pages, big, label):
    """Music-like pages. big=True embeds 600-dpi colour scans with grain."""
    doc = fitz.open()
    rnd = random.Random(label)
    for p in range(pages):
        page = doc.new_page(width=612, height=792)
        w, h = (5100, 6600) if big else (850, 1100)
        img = Image.new("RGB", (w, h), (250, 248, 240))
        d = ImageDraw.Draw(img)
        for staff in range(10):
            y0 = int(h * (0.08 + staff * 0.09))
            for line in range(5):
                y = y0 + line * int(h * 0.008)
                d.line([(int(w * 0.06), y), (int(w * 0.94), y)], fill=(20, 20, 20), width=max(2, w // 900))
            for n in range(24):
                x = int(w * (0.1 + n * 0.035))
                yy = y0 + rnd.randint(0, 4) * int(h * 0.008)
                r = max(4, w // 250)
                d.ellipse([x - r, yy - r, x + r, yy + r], fill=(10, 10, 10))
        if big:
            # grain, so the JPEG is large the way a real colour scan is
            px = img.load()
            for _ in range(w * h // 40):
                x, y = rnd.randrange(w), rnd.randrange(h)
                c = rnd.randint(225, 255)
                px[x, y] = (c, c - 3, c - 8)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=97 if big else 80)
        page.insert_image(page.rect, stream=buf.getvalue())
        page.insert_text((40, 40), "{} p{}".format(label, p + 1), fontsize=9)
    doc.save(path)
    doc.close()
    return os.path.getsize(path)


big = os.path.join(TMP, "BigScore.pdf")
size_big = score_pdf(big, 9, True, "Big")
small = os.path.join(TMP, "ANS-Bernstein-Somewhere.pdf")
score_pdf(small, 3, False, "Bernstein")
small_copy = os.path.join(TMP, "Bernstein-Somewhere-SM.pdf")
shutil.copy(small, small_copy)
print("big test PDF: %.1f MB" % (size_big / 1048576.0))
assert size_big > 30 * 1048576, "test PDF must be over 30 MB"

FILES["f-big"] = {"root": "A", "name": "BigScore.pdf", "rel_path": ["PDFs"], "path": big}
FILES["f-bern"] = {"root": "R", "name": "ANS-Bernstein-Somewhere.pdf", "rel_path": [], "path": small}
FILES["f-bern-sm"] = {"root": "S", "name": "Bernstein-Somewhere-SM.pdf", "rel_path": ["PDFs"], "path": small_copy}

ok = 0


def check(cond, what):
    global ok
    if not cond:
        print("FAIL:", what)
        sys.exit(1)
    ok += 1
    print("ok  ", what)


# 1. Automatic optimisation on intake, no click.
code, res = post("/scan", {"group": "claude-test", "folder_id": "A", "project_prefix": "Claude Test Concert"})
check(code == 200, "scan answers 200")
row = res["results"][0]
check(row["outcome"] == "staged", "big PDF staged")
check(row["optimisation"]["applied_before_publishing"], "optimised automatically on intake: " + row["optimisation"]["outcome"])
sid = row["staging_id"]
stg = librarian.load_staging()[0]["items"][sid]
check(stg["source_size"] < 30 * 1048576, "staged copy is under 30 MB (%.1f MB)" % (stg["source_size"] / 1048576.0))
check(stg["source_sha"] != stg["content_sha"], "original's hash kept beside the optimised one")
check(len(OBJ[stg["staging_path"]]["data"]) == stg["source_size"], "stored object is the optimised bytes")

# 2. Publish it; a rescan of the unchanged original is a duplicate.
code, res = post("/publish", {"staging_id": sid, "decision": "new_work", "canonical": "BigScore"})
check(code == 200 and res["ok"], "optimised file publishes")
OBJ.pop(store.CURSORS_PATH, None)  # force a full re-examination
code, res = post("/scan", {"group": "claude-test", "folder_id": "A", "project_prefix": "Claude Test Concert"})
check(res["results"][0]["outcome"] == "duplicate", "rescan of the original recognised as already published")

# 3. Publish refuses an oversized PDF unless allow_large.
item = dict(stg)
item.update({"state": "pending", "source_size": size_big, "staging_id": "bigone"})
librarian.stage(item, staging_id="bigone")
code, res = post("/publish", {"staging_id": "bigone", "decision": "new_work", "canonical": "BigAgain"})
check(code == 400 and "30 MB" in res["error"], "publish refuses >30 MB: " + res["error"][:60])

# 4. A second publish of an already-published file is refused.
code, res = post("/publish", {"staging_id": "bigone", "decision": "new_work", "canonical": "BigAgain", "allow_large": True})
check(code == 400 and "already published" in res["error"], "duplicate publish refused: " + res["error"][:60])

# 5. A file that cannot be stored is recorded as an error, and the scan goes on.
FILES["f-fail"] = {"root": "A", "name": "WillFail.pdf", "rel_path": [], "path": small}
os.utime(small, None)
FAIL_UPLOAD.add(".pdf")
code, res = post("/scan", {"group": "claude-test-2", "folder_id": "A", "project_prefix": "Fail Test"})
FAIL_UPLOAD.clear()
check(code == 200, "scan with a store failure still answers 200")
failed = [r for r in res["results"] if r["outcome"] == "store_failed"]
check(failed and "timed out" in failed[0]["detail"], "store failure reported per file")
check(res["store_failed"] >= 1, "store_failed counted in the scan response")
code, res = get("/staging?state=error")
check(res["count"] >= 1 and res["items"][0]["error"], "error items listed under state=error")
FILES.pop("f-fail")

# 6. The same score in another project is proposed as link_existing, never a new work.
post("/scan", {"group": "ans", "folder_id": "R", "project_prefix": ""})
code, res = get("/staging?state=pending")
rsid = [i for i in res["items"] if i["source_file_id"] == "f-bern"][0]["staging_id"]
code, res = post("/publish", {"staging_id": rsid, "decision": "new_work", "canonical": "ANS-Bernstein-Somewhere"})
check(res["ok"], "September Bernstein published in _root")
keep_id = res["work_id"]
code, res = post("/scan", {"group": "ans", "folder_id": "S", "project_prefix": "Sound & Motion"})
prop = res["results"][0]["proposal"]
check(prop["decision"] == "link_existing" and prop["work_id"] == keep_id, "S&M copy proposed as link_existing to the existing work")
ssid = res["results"][0]["staging_id"]
code, res = post("/publish", {"staging_id": ssid, "decision": "link_existing", "work_id": keep_id})
check(res["ok"] and res["alias_of"] == keep_id, "link_existing creates an alias, not a second work")
alias_id = res["work_id"]
lib = get("/library/ans?urls=0")[1]["scores"]
rows = {r["work_id"]: r for r in lib}
check(rows[alias_id]["alias_of"] == keep_id and rows[alias_id]["project"] == "Sound & Motion", "library lists the S&M copy, marked as an alias")

# 7. Merge two works that are one piece; dry run first, current must be stated.
reg, gen = librarian.load_registry()
dup = json.loads(json.dumps(reg["works"][keep_id]))
dup.update({"work_id": "dupwork00001", "project": "Sound & Motion Old", "canonical": "Bernstein-SM"})
reg["works"]["dupwork00001"] = dup
store.write_json(store.REGISTRY_PATH, reg, gen)
OBJ[store.work_published_path(dup)] = {"data": b"x", "gen": 1, "meta": {}}
code, res = post("/works/merge", {"keep": keep_id, "merge": "dupwork00001"})
check(code == 400 and "current" in res["error"], "merge refuses without a stated current version")
code, res = post("/works/merge", {"keep": keep_id, "merge": "dupwork00001", "current": "keep"})
check(res["dry_run"] and len(res["versions_after"]) == 2, "merge dry run shows the combined history")
check("dupwork00001" in librarian.load_registry()[0]["works"] and not librarian.load_registry()[0]["works"]["dupwork00001"].get("alias_of"), "dry run wrote nothing")
code, res = post("/works/merge", {"keep": keep_id, "merge": "dupwork00001", "current": "keep", "dry_run": False})
check(res["ok"] and not res["dry_run"], "merge applied")
reg = librarian.load_registry()[0]
check(reg["works"]["dupwork00001"]["alias_of"] == keep_id and len(reg["works"][keep_id]["versions"]) == 2, "merged work is an alias; history carried")
kept_bytes = OBJ[store.work_published_path(reg["works"][keep_id])]["data"]
check(OBJ[store.work_published_path(reg["works"]["dupwork00001"])]["data"] == kept_bytes, "both published paths serve the chosen version")

# 8. An update to the shared piece lands on every path.
score_pdf(small, 3, False, "Bernstein")  # same pages, new bytes
doc = fitz.open(small)
doc.set_metadata({"title": "revised"})
doc.save(small + ".2.pdf")
doc.close()
shutil.move(small + ".2.pdf", small)
OBJ.pop(store.CURSORS_PATH, None)
code, res = post("/scan", {"group": "ans", "folder_id": "R", "project_prefix": ""})
r0 = res["results"][0]
check(r0["proposal"]["decision"] == "new_edition", "changed file proposed as a new edition: " + r0["proposal"]["decision"])
code, res = post("/publish", {"staging_id": r0["staging_id"], "decision": "new_edition", "work_id": alias_id})
check(res["ok"] and res["work_id"] == keep_id, "an edition approved against the alias lands on the shared work")
reg = librarian.load_registry()[0]
newest = OBJ[store.work_published_path(reg["works"][keep_id])]["data"]
check(OBJ[store.work_published_path(reg["works"][alias_id])]["data"] == newest, "S&M's path got the new version too")
check(OBJ[store.work_published_path(reg["works"]["dupwork00001"])]["data"] == newest, "the merged work's path got it too")

# 9. Resolve waiting items whose exact file is already published.
pub = reg["works"][keep_id]["versions"][-1]
librarian.stage({"group": "ans", "project": "_root", "source_file_id": pub["source_file_id"],
                 "source_name": "stale.pdf", "source_size": 1, "content_sha": pub["content_sha"],
                 "inspected": {"media": "pdf", "edition_key": "k", "page_count": 3, "page_dims": []},
                 "proposal": {"decision": "new_work"}, "staging_path": ""}, staging_id="stale0000001")
code, res = post("/staging/resolve", {"group": "ans"})
check(res["dry_run"] and res["count"] == 1, "resolve dry run finds the stale item")
code, res = post("/staging/resolve", {"group": "ans", "dry_run": False})
check(librarian.load_staging()[0]["items"]["stale0000001"]["state"] == "published", "stale item closed as published")


# 10. v0.9.1: the real Drive folder travels with PDFs too ("PDFs" is not lost).
code, res = post("/scan", {"group": "claude-test", "folder_id": "A", "project_prefix": "Claude Test Concert"})
check("Claude Test Concert/PDFs" in res["folders_in_scan"], "folders_in_scan reports the real PDFs folder")
lib = {r["canonical"]: r for r in get("/library/claude-test?urls=0")[1]["scores"]}
check(lib["BigScore"]["project"] == "Claude Test Concert" and lib["BigScore"]["folder"] == "Claude Test Concert/PDFs",
      "published path unchanged (frozen), folder reports PDFs")
# 11. v0.9.2: a score optimised AFTER publishing (/optimise/scan) keeps its Drive
# link, so renaming its folder is a move - not a lost score, not a re-stage.
big2 = os.path.join(TMP, "BigTwo.pdf")
score_pdf(big2, 9, True, "BigTwo")
FILES["f-big2"] = {"root": "B", "name": "BigTwo.pdf", "rel_path": ["PDFs"], "path": big2}
librarian.BIG_FILE_BYTES, keep_big = 10 ** 12, librarian.BIG_FILE_BYTES  # stage unoptimised, as before v0.9
code, res = post("/scan", {"group": "g2", "folder_id": "B", "project_prefix": "Concert Two"})
librarian.BIG_FILE_BYTES = keep_big
r = res["results"][0]
code, res = post("/publish", {"staging_id": r["staging_id"], "decision": "new_work", "canonical": "BigTwo", "allow_large": True})
w2 = res["work_id"]
code, res = post("/optimise/scan", {"group": "g2", "work_ids": [w2]})
code, res = post("/publish", {"staging_id": res["results"][0]["staging_id"], "decision": "new_edition", "work_id": w2})
check(res["ok"] and res["version"] == 2, "published score optimised as edition v2 (source optimise:...)")
FILES["f-big2"]["rel_path"] = ["01 Scores"]
OBJ.pop(store.CURSORS_PATH, None)
code, res = post("/scan", {"group": "g2", "folder_id": "B", "project_prefix": "Concert Two"})
check(any(m["work_id"] == w2 and m["folder"] == "Concert Two/01 Scores" for m in res["moved"]), "folder rename recorded as a move for the optimised score")
check(res["results"] and res["results"][0]["outcome"] == "duplicate", "unchanged original is a duplicate, not re-staged")
check(get("/library/g2?urls=0")[1]["scores"][0]["folder"] == "Concert Two/01 Scores", "library reports the new folder")

code, res = get("/health")
check(res["version"] == "0.9.2", "health reports 0.9.2")
print("\nALL %d CHECKS PASSED" % ok)
