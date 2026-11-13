"""
Drive push (v0.12.0, reworked in v0.12.1, HUB-78).

WHY. Nothing looked at Drive unless a manager pressed Rescan, so on 2026-11-12
Tom told the choir three new scores were on the Hub hours before they were.
Polling every project all day is the shape Jonathan rejected in 1.38.1. This
is the other shape: Drive tells us when something changed.

WHY THE WORKER TAKES THE PINGS (0.12.1). 0.12.0 had Drive ping WordPress
directly. On Staging that was 78 pings in seven minutes - Drive pings for
EVERY change anywhere in the shared drive, and a Google Doc being typed in
changes every few seconds - and each ping was a full WordPress request.
Staging slowed to 25-40 s a page, on the host it shares with LIVE. So the
pings come here, where Cloud Run absorbs them, and WordPress hears only
what concerns it:

    WP    -> POST /drive/watch {drive_id, notify_url, notify_token, folders}
             open or renew the channel; also refreshes notify_url / folders
    Drive -> POST /drive/push   (checked by channel id + channel token)
             read the change feed; keep changes to PDFs, audio and folders
             whose ancestors include one of `folders`; then, only if any:
    worker-> POST notify_url {folders: [...], files: [...]}
             header X-ANS-Notify-Token: notify_token

A native Google Sheet (the HUB-68 video list) counts too, but notifies a
folder at most once every SHEET_QUIET seconds, because a Sheet being edited
changes constantly. Google Docs and everything else never notify.

Each worker keeps its own channel and page token in its own bucket
(_state/drive_watch.json), so the Staging and LIVE services never share one.
Drive caps a changes channel at 7 days; WordPress renews it twice a day.
"""

import json
import secrets
import time
import urllib.request
import uuid

from flask import jsonify, request

from . import drive, media, store

WATCH_PATH = "_state/drive_watch.json"
MAX_TTL_HOURS = 168  # Drive's ceiling for a changes channel
SHEET_QUIET = 300
FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _public(entry: dict) -> dict:
    """A drive's watch state as callers see it. Never a token."""
    ch = entry.get("channel") or {}
    out = {
        "has_page_token": bool(entry.get("page_token")),
        "last_changes_at": entry.get("last_changes_at", ""),
        "notify_url": (entry.get("notify") or {}).get("url", ""),
        "folders": len(entry.get("folders") or []),
        "last_push_at": entry.get("last_push_at", ""),
        "pushes": entry.get("pushes", 0),
        "last_notify": entry.get("last_notify"),
        "channel": None,
    }
    if ch:
        out["channel"] = {
            "id": ch.get("id"),
            "address": ch.get("address"),
            "expiration_ms": ch.get("expiration_ms"),
            "expires_in_hours": round((int(ch.get("expiration_ms") or 0) - _now_ms()) / 3600000, 1),
            "opened_at": ch.get("opened_at"),
        }
    return out


def _load():
    state, gen = store.read_json(WATCH_PATH, {"drives": {}})
    state.setdefault("drives", {})
    return state, gen


def _save(mutate):
    """Read, change, write with the generation precondition; retry on a race."""
    for _attempt in range(4):
        state, gen = _load()
        result = mutate(state)
        try:
            store.write_json(WATCH_PATH, state, gen)
            return result
        except store.Conflict:
            continue
    raise RuntimeError("drive watch state kept changing underneath; try again")


def relevant(change: dict) -> str:
    """'file', 'sheet' or '' - does this kind of change ever matter to the Hub?"""
    f = change.get("file") or {}
    mime = f.get("mimeType", "")
    if change.get("removed") or f.get("trashed"):
        return ""  # a removal is judged by the next scan or the daily fallback
    if mime == FOLDER_MIME:
        return "file"
    if mime == SHEET_MIME:
        return "sheet"
    if media.classify({"name": f.get("name", ""), "mimeType": mime}) in (media.PDF, media.AUDIO):
        return "file"
    return ""


def hits_for(changes: list[dict], folders: set, sheet_last: dict, now: float, svc=None) -> tuple[list, list, dict]:
    """
    Which watched folders these changes touched. Returns (folders hit, file
    names, updated sheet_last). Ancestors are looked up only for relevant
    changes, sharing one cache, so a burst of Doc edits costs nothing.
    """
    cache: dict = {}
    hit: dict = {}
    names = []
    for ch in changes:
        kind = relevant(ch)
        if not kind:
            continue
        f = ch.get("file") or {}
        chain = [ch.get("fileId", "")] + drive.ancestors(f.get("parents") or [], svc, cache)
        mine = [fid for fid in chain if fid in folders]
        if not mine:
            continue
        for fid in mine:
            if kind == "sheet":
                if now - float(sheet_last.get(fid, 0)) < SHEET_QUIET:
                    continue
                sheet_last[fid] = now
            hit[fid] = True
        names.append(f.get("name", ""))
    return list(hit), names, sheet_last


def _notify(url: str, token: str, payload: dict) -> str:
    """POST the hits to WordPress. Returns '' or the error. Never raises."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-ANS-Notify-Token": token,
                 "User-Agent": "ans-scores-worker"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - the URL is WordPress's own, set over the bearer-authed /drive/watch
            return "" if 200 <= resp.status < 300 else "HTTP {}".format(resp.status)
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:300]


def register(app, authorised, deny):

    @app.get("/drive/watch")
    def drive_watch_state():
        if not authorised():
            return deny()
        drive_id = (request.args.get("drive_id") or "").strip()
        state, _gen = _load()
        if drive_id:
            return jsonify({"ok": True, "drive_id": drive_id, **_public(state["drives"].get(drive_id, {}))})
        return jsonify({"ok": True, "drives": {k: _public(v) for k, v in state["drives"].items()}})

    @app.post("/drive/watch")
    def drive_watch():
        if not authorised():
            return deny()
        body = request.get_json(force=True, silent=True) or {}
        drive_id = str(body.get("drive_id") or "").strip()
        notify_url = str(body.get("notify_url") or "").strip()
        notify_token = str(body.get("notify_token") or "")
        folders = sorted({str(f).strip() for f in (body.get("folders") or []) if str(f).strip()})
        force = bool(body.get("force"))
        ttl_hours = max(1, min(MAX_TTL_HOURS, int(body.get("ttl_hours") or MAX_TTL_HOURS)))
        renew_within = max(0, int(body.get("renew_within_hours") or 72))
        if not drive_id or not notify_url.startswith("https://") or not notify_token:
            return jsonify({"ok": False, "error": "drive_id, an https notify_url and notify_token are required"}), 400

        # Drive pings THIS service. Cloud Run terminates TLS, so the scheme
        # Flask sees is http; the public address is always https.
        address = "https://" + request.host + "/drive/push"

        def keep_settings(st):
            e = st["drives"].setdefault(drive_id, {})
            e["notify"] = {"url": notify_url, "token": notify_token}
            e["folders"] = folders
            return dict(e)

        entry = _save(keep_settings)
        current = entry.get("channel") or {}
        left_ms = int(current.get("expiration_ms") or 0) - _now_ms()
        if current and not force and current.get("address") == address and left_ms > renew_within * 3600000:
            return jsonify({"ok": True, "renewed": False, "drive_id": drive_id, **_public(entry)})

        svc = drive.service()
        channel_token = secrets.token_urlsafe(32)
        try:
            page_token = entry.get("page_token") or drive.start_page_token(drive_id, svc)
            channel_id = "ans-" + uuid.uuid4().hex
            resp = drive.watch_changes(drive_id, page_token, channel_id, address, channel_token,
                                       _now_ms() + ttl_hours * 3600000, svc)
        except Exception as exc:  # noqa: BLE001 - Drive's own message is the useful part
            return jsonify({"ok": False, "error": "drive watch failed", "detail": str(exc)}), 502

        new_channel = {
            "id": channel_id,
            "resource_id": resp.get("resourceId", ""),
            "address": address,
            "token": channel_token,
            "expiration_ms": int(resp.get("expiration") or 0),
            "opened_at": _stamp(),
        }

        def put_channel(st):
            e = st["drives"].setdefault(drive_id, {})
            e.setdefault("page_token", page_token)
            old = e.get("channel") or {}
            e["channel"] = new_channel
            return old, dict(e)

        old, saved = _save(put_channel)
        # The new channel is listening before the old one is closed.
        stopped = ""
        if old.get("id") and old.get("resource_id"):
            try:
                drive.stop_channel(old["id"], old["resource_id"], svc)
                stopped = old["id"]
            except Exception as exc:  # noqa: BLE001 - an expired channel cannot be stopped; harmless
                stopped = "not stopped: {}".format(exc)
        return jsonify({"ok": True, "renewed": True, "stopped_previous": stopped,
                        "drive_id": drive_id, **_public(saved)})

    @app.post("/drive/unwatch")
    def drive_unwatch():
        if not authorised():
            return deny()
        body = request.get_json(force=True, silent=True) or {}
        drive_id = str(body.get("drive_id") or "").strip()
        state, _gen = _load()
        current = (state["drives"].get(drive_id) or {}).get("channel") or {}
        if not current:
            return jsonify({"ok": True, "stopped": ""})
        detail = ""
        try:
            drive.stop_channel(current["id"], current.get("resource_id", ""))
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)

        def mutate(st):
            st["drives"].setdefault(drive_id, {}).pop("channel", None)

        _save(mutate)
        return jsonify({"ok": True, "stopped": current["id"], "detail": detail})

    @app.post("/drive/push")
    def drive_push():
        """
        Drive's ping. No bearer token - Drive cannot send one - so the channel
        id and its token, both known only to Drive and this service, are the
        credential. Always answers 200 to a genuine ping, so Drive does not
        retry; a failure to tell WordPress is recorded and the daily fallback
        catches what it missed.
        """
        channel_id = request.headers.get("X-Goog-Channel-ID", "")
        token = request.headers.get("X-Goog-Channel-Token", "")
        state, _gen = _load()
        drive_id = ""
        for did, e in state["drives"].items():
            ch = e.get("channel") or {}
            if ch.get("id") and ch["id"] == channel_id and secrets.compare_digest(str(ch.get("token", "")), token):
                drive_id = did
                break
        if not drive_id:
            return jsonify({"ok": False}), 403
        if request.headers.get("X-Goog-Resource-State", "").lower() == "sync":
            return jsonify({"ok": True, "sync": True})

        entry = state["drives"][drive_id]
        if not entry.get("page_token"):
            return jsonify({"ok": True, "note": "no page token"})
        svc = drive.service()
        try:
            changes, next_token, _complete = drive.list_changes(drive_id, entry["page_token"], svc)
        except Exception as exc:  # noqa: BLE001
            def failed(st):
                st["drives"][drive_id]["last_error"] = "changes.list: {}".format(exc)[:300]
            _save(failed)
            return jsonify({"ok": True, "error": "changes.list failed"})

        folders = set(entry.get("folders") or [])
        hit, names, sheet_last = hits_for(changes, folders, dict(entry.get("sheet_last") or {}), time.time(), svc)
        notify = entry.get("notify") or {}
        result = None
        if hit and notify.get("url"):
            err = _notify(notify["url"], notify.get("token", ""), {"folders": hit, "files": names[:50]})
            result = {"at": _stamp(), "folders": hit, "files": names[:10], "error": err}

        def advance(st):
            e = st["drives"][drive_id]
            e["page_token"] = next_token
            e["last_changes_at"] = _stamp()
            e["last_push_at"] = _stamp()
            e["pushes"] = int(e.get("pushes", 0)) + 1
            e["sheet_last"] = sheet_last
            if result:
                e["last_notify"] = result

        _save(advance)
        return jsonify({"ok": True, "changes": len(changes), "notified": bool(result)})

    @app.post("/drive/changes")
    def drive_changes():
        """
        Manual look at the change feed: every file changed since the last
        read, each with every folder above it. Advances the page token, so
        use it only for diagnosis - /drive/push reads the same feed.
        """
        if not authorised():
            return deny()
        body = request.get_json(force=True, silent=True) or {}
        drive_id = str(body.get("drive_id") or "").strip()
        if not drive_id:
            return jsonify({"ok": False, "error": "drive_id is required"}), 400

        state, _gen = _load()
        entry = state["drives"].get(drive_id, {})
        svc = drive.service()
        if not entry.get("page_token"):
            token = drive.start_page_token(drive_id, svc)

            def first(st):
                st["drives"].setdefault(drive_id, {})["page_token"] = token

            _save(first)
            return jsonify({"ok": True, "drive_id": drive_id, "changes": [], "complete": True,
                            "note": "no page token yet; changes are counted from now"})

        try:
            changes, next_token, complete = drive.list_changes(drive_id, entry["page_token"], svc)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": "changes.list failed", "detail": str(exc)}), 502

        cache: dict = {}
        out = []
        for ch in changes:
            f = ch.get("file") or {}
            parents = f.get("parents") or []
            out.append({
                "file_id": ch.get("fileId"),
                "name": f.get("name", ""),
                "mimeType": f.get("mimeType", ""),
                "removed": bool(ch.get("removed")),
                "trashed": bool(f.get("trashed")),
                "is_folder": f.get("mimeType") == FOLDER_MIME,
                "ancestors": drive.ancestors(parents, svc, cache) if parents else [],
            })

        def advance(st):
            e = st["drives"].setdefault(drive_id, {})
            e["page_token"] = next_token
            e["last_changes_at"] = _stamp()

        _save(advance)
        return jsonify({"ok": True, "drive_id": drive_id, "complete": complete,
                        "count": len(out), "changes": out})
