"""
Practice takes - singers' saved recordings (v0.8.0, 2026-09-17).

The Ars Nova Practice add-on (WordPress) mixes a singer's take with the
practice track IN THE BROWSER and uploads the finished file straight to a
private bucket. This module only hands out short-lived signed URLs for that
bucket and deletes a take when the singer asks. It knows nothing about
singers: WordPress decides who may upload, play or delete what, and asks here
with the service token.

Why signed URLs and not an upload through this service: Cloud Run refuses a
request body over 32 MiB, and a nine-minute WAV mixdown is ~95 MB.

This is a DIFFERENT bucket from the scores mirror, and the mirror's R1 ("nothing
is ever deleted") does not apply to it: a take belongs to the singer, and the
singer deleting it is the point. store.py still contains no delete call.

Object layout (enforced by PATH_RE):

    takes/{env}/u{user_id}/{take_id}.{mp3|wav}

{env} is "staging" or "live", so a staging test never mixes with real takes.

All routes are POST with a JSON body and require the same bearer token as the
rest of the service.
"""

import os
import re
import threading
from datetime import timedelta

from flask import jsonify, request

TAKES_BUCKET = os.environ.get("TAKES_BUCKET", "ars-nova-practice-takes")

# Sites whose pages may PUT/GET takes from the browser.
DEFAULT_ORIGINS = [
    "https://stg-arsnovasingers-staging.kinsta.cloud",
    "https://arsnovasingers.org",
    "https://www.arsnovasingers.org",
]

PATH_RE = re.compile(r"^takes/(staging|live)/u[0-9]{1,10}/[a-z0-9][a-z0-9-]{7,63}\.(mp3|wav)$")
TYPES = {"mp3": "audio/mpeg", "wav": "audio/wav"}
MAX_BYTES = 150 * 1024 * 1024  # a 12-minute stereo WAV at 44.1 kHz is ~127 MB
LENGTH_HEADER = "x-goog-content-length-range"

_client = None
_lock = threading.Lock()


def _bucket():
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                from google.cloud import storage

                _client = storage.Client()
    return _client.bucket(TAKES_BUCKET)


def _signer_kwargs() -> dict:
    """Same approach as store.signed_url: on Cloud Run, sign through IAM SignBlob."""
    import google.auth
    from google.auth.transport.requests import Request

    creds, _project = google.auth.default()
    if getattr(creds, "signer_email", None) is not None:
        return {}
    creds.refresh(Request())
    return {
        "service_account_email": getattr(creds, "service_account_email", None),
        "access_token": creds.token,
    }


def _clean_path(raw) -> str | None:
    path = str(raw or "").strip()
    return path if PATH_RE.match(path) else None


def _ext(path: str) -> str:
    return path.rsplit(".", 1)[-1]


def _safe_filename(raw, ext: str) -> str:
    name = re.sub(r"[^A-Za-z0-9 ._()-]+", "", str(raw or "")).strip(" .") or "practice-take"
    name = name[:80]
    if not name.lower().endswith("." + ext):
        name += "." + ext
    return name


def cors_rules() -> list[dict]:
    raw = os.environ.get("TAKES_ORIGINS", "").strip()
    origins = [o.strip() for o in raw.split(",") if o.strip()] if raw else DEFAULT_ORIGINS
    return [
        {
            "origin": origins,
            "method": ["PUT", "GET", "HEAD"],
            # GCS uses this list both for exposed response headers and for the
            # request headers a preflight may ask for - the upload sends
            # Content-Type and the length-range header, so both must be here.
            "responseHeader": [
                "Content-Type",
                "Content-Length",
                "Content-Range",
                "Accept-Ranges",
                "ETag",
                LENGTH_HEADER,
            ],
            "maxAgeSeconds": 3600,
        }
    ]


def register(app, authorised, deny) -> None:
    """Wire the /takes routes. `authorised` and `deny` are main.py's token check."""

    def body() -> dict:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def bad(msg: str, code: int = 400):
        return jsonify({"ok": False, "error": msg}), code

    @app.post("/takes/setup")
    def takes_setup():
        """Apply the bucket's CORS rules (kept here, in code, rather than set by hand)."""
        if not authorised():
            return deny()
        b = _bucket()
        b.reload()
        b.cors = cors_rules()
        b.patch()
        return jsonify({"ok": True, "bucket": TAKES_BUCKET, "cors": b.cors})

    @app.post("/takes/upload-url")
    def takes_upload_url():
        if not authorised():
            return deny()
        data = body()
        path = _clean_path(data.get("path"))
        if not path:
            return bad("bad path")
        ctype = TYPES[_ext(path)]
        headers = {"Content-Type": ctype, LENGTH_HEADER: "1,{}".format(MAX_BYTES)}
        try:
            url = _bucket().blob(path).generate_signed_url(
                version="v4",
                expiration=timedelta(minutes=30),
                method="PUT",
                content_type=ctype,
                headers={LENGTH_HEADER: headers[LENGTH_HEADER]},
                **_signer_kwargs(),
            )
        except Exception as exc:  # noqa: BLE001
            return bad("could not sign an upload URL: {}".format(exc), 500)
        return jsonify({"ok": True, "url": url, "method": "PUT", "headers": headers, "max_bytes": MAX_BYTES})

    @app.post("/takes/stat")
    def takes_stat():
        if not authorised():
            return deny()
        path = _clean_path(body().get("path"))
        if not path:
            return bad("bad path")
        blob = _bucket().get_blob(path)
        if blob is None:
            return jsonify({"ok": True, "exists": False})
        return jsonify({"ok": True, "exists": True, "size": blob.size, "content_type": blob.content_type})

    @app.post("/takes/url")
    def takes_url():
        """A fresh read URL. download=true makes the browser save it under `filename`."""
        if not authorised():
            return deny()
        data = body()
        path = _clean_path(data.get("path"))
        if not path:
            return bad("bad path")
        ext = _ext(path)
        kwargs = {"response_type": TYPES[ext]}
        if data.get("download"):
            fname = _safe_filename(data.get("filename"), ext)
            kwargs["response_disposition"] = 'attachment; filename="{}"'.format(fname)
        minutes = max(5, min(240, int(data.get("minutes") or 120)))
        try:
            url = _bucket().blob(path).generate_signed_url(
                version="v4",
                expiration=timedelta(minutes=minutes),
                method="GET",
                **kwargs,
                **_signer_kwargs(),
            )
        except Exception as exc:  # noqa: BLE001
            return bad("could not sign a read URL: {}".format(exc), 500)
        return jsonify({"ok": True, "url": url, "minutes": minutes})

    @app.post("/takes/delete")
    def takes_delete():
        if not authorised():
            return deny()
        path = _clean_path(body().get("path"))
        if not path:
            return bad("bad path")
        blob = _bucket().get_blob(path)
        if blob is None:
            return jsonify({"ok": True, "deleted": False, "missing": True})
        blob.delete()
        return jsonify({"ok": True, "deleted": True})
