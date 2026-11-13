"""
Drive push (v0.12.0, HUB-78).

WHY. Nothing looked at Drive unless a manager pressed Rescan, so on 2026-11-12
Tom told the choir three new scores were on the Hub hours before they were.
Polling every project all day is the shape Jonathan rejected in 1.38.1. This
is the other shape: Drive tells us when something changed.

HOW IT IS SPLIT. The worker holds the Drive credentials, so it opens the
channel and reads the change feed. It does NOT receive the pings: WordPress
does, at the address WordPress hands in, because WordPress is what knows which
Drive folder belongs to which project. So:

    WP  -> POST /drive/watch   {drive_id, address, token}   open or renew
    Drive -> POST <address>                                   "something changed"
    WP  -> POST /drive/changes {drive_id}                     what changed, with
                                                              every ancestor
                                                              folder of each file
    WP  -> POST /scan ...                                     the projects hit

Each worker keeps its own channel and page token in its own bucket
(_state/drive_watch.json), so the Staging and LIVE services never share one.

Drive limits a changes channel to 7 days. WordPress renews it twice a day;
/drive/watch only opens a new channel when the current one is inside
`renew_within_hours` of expiring, or has a different address, or `force`.
The page token is kept across renewals, so nothing that changed in between is
skipped.
"""

import time
import uuid

from flask import jsonify, request

from . import drive, store

WATCH_PATH = "_state/drive_watch.json"
MAX_TTL_HOURS = 168  # Drive's ceiling for a changes channel


def _now_ms() -> int:
    return int(time.time() * 1000)


def _public(entry: dict) -> dict:
    """A drive's watch state as callers see it."""
    ch = entry.get("channel") or {}
    out = {
        "has_page_token": bool(entry.get("page_token")),
        "last_changes_at": entry.get("last_changes_at", ""),
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
    """Read, change, write with the generation precondition; retry once on a race."""
    for _attempt in range(3):
        state, gen = _load()
        result = mutate(state)
        try:
            store.write_json(WATCH_PATH, state, gen)
            return result
        except store.Conflict:
            continue
    raise RuntimeError("drive watch state kept changing underneath; try again")


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
        address = str(body.get("address") or "").strip()
        token = str(body.get("token") or "")
        force = bool(body.get("force"))
        ttl_hours = max(1, min(MAX_TTL_HOURS, int(body.get("ttl_hours") or MAX_TTL_HOURS)))
        renew_within = max(0, int(body.get("renew_within_hours") or 72))
        if not drive_id or not address.startswith("https://"):
            return jsonify({"ok": False, "error": "drive_id and an https address are required"}), 400
        if len(token) > 256:
            return jsonify({"ok": False, "error": "token is longer than Drive allows (256)"}), 400

        state, _gen = _load()
        entry = state["drives"].get(drive_id, {})
        current = entry.get("channel") or {}
        left_ms = int(current.get("expiration_ms") or 0) - _now_ms()
        if (
            current
            and not force
            and current.get("address") == address
            and left_ms > renew_within * 3600000
        ):
            return jsonify({"ok": True, "renewed": False, "drive_id": drive_id, **_public(entry)})

        svc = drive.service()
        try:
            page_token = entry.get("page_token") or drive.start_page_token(drive_id, svc)
            channel_id = "ans-" + uuid.uuid4().hex
            resp = drive.watch_changes(
                drive_id, page_token, channel_id, address, token,
                _now_ms() + ttl_hours * 3600000, svc,
            )
        except Exception as exc:  # noqa: BLE001 - Drive's own message is the useful part
            return jsonify({"ok": False, "error": "drive watch failed", "detail": str(exc)}), 502

        new_channel = {
            "id": channel_id,
            "resource_id": resp.get("resourceId", ""),
            "address": address,
            "expiration_ms": int(resp.get("expiration") or 0),
            "opened_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        # The new channel exists before the old one is closed, so there is
        # never a gap with nobody listening.
        stopped = ""
        if current.get("id") and current.get("resource_id"):
            try:
                drive.stop_channel(current["id"], current["resource_id"], svc)
                stopped = current["id"]
            except Exception as exc:  # noqa: BLE001 - an expired channel cannot be stopped; harmless
                stopped = "not stopped: {}".format(exc)

        def mutate(st):
            e = st["drives"].setdefault(drive_id, {})
            e.setdefault("page_token", page_token)
            e["channel"] = new_channel
            return e

        saved = _save(mutate)
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

    @app.post("/drive/changes")
    def drive_changes():
        """
        Everything that changed in the drive since the last call, each file
        with every folder above it. Advances the page token, so a second call
        returns only what changed after the first. A missed batch is caught by
        WordPress's daily fallback scan, never by replaying old changes.
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
                "is_folder": f.get("mimeType") == "application/vnd.google-apps.folder",
                "ancestors": drive.ancestors(parents, svc, cache) if parents else [],
            })

        def advance(st):
            e = st["drives"].setdefault(drive_id, {})
            e["page_token"] = next_token
            e["last_changes_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        _save(advance)
        return jsonify({"ok": True, "drive_id": drive_id, "complete": complete,
                        "count": len(out), "changes": out})
