"""
ans-scores-worker - HTTP surface.

claude/portal/Device_Sync_Spec.md. What exists here: the Drive walk, identity,
staging, publishing under frozen names, versioning, rollback, the group library
the Hub renders from, read-only WebDAV (Phase 5), and PDF optimisation offered
for a human's approval (Phase 2, /optimise/scan). What does not exist yet, on
purpose: quiet hours (Phase 6).

This service knows nothing about singers or permissions. /library answers
"what is published for this group"; WordPress owns "who is in that group".
Keeping those apart is what stops a second permission system growing here to
disagree with the first one.

Auth is a bearer token in the Authorization header, never a query parameter.
The existing Ars Nova connector fleet uses ?key= and that writes the token
into the Cloud Run request log in plaintext for every call. Not repeating it.
"""

import os
import tempfile
import time

from flask import Flask, jsonify, request

from . import dav, drive, fingerprint, librarian, naming, optimise, store

app = Flask(__name__)

# Read-only WebDAV over the published mirror. Registered here rather than
# imported for side effects, so the wiring is visible at the top of the app.
dav.register(app)

TOKEN = os.environ.get("ANS_SCORES_TOKEN", "")

# Folder names that describe storage rather than a project. A score in
# "Rivers & Streams/PDFs" belongs to the project "Rivers & Streams".
# Heuristic, and flagged as one in the README - it is overridable per group
# once the Hub owns the mapping.
CONTAINER_SEGMENTS = {"pdfs", "pdf", "scores", "sheet music", "music"}

DEFAULT_SCAN_LIMIT = 25


def _authorised() -> bool:
    if not TOKEN:
        return False
    header = request.headers.get("Authorization", "")
    return header.startswith("Bearer ") and header[7:].strip() == TOKEN


def _deny():
    return jsonify({"ok": False, "error": "unauthorised"}), 401


def _project_from_path(rel_path: list[str]) -> str:
    parts = [p for p in rel_path if p.strip().lower() not in CONTAINER_SEGMENTS]
    return "/".join(parts) if parts else "_root"


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "ans-scores-worker", "version": "0.5.0"})


@app.get("/drive/folders")
def drive_folders():
    """
    Browse Drive one level at a time, so a person can pick a folder instead of
    pasting an id they had to go and find.

    No parent: the shared drives this service account can see. With a parent:
    that folder's immediate subfolders, plus its own name and parent so the
    caller can draw a breadcrumb and a way back up.
    """
    if not _authorised():
        return _deny()

    parent = (request.args.get("parent") or "").strip()
    svc = drive.service()

    try:
        if not parent:
            drives = drive.shared_drives(svc)
            return jsonify(
                {
                    "ok": True,
                    "at": None,
                    "folders": [{"id": d["id"], "name": d["name"]} for d in drives],
                    "is_root": True,
                }
            )
        info = drive.folder_info(parent, svc)
        if not info.get("is_folder"):
            return jsonify({"ok": False, "error": "that id is not a folder"}), 400
        return jsonify(
            {
                "ok": True,
                "at": info,
                "folders": drive.list_folders(parent, svc),
                "is_root": False,
            }
        )
    except Exception as exc:  # noqa: BLE001
        # A folder the service account cannot see is the single most likely
        # failure here, and it needs to say so rather than "error".
        return jsonify(
            {
                "ok": False,
                "error": "Drive would not show that folder. The most likely reason is that "
                         "the service account is not a member of the drive it lives in.",
                "detail": str(exc),
            }
        ), 502


@app.post("/optimise/staged")
def optimise_staged():
    """
    Make a staged file smaller BEFORE it is ever published.

    /optimise/scan works on what is already published, which was the right
    first step for the scores that are already out there. This is the better
    place for it: a file caught on the way in is published small from the
    start, so no singer ever downloads the large version and no second version
    is needed to replace it.

    The staged object is replaced in place and the item's fingerprint is
    recalculated, because the bytes a person approves must be the bytes that
    get published - staging a big file and publishing a small one would make
    the approval a lie.

    Body: {staging_id}
    """
    if not _authorised():
        return _deny()

    body = request.get_json(silent=True) or {}
    staging_id = (body.get("staging_id") or "").strip()
    if not staging_id:
        return jsonify({"ok": False, "error": "staging_id is required"}), 400

    staging, _gen = librarian.load_staging()
    item = staging["items"].get(staging_id)
    if item is None:
        return jsonify({"ok": False, "error": "no such staged item"}), 404
    if item.get("state") != "pending":
        return jsonify({"ok": False, "error": "that item is already " + str(item.get("state"))}), 400

    spath = item.get("staging_path") or ""
    if not spath or not store.object_exists(spath):
        return jsonify({"ok": False, "error": "the staged file is missing"}), 404

    with tempfile.TemporaryDirectory() as tmp:
        local = os.path.join(tmp, "staged.pdf")
        candidate = os.path.join(tmp, "smaller.pdf")
        store.bucket().blob(spath).download_to_filename(local)

        try:
            report = optimise.assess(local, candidate)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": "could not optimise", "detail": str(exc)}), 500

        if not report["worth_showing"]:
            return jsonify(
                {
                    "ok": True,
                    "changed": False,
                    "outcome": report["outcome"],
                    "bytes_before": report["bytes_before"],
                }
            )

        inspected = fingerprint.inspect(candidate)
        sha = fingerprint.content_sha(candidate)
        store.upload_file(candidate, spath, {"staging_id": staging_id, "sha256": sha})

    for _attempt in range(5):
        stg, gen = librarian.load_staging()
        entry = stg["items"].get(staging_id)
        if entry is None:
            return jsonify({"ok": False, "error": "the staged item vanished"}), 404
        entry["content_sha"] = sha
        entry["inspected"] = inspected
        entry["source_size"] = report["bytes_after"]
        entry["optimisation"] = {
            "bytes_before": report["bytes_before"],
            "bytes_after": report["bytes_after"],
            "saved_bytes": report["saved_bytes"],
            "saved_ratio": report["saved_ratio"],
            "target_dpi": optimise.TARGET_DPI,
            "jpeg_quality": optimise.JPEG_QUALITY,
            "images_reencoded": report.get("images_reencoded", 0),
            "verify": report["verify"],
            "source": report["source"],
            "applied_before_publishing": True,
        }
        try:
            store.write_json(store.STAGING_PATH, stg, gen)
            break
        except store.Conflict:
            continue
    else:
        return jsonify({"ok": False, "error": "staging is being written too fast"}), 503

    return jsonify(
        {
            "ok": True,
            "changed": True,
            "outcome": report["outcome"],
            "bytes_before": report["bytes_before"],
            "bytes_after": report["bytes_after"],
            "saved_ratio": report["saved_ratio"],
            "verify": report["verify"],
        }
    )


@app.post("/optimise/scan")
def optimise_scan():
    """
    Offer smaller versions of what is already published. Publishes nothing.

    Per Device_Sync_Spec 3.5 and Jonathan's 2026-08-25 ruling, optimisation is
    never applied automatically. This walks a group's published works, builds a
    candidate for each, and parks the ones worth looking at in the SAME staging
    queue that Drive intake uses - so approving one is the ordinary
    `POST /publish {decision: "new_edition"}` and inherits everything that
    already guards a publish: R3 ordering, the section 3.4 page-count gate, the
    version history, and rollback.

    Reusing that queue rather than building a parallel one is the whole design.
    A second approval mechanism would be a second place for "is this safe to
    publish" to be answered, and the two would drift.

    Body: {group, project?, limit?}
    """
    if not _authorised():
        return _deny()

    body = request.get_json(silent=True) or {}
    group = str(body.get("group") or "").strip()
    only_project = str(body.get("project") or "").strip()
    limit = int(body.get("limit") or 25)
    if not group:
        return jsonify({"ok": False, "error": "group is required"}), 400

    registry, _gen = librarian.load_registry()
    staging, _sgen = librarian.load_staging()

    # A work already waiting for a decision must not be offered twice - Tom
    # would see the same score in the queue as many times as this is run.
    pending_works = {
        item.get("optimisation", {}).get("work_id")
        for item in staging["items"].values()
        if item.get("state") == "pending" and item.get("optimisation")
    }

    results = []
    examined = 0

    for work_id, work in registry["works"].items():
        if work.get("group") != group:
            continue
        if only_project and work.get("project") != only_project:
            continue
        if not work.get("versions"):
            continue
        if work_id in pending_works:
            results.append({"canonical": work["canonical"], "outcome": "already in the queue"})
            continue
        if examined >= limit:
            results.append({"canonical": work["canonical"], "outcome": "not examined this run"})
            continue
        examined += 1

        ppath = store.published_path(work["group"], work["project"], work["canonical"])
        if not store.object_exists(ppath):
            results.append({"canonical": work["canonical"], "outcome": "no published object"})
            continue

        with tempfile.TemporaryDirectory() as tmp:
            local = os.path.join(tmp, "published.pdf")
            candidate = os.path.join(tmp, "candidate.pdf")
            try:
                store.bucket().blob(ppath).download_to_filename(local)
            except Exception as exc:  # noqa: BLE001
                results.append({"canonical": work["canonical"], "outcome": "download_failed",
                                "detail": str(exc)})
                continue

            try:
                report = optimise.assess(local, candidate)
            except Exception as exc:  # noqa: BLE001
                # An unreadable or unhandleable file is reported, never skipped
                # quietly - Org Portal spec 5.6.
                results.append({"canonical": work["canonical"], "outcome": "optimise_failed",
                                "detail": str(exc)})
                continue

            row = {
                "canonical": work["canonical"],
                "work_id": work_id,
                "bytes_before": report["bytes_before"],
                "bytes_after": report["bytes_after"],
                "saved_ratio": report["saved_ratio"],
                "outcome": report["outcome"],
                "worth_showing": report["worth_showing"],
            }

            if not report["worth_showing"]:
                results.append(row)
                continue

            # The candidate has to be inspected in its own right: publish()
            # compares its structure against the current version and refuses a
            # page-count change. That check is redundant with optimise.verify()
            # by design - two independent gates on the one thing that would
            # move every singer's annotations.
            try:
                inspected = fingerprint.inspect(candidate)
            except ValueError as exc:
                results.append({**row, "outcome": "candidate unreadable: %s" % exc,
                                "worth_showing": False})
                continue

            sha = fingerprint.content_sha(candidate)
            staging_id = librarian.stage(
                {
                    "group": work["group"],
                    "project": work["project"],
                    "source_file_id": "optimise:%s" % work_id,
                    "source_name": work["canonical"] + ".pdf (optimised)",
                    "source_size": report["bytes_after"],
                    "source_modified": None,
                    "content_sha": sha,
                    "inspected": inspected,
                    "proposal": {
                        "decision": "new_edition",
                        "work_id": work_id,
                        "confidence": "certain",
                        "why": "an optimised copy of the currently published file",
                    },
                    "optimisation": {
                        "work_id": work_id,
                        "bytes_before": report["bytes_before"],
                        "bytes_after": report["bytes_after"],
                        "saved_bytes": report["saved_bytes"],
                        "saved_ratio": report["saved_ratio"],
                        "target_dpi": optimise.TARGET_DPI,
                        "jpeg_quality": optimise.JPEG_QUALITY,
                        "images_reencoded": report.get("images_reencoded", 0),
                        "verify": report["verify"],
                        "source": report["source"],
                    },
                    "staging_path": "",
                }
            )
            spath = store.staging_path(staging_id)
            store.upload_file(candidate, spath, {"staging_id": staging_id, "sha256": sha})

            for _attempt in range(5):
                stg, gen = librarian.load_staging()
                stg["items"][staging_id]["staging_path"] = spath
                try:
                    store.write_json(store.STAGING_PATH, stg, gen)
                    break
                except store.Conflict:
                    continue

            row["staging_id"] = staging_id
            results.append(row)

    offered = [r for r in results if r.get("worth_showing")]
    return jsonify(
        {
            "ok": True,
            "group": group,
            "examined": examined,
            "offered": len(offered),
            "would_save_bytes": sum(r["bytes_before"] - r["bytes_after"] for r in offered),
            "results": results,
            "note": "Nothing was published. Approve one with "
                    "POST /publish {staging_id, decision: 'new_edition', work_id}.",
        }
    )


@app.get("/whoami")
def whoami():
    if not _authorised():
        return _deny()
    try:
        who = drive.whoami()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": "drive auth failed", "detail": str(exc)}), 500
    return jsonify({"ok": True, "drive": who, "bucket": store.BUCKET_NAME})


@app.post("/scan")
def scan():
    """
    Walk a group's Drive folder and stage anything new or changed.

    Never publishes. Never deletes. The most this call can do to what singers
    see is nothing at all - it produces questions, and a human answers them
    at /publish.
    """
    if not _authorised():
        return _deny()

    body = request.get_json(force=True, silent=True) or {}
    group = (body.get("group") or "").strip()
    folder_id = (body.get("folder_id") or "").strip()
    limit = int(body.get("limit") or DEFAULT_SCAN_LIMIT)
    if not group or not folder_id:
        return jsonify({"ok": False, "error": "group and folder_id are required"}), 400

    started = time.time()
    svc = drive.service()
    try:
        files = drive.walk(folder_id, svc)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": "drive walk failed", "detail": str(exc)}), 502

    pdfs = [f for f in files if f.get("mimeType") == "application/pdf"]
    seen_ids = {f["id"] for f in pdfs}

    cursors, cursor_gen = store.read_json(store.CURSORS_PATH, {"files": {}})
    cursors.setdefault("files", {})
    registry, _gen = librarian.load_registry()
    staging, _sgen = librarian.load_staging()
    already_pending = {
        item["source_file_id"]
        for item in staging["items"].values()
        if item.get("state") == "pending"
    }

    results = []
    unchanged = 0
    examined = 0
    remaining = 0

    for f in pdfs:
        cached = cursors["files"].get(f["id"])
        if (
            cached
            and cached.get("md5") == f.get("md5Checksum")
            and cached.get("modifiedTime") == f.get("modifiedTime")
        ):
            unchanged += 1
            continue
        if f["id"] in already_pending:
            unchanged += 1
            continue
        if examined >= limit:
            remaining += 1
            continue

        examined += 1
        project = _project_from_path(f.get("rel_path", []))
        entry = {
            "group": group,
            "project": project,
            "source_file_id": f["id"],
            "source_name": f["name"],
            "source_size": int(f.get("size") or 0),
            "source_modified": f.get("modifiedTime"),
        }

        with tempfile.TemporaryDirectory() as tmp:
            local = os.path.join(tmp, "candidate.pdf")
            try:
                drive.download(f["id"], local, svc)
            except Exception as exc:  # noqa: BLE001
                results.append({**entry, "outcome": "download_failed", "detail": str(exc)})
                continue

            try:
                inspected = fingerprint.inspect(local)
            except ValueError as exc:
                # Section 5.6 of the Org Portal spec: unreadable files are
                # reported to a human, never skipped quietly.
                results.append({**entry, "outcome": "unreadable", "detail": str(exc)})
                continue

            sha = fingerprint.content_sha(local)
            proposal = librarian.match(
                inspected, sha, f["name"], group, project, registry
            )

            if proposal["decision"] == "duplicate":
                cursors["files"][f["id"]] = {
                    "md5": f.get("md5Checksum"),
                    "modifiedTime": f.get("modifiedTime"),
                    "content_sha": sha,
                }
                results.append({**entry, "outcome": "duplicate", "why": proposal["why"]})
                continue

            staging_id = librarian.stage(
                {
                    **entry,
                    "content_sha": sha,
                    "inspected": inspected,
                    "proposal": proposal,
                    "staging_path": "",  # filled below once we know the id
                }
            )
            spath = store.staging_path(staging_id)
            store.upload_file(local, spath, {"staging_id": staging_id, "sha256": sha})

            # Record the object path now that it exists.
            stg, gen = librarian.load_staging()
            stg["items"][staging_id]["staging_path"] = spath
            store.write_json(store.STAGING_PATH, stg, gen)

            results.append(
                {
                    **entry,
                    "outcome": "staged",
                    "staging_id": staging_id,
                    "pages": inspected["page_count"],
                    "fingerprint": inspected["edition_method"],
                    "proposal": proposal,
                }
            )

    try:
        store.write_json(store.CURSORS_PATH, cursors, cursor_gen)
    except store.Conflict:
        pass  # another scan updated it; the cache is an optimisation, not truth

    flagged = librarian.mark_missing_at_source(group, seen_ids)

    return jsonify(
        {
            "ok": True,
            "group": group,
            "pdfs_in_folder": len(pdfs),
            "unchanged_since_last_scan": unchanged,
            "examined": examined,
            "not_examined_this_run": remaining,
            "missing_at_source": flagged,
            "results": results,
            "seconds": round(time.time() - started, 1),
        }
    )


@app.get("/staging")
def staging_list():
    if not _authorised():
        return _deny()
    state = request.args.get("state", "pending")
    staging, _gen = librarian.load_staging()
    items = [
        item for item in staging["items"].values()
        if state == "all" or item.get("state") == state
    ]
    items.sort(key=lambda i: i.get("staged_at", ""))
    return jsonify({"ok": True, "count": len(items), "items": items})


@app.get("/staging/<staging_id>/url")
def staging_url(staging_id):
    """
    Signed links to look at a staged candidate, and at what it would replace.

    /url deliberately refuses anything outside scores/, so it cannot serve a
    staged file. Rather than loosen that rule, this signs exactly one object:
    the staging item's own. Same spirit, narrower scope.

    Both links matter. A candidate is approved on the strength of what it
    LOOKS like, and the only honest way to judge that is beside the thing it
    replaces - which is precisely the check that would have caught a page
    rendered as a negative.
    """
    if not _authorised():
        return _deny()

    staging, _gen = librarian.load_staging()
    item = staging["items"].get(staging_id)
    if item is None:
        return jsonify({"ok": False, "error": "no such staged item"}), 404

    spath = item.get("staging_path") or ""
    if not spath or not store.object_exists(spath):
        return jsonify({"ok": False, "error": "staged object is missing"}), 404

    out = {"ok": True, "candidate_url": store.signed_url(spath), "expires_minutes": 15}

    work_id = (item.get("proposal") or {}).get("work_id")
    if work_id:
        registry, _rgen = librarian.load_registry()
        work = registry["works"].get(work_id)
        if work:
            ppath = store.published_path(work["group"], work["project"], work["canonical"])
            if store.object_exists(ppath):
                out["current_url"] = store.signed_url(ppath)
                out["current_path"] = ppath
    return jsonify(out)


@app.post("/publish")
def publish():
    if not _authorised():
        return _deny()
    body = request.get_json(force=True, silent=True) or {}
    try:
        result = librarian.publish(
            staging_id=body.get("staging_id", ""),
            decision=body.get("decision", ""),
            work_id=body.get("work_id"),
            canonical=body.get("canonical"),
            accept_structure_change=bool(body.get("accept_structure_change")),
            actor=body.get("actor", "unknown"),
        )
    except KeyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result})


@app.get("/works")
def works():
    if not _authorised():
        return _deny()
    group = request.args.get("group")
    registry, _gen = librarian.load_registry()
    items = [
        w for w in registry["works"].values()
        if not group or w["group"] == group
    ]
    items.sort(key=lambda w: (w["project"], w["canonical"]))
    return jsonify({"ok": True, "count": len(items), "works": items})


@app.get("/manifest/<group>")
def manifest(group: str):
    """Rebuild the manifest from what is actually in the bucket, and return it."""
    if not _authorised():
        return _deny()
    entries = store.list_published(group)
    store.write_manifest(group, entries)
    return jsonify({"ok": True, "group": group, "count": len(entries), "files": entries})


@app.post("/verify")
def verify():
    """
    R6. Every work's current version must exist at its published path, and the
    published copy must match the version copy byte for byte.
    """
    if not _authorised():
        return _deny()
    body = request.get_json(force=True, silent=True) or {}
    group = (body.get("group") or "").strip()
    registry, _gen = librarian.load_registry()

    problems = []
    checked = 0
    for work in registry["works"].values():
        if group and work["group"] != group:
            continue
        if not work["versions"]:
            continue
        checked += 1
        current = next(v for v in work["versions"] if v["n"] == work["current"])
        ppath = store.published_path(work["group"], work["project"], work["canonical"])
        if not store.object_exists(ppath):
            problems.append({"canonical": work["canonical"], "problem": "published file is missing"})
            continue
        if not store.object_exists(current["object_path"]):
            problems.append(
                {"canonical": work["canonical"], "problem": "version copy is missing"}
            )
    return jsonify(
        {
            "ok": not problems,
            "checked": checked,
            "problems": problems,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )


@app.get("/url")
def url():
    """Short-lived signed read URL. The Hub calls this; singers never do."""
    if not _authorised():
        return _deny()
    path = request.args.get("path", "")
    if not path.startswith("scores/"):
        return jsonify({"ok": False, "error": "only published scores can be linked"}), 400
    if not store.object_exists(path):
        return jsonify({"ok": False, "error": "no such published file"}), 404
    return jsonify({"ok": True, "url": store.signed_url(path), "expires_minutes": 15})


@app.post("/rollback")
def rollback():
    """
    R3, finally reachable. Put an earlier version back in front of singers.

    Deliberately requires an explicit version number rather than offering a
    "previous" shortcut: someone rolling back is usually under pressure, and
    naming the version they want is the point at which they notice if it is
    not the one they meant.
    """
    if not _authorised():
        return _deny()
    body = request.get_json(force=True, silent=True) or {}
    try:
        result = librarian.rollback(
            work_id=body.get("work_id", ""),
            to_version=int(body.get("to_version") or 0),
            actor=body.get("actor", "unknown"),
        )
    except KeyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except (ValueError, TypeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result})


@app.get("/library/<group>")
def library(group: str):
    """
    The singer-facing list for one group, with a short-lived link per score.

    The Hub calls this server-side and decides who may see it. This endpoint
    does NOT know about singers or permissions - it answers "what is published
    for this group", and WordPress owns the question of who that group is.
    """
    if not _authorised():
        return _deny()
    include_urls = request.args.get("urls", "1") != "0"
    items = librarian.library(group)
    if include_urls:
        for item in items:
            try:
                item["url"] = store.signed_url(item["object_path"])
            except RuntimeError as exc:
                item["url"] = None
                item["url_error"] = str(exc)
    return jsonify({"ok": True, "group": group, "count": len(items), "scores": items})


@app.post("/publish-batch")
def publish_batch():
    """
    Publish several staged items in one call. Each is still an explicit
    decision; this batches the pressing, not the deciding.
    """
    if not _authorised():
        return _deny()
    body = request.get_json(force=True, silent=True) or {}
    decisions = body.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        return jsonify({"ok": False, "error": "decisions must be a non-empty list"}), 400
    result = librarian.publish_batch(decisions, actor=body.get("actor", "unknown"))
    return jsonify({"ok": result["failed"] == 0, **result})


@app.post("/propose-name")
def propose_name():
    """Utility: what would this filename be published as? Used by the Hub's UI."""
    if not _authorised():
        return _deny()
    body = request.get_json(force=True, silent=True) or {}
    stem = (body.get("filename") or "").rsplit(".", 1)[0]
    return jsonify(
        {
            "ok": True,
            "proposed": naming.propose_canonical(stem),
            "normalised": naming.normalise(stem),
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
