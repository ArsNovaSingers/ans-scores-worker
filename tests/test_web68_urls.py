"""
WEB-68 (v0.10.0/0.10.1): audio links last two hours and are served as audio/*;
`?download=<work_id>` adds an attachment-signed link for that one item;
`?work=<work_id>` signs only that item (0.10.1).
Nothing here talks to Google: store.signed_url is replaced with a recorder.
"""
import os
import sys

WORKER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WORKER)
os.environ["ANS_SCORES_TOKEN"] = "t"

from app import main, store, librarian  # noqa: E402

CALLS = []


def fake_signed_url(object_path, minutes=15, response_type=None, response_disposition=None):
    CALLS.append((object_path, minutes, response_type, response_disposition))
    return "https://storage.googleapis.com/ars-nova-scores/{}?m={}".format(object_path, minutes)


store.signed_url = fake_signed_url
librarian.library = lambda group: [
    {"work_id": "w_pdf", "media": "pdf", "mime": "application/pdf", "object_path": "g/p/Score.pdf"},
    {"work_id": "w_mp3", "media": "audio", "mime": "audio/mpeg", "object_path": "g/p/1-Prólogo e Balada.mp3"},
]

client = main.app.test_client()
H = {"Authorization": "Bearer t"}

r = client.get("/library/g", headers=H).get_json()
by = {s["work_id"]: s for s in r["scores"]}
assert CALLS[0] == ("g/p/Score.pdf", 15, None, None), CALLS[0]
assert CALLS[1] == ("g/p/1-Prólogo e Balada.mp3", 120, "audio/mpeg", None), CALLS[1]
assert by["w_mp3"]["url_expires_minutes"] == 120
assert "download_url" not in by["w_mp3"] and "download_url" not in by["w_pdf"]

CALLS.clear()
r = client.get("/library/g?download=w_mp3", headers=H).get_json()
by = {s["work_id"]: s for s in r["scores"]}
assert "download_url" in by["w_mp3"] and "download_url" not in by["w_pdf"]
path, minutes, rtype, disp = CALLS[-1]
assert minutes == 15 and rtype == "audio/mpeg", CALLS[-1]
assert disp.startswith('attachment; filename="1-Pr_logo e Balada.mp3"'), disp
assert "filename*=UTF-8''1-Pr%C3%B3logo%20e%20Balada.mp3" in disp, disp

CALLS.clear()
r = client.get("/library/g?work=w_mp3&download=w_mp3", headers=H).get_json()
assert [s["work_id"] for s in r["scores"]] == ["w_mp3"] and r["count"] == 1, r
assert len(CALLS) == 2, CALLS  # one inline link + one attachment link, nothing else signed
CALLS.clear()
r = client.get("/library/g?work=nope", headers=H).get_json()
assert r["scores"] == [] and CALLS == [], (r, CALLS)

assert client.get("/library/g").status_code in (401, 403)
assert client.get("/health").get_json()["version"] == "0.11.0"
print("ok - WEB-68 worker url terms")
