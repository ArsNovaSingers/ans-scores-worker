"""
ans-scores-worker - HTTP surface.

claude/portal/Device_Sync_Spec.md. What exists here: the Drive walk, identity,
staging, publishing under frozen names, versioning, rollback, the group library
the Hub renders from, read-only WebDAV (Phase 5), and PDF optimisation (Phase 2).
Since v0.9.0 a staged PDF over 30 MB is optimised AUTOMATICALLY on intake
(Jonathan, 2026-10-22, reversing the 2026-08-25 ruling); /optimise/scan still
offers smaller copies of files published before that. What does not exist yet,
on purpose: quiet hours (Phase 6).

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

from . import dav, drive, fingerprint, librarian, media, naming, optimise, store, takes

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


# Practice takes (v0.8.0): signed upload/read URLs for a separate private bucket.
takes.register(app, _authorised, _deny)


def _project_from_path(rel_path: list[str], prefix: str = "") -> str:
    """
    The mirror "project" a file is published under.

    Without a prefix this is the path inside the scanned folder, which is how
    every project published before v0.7.0 was named - and those names are
    frozen (R2), so the rule is unchanged for them.

    WITH a prefix (v0.7.0) every path is placed under it: `Darkness & Light`,
    `Darkness & Light/Click Tracks`. Two concerts of one ensemble can then
    both have a `Click Tracks` folder without showing each other's files,
    which the unprefixed scheme could not promise - both would have been
    `ans/Click Tracks`.
    """
    parts = [p for p in rel_path if p.strip().lower() not in CONTAINER_SEGMENTS]
    if prefix:
        return "/".join([prefix] + parts)
    return "/".join(parts) if parts else "_root"


def _folder_from_path(rel_path: list[str], prefix: str = "") -> str:
    """
    The Drive folder a file really sits in, with NOTHING collapsed (v0.9.1).

    _project_from_path() drops storage-sounding folders ("PDFs") because the
    published path is built from it and those paths are frozen (R2) - so it
    cannot change. But the Hub names its headings after the real folder
    (Jonathan, 2026-10-22: "PDFs" shows "PDFs"), so the real folder travels
    separately: on staged items, on works as `folder`, and in folders_in_scan.
    """
    parts = [p for p in rel_path if p.strip()]
    if prefix:
        return "/".join([prefix] + parts)
    return "/".join(parts) if parts else "_root"


def _clean_prefix(raw) -> str:
    """A prefix is a folder name in an object path: no slashes at the ends, no dot segments."""
    parts = [p.strip() for p in str(raw or "").replace("\\", "/").split("/")]
    parts = [p for p in parts if p and p not in (".", "..")]
    return "/".join(parts)


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "ans-scores-worker", "version": "0.9.2"})


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
    if (item.get("inspected") or {}).get("media", media.PDF) != media.PDF:
        return jsonify({"ok": False, "error": "only a PDF can be optimised"}), 400

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
        # v0.9.0: remember what the ORIGINAL was, so a later scan of the
        # unchanged Drive file still recognises it after the swap.
        entry.setdefault("source_sha", entry.get("content_sha"))
        entry.setdefault("source_edition_key", (entry.get("inspected") or {}).get("edition_key"))
        entry.setdefault("original_size", entry.get("source_size"))
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

    Since v0.9.0 NEW files are optimised automatically on intake (Jonathan,
    2026-10-22, reversing the 2026-08-25 ruling that optimisation is never
    automatic - see /scan). This route is for what was published before that:
    it walks a group's published works, builds a candidate for each, and parks
    the ones worth looking at in the SAME staging queue that Drive intake
    uses - so approving one is the ordinary
    `POST /publish {decision: "new_edition"}` and inherits everything that
    already guards a publish: R3 ordering, the section 3.4 page-count gate, the
    version history, and rollback.

    Reusing that queue rather than building a parallel one is the whole design.
    A second approval mechanism would be a second place for "is this safe to
    publish" to be answered, and the two would drift.

    Body: {group, project?, limit?, work_ids?}
    """
    if not _authorised():
        return _deny()

    body = request.get_json(silent=True) or {}
    group = str(body.get("group") or "").strip()
    only_project = str(body.get("project") or "").strip()
    only_works = {str(w) for w in (body.get("work_ids") or []) if str(w).strip()}
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
        if only_works and work_id not in only_works:
            continue
        if not work.get("versions"):
            continue
        if work.get("media", media.PDF) != media.PDF:
            continue  # optimisation is a PDF operation
        if work_id in pending_works:
            results.append({"canonical": work["canonical"], "outcome": "already in the queue"})
            continue
        if examined >= limit:
            results.append({"canonical": work["canonical"], "outcome": "not examined this run"})
            continue
        examined += 1

        ppath = store.work_published_path(work)
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

    Scores and, since v0.6.0, recordings. Everything else in the folder is
    counted and reported, never silently dropped.

    Never publishes on its own account. The caller may ask for two narrow kinds
    of publish to happen in the same call, and nothing else:

        auto_publish.audio              a recording whose identity is certain -
                                        new, or the same Drive file with new
                                        bytes. There is no annotation layer on
                                        audio for a wrong call to damage.
        auto_publish.new_work_projects  a PDF in one of these project folders
                                        that matches NOTHING already published.
                                        Meant for rehearsal notes: a new dated
                                        note is always a new work, and waiting
                                        for a human is why the 9/10 note never
                                        reached anyone.

    A PDF that might be an edition of an existing score is ALWAYS left for a
    human. R4 is about that case, and it is untouched.

    v0.9.0 (HUB-52): a PDF over 30 MB is made smaller HERE, before it is
    staged, on every path that scans - the Hub button, the project screen,
    REST. Same optimise.assess() and the same verification gates as the
    Optimize button; the original stays untouched in Drive and its hash and
    fingerprint are kept on the item, so the unchanged original is recognised
    on the next scan. Approve still decides what publishes.

    A file that cannot be stored is recorded with state "error" and reported
    as outcome "store_failed", instead of a 500 that loses the whole scan.
    """
    if not _authorised():
        return _deny()

    body = request.get_json(force=True, silent=True) or {}
    group = (body.get("group") or "").strip()
    folder_id = (body.get("folder_id") or "").strip()
    limit = int(body.get("limit") or DEFAULT_SCAN_LIMIT)
    auto = body.get("auto_publish") or {}
    auto_audio = bool(auto.get("audio"))
    auto_projects = {
        str(p).strip().lower() for p in (auto.get("new_work_projects") or []) if str(p).strip()
    }
    actor = str(body.get("actor") or "scan")
    prefix = _clean_prefix(body.get("project_prefix"))
    if not group or not folder_id:
        return jsonify({"ok": False, "error": "group and folder_id are required"}), 400

    started = time.time()
    svc = drive.service()
    try:
        files = drive.walk(folder_id, svc)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": "drive walk failed", "detail": str(exc)}), 502

    pdfs, audio, ignored = [], [], []
    for f in files:
        kind = media.classify(f)
        if kind == media.PDF:
            pdfs.append(f)
        elif kind == media.AUDIO:
            audio.append(f)
        else:
            ignored.append(f)
    audio_all_ids = {f["id"] for f in audio}
    # Every mirror folder this scan can publish into, changed or not. The Hub
    # reads this to show new subfolders without anybody typing their names.
    # v0.9.1: both the publishing name and the real folder, so an address the
    # Hub already knows keeps matching and the real one ("…/PDFs") appears.
    folders_in_scan = sorted(
        {_project_from_path(f.get("rel_path", []), prefix) for f in pdfs + audio}
        | {_folder_from_path(f.get("rel_path", []), prefix) for f in pdfs + audio}
    )
    audio, lossless_skipped = media.prefer_compressed(audio)
    seen_ids = {f["id"] for f in pdfs} | audio_all_ids

    cursors, cursor_gen = store.read_json(store.CURSORS_PATH, {"files": {}})
    cursors.setdefault("files", {})
    registry, _gen = librarian.load_registry()
    staging, _sgen = librarian.load_staging()
    already_pending = {
        item["source_file_id"]
        for item in staging["items"].values()
        if item.get("state") == "pending"
    }
    # A file that failed to store last time is tried again; its old error
    # item is closed as superseded when the new attempt is recorded.
    failed_before = {
        item["source_file_id"]: item["staging_id"]
        for item in staging["items"].values()
        if item.get("state") == "error"
    }

    results = []
    counters = {"unchanged": 0, "examined": 0, "remaining": 0}

    # v0.7.1 - a file MOVED in Drive keeps its file id, bytes and modified
    # time, so the cursor below skips it and nothing would ever notice. Look
    # for moves first, for every walked file. The published object does not
    # move (R2 - a singer's device holds that path); the work records the
    # folder it now sits in, and the Hub groups it by that.
    # v0.9.2: a work is traced to the Drive file of its newest version that
    # CAME from Drive. An edition made by /optimise/scan records its source as
    # "optimise:<work>", and keying on the current version alone lost the
    # Drive link for every optimised score - on 2026-10-22 a folder rename then
    # left five scores unmoved and off the Hub until this was fixed.
    by_source = {}
    for work in registry["works"].values():
        if work["group"] != group or not work["versions"]:
            continue
        for v in sorted(work["versions"], key=lambda x: x["n"], reverse=True):
            sid = v.get("source_file_id") or ""
            if sid and not sid.startswith("optimise:"):
                by_source.setdefault(sid, work)
                break
    moves = {}
    for f in pdfs + audio:
        work = by_source.get(f["id"])
        if work is None:
            continue
        now = _folder_from_path(f.get("rel_path", []), prefix)
        if now != work.get("folder", work["project"]):
            moves[work["work_id"]] = now
    moved = librarian.record_folders(moves) if moves else []

    def _same_file_same_bytes(f, sha) -> bool:
        """The very file already published, unchanged - wherever it now sits."""
        work = by_source.get(f["id"])
        if work is None:
            return False
        # Any version taken from this very file with these bytes - including
        # the original an optimised edition now stands in for (v0.9.2).
        return any(
            v.get("source_file_id") == f["id"] and sha in (v.get("content_sha"), v.get("source_sha"))
            for v in work["versions"]
        )

    def _skip(f) -> bool:
        cached = cursors["files"].get(f["id"])
        if (
            cached
            and cached.get("md5") == f.get("md5Checksum")
            and cached.get("modifiedTime") == f.get("modifiedTime")
        ):
            counters["unchanged"] += 1
            return True
        if f["id"] in already_pending:
            counters["unchanged"] += 1
            return True
        if counters["examined"] >= limit:
            counters["remaining"] += 1
            return True
        counters["examined"] += 1
        return False

    def _remember(f, sha):
        cursors["files"][f["id"]] = {
            "md5": f.get("md5Checksum"),
            "modifiedTime": f.get("modifiedTime"),
            "content_sha": sha,
        }

    def _stage(entry, sha, inspected, proposal, local, ext, extra=None):
        """
        Store the file FIRST, then record the item (v0.9.0). If storing fails
        the item is recorded with state "error" and the reason, and the scan
        carries on: returns (staging_id, error or "").
        """
        staging_id = librarian.new_staging_id()
        spath = store.staging_path(staging_id, ext)
        record = {
            **entry,
            **(extra or {}),
            "content_sha": sha,
            "inspected": inspected,
            "proposal": proposal,
            "staging_path": spath,
        }
        error = ""
        try:
            store.upload_file(
                local, spath, {"staging_id": staging_id, "sha256": sha},
                content_type=media.content_type(ext),
            )
        except Exception as exc:  # noqa: BLE001 - reported on the item, never lost
            error = "could not store the file: {}".format(exc)
            record["staging_path"] = ""
            record["error"] = error
        librarian.stage(record, staging_id=staging_id, state="error" if error else "pending")
        previous = failed_before.get(entry["source_file_id"])
        if previous:
            try:
                librarian._close_staging(previous, "superseded", actor, {"superseded_by": staging_id})
            except (KeyError, RuntimeError):
                pass
        return staging_id, error

    def _auto(staging_id, proposal, row):
        try:
            done = librarian.publish(
                staging_id=staging_id,
                decision=proposal["decision"],
                work_id=proposal.get("work_id"),
                canonical=proposal.get("proposed_canonical"),
                actor=actor,
            )
        except Exception as exc:  # noqa: BLE001 - one bad publish must not end the scan
            row["outcome"] = "staged"
            row["auto_publish_failed"] = str(exc)
            return
        row["outcome"] = "published"
        row["published"] = done

    for f in pdfs:
        if _skip(f):
            continue
        project = _project_from_path(f.get("rel_path", []), prefix)
        entry = {
            "group": group,
            "project": project,
            "folder": _folder_from_path(f.get("rel_path", []), prefix),
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
            if _same_file_same_bytes(f, sha):
                _remember(f, sha)
                results.append({**entry, "outcome": "duplicate",
                                "why": "the same Drive file, unchanged"})
                continue
            proposal = librarian.match(
                inspected, sha, f["name"], group, project, registry
            )

            if proposal["decision"] == "duplicate":
                _remember(f, sha)
                results.append({**entry, "outcome": "duplicate", "why": proposal["why"]})
                continue

            # v0.9.0 (HUB-52): automatic optimisation on intake, over 30 MB.
            source_size = os.path.getsize(local)
            entry["source_size"] = source_size
            extra = {"source_sha": sha, "source_edition_key": inspected["edition_key"],
                     "original_size": source_size}
            staged_file, staged_sha, staged_inspected = local, sha, inspected
            if source_size > librarian.BIG_FILE_BYTES:
                smaller = os.path.join(tmp, "smaller.pdf")
                try:
                    report = optimise.assess(local, smaller)
                except Exception as exc:  # noqa: BLE001 - reported, and the original is staged
                    report = {"outcome": "optimise_failed: {}".format(exc), "verify": None,
                              "bytes_before": source_size, "bytes_after": None}
                usable = bool(
                    report.get("bytes_after")
                    and (report.get("verify") or {}).get("ok")
                    and report["bytes_after"] < source_size
                )
                if usable:
                    try:
                        staged_inspected = fingerprint.inspect(smaller)
                        staged_sha = fingerprint.content_sha(smaller)
                        staged_file = smaller
                    except ValueError as exc:
                        usable = False
                        report["outcome"] = "candidate unreadable: {}".format(exc)
                extra["optimisation"] = {
                    "automatic": True,
                    "applied_before_publishing": usable,
                    "outcome": report.get("outcome", ""),
                    "bytes_before": source_size,
                    "bytes_after": report.get("bytes_after") if usable else None,
                    "saved_bytes": (source_size - report["bytes_after"]) if usable else 0,
                    "saved_ratio": report.get("saved_ratio", 0.0) if usable else 0.0,
                    "target_dpi": optimise.TARGET_DPI,
                    "jpeg_quality": optimise.JPEG_QUALITY,
                    "images_reencoded": report.get("images_reencoded", 0),
                    "verify": report.get("verify"),
                }
                if usable:
                    entry["source_size"] = report["bytes_after"]

            staging_id, store_error = _stage(entry, staged_sha, staged_inspected, proposal,
                                             staged_file, "pdf", extra)
            row = {
                **entry,
                "media": media.PDF,
                "outcome": "store_failed" if store_error else "staged",
                "staging_id": staging_id,
                "pages": inspected["page_count"],
                "fingerprint": inspected["edition_method"],
                "proposal": proposal,
            }
            if store_error:
                row["detail"] = store_error
            if "optimisation" in extra:
                row["optimisation"] = {k: extra["optimisation"][k] for k in
                                       ("applied_before_publishing", "bytes_before", "bytes_after", "outcome")}
            if store_error:
                results.append(row)
                continue
            if proposal["decision"] == "new_work" and project.strip().lower() in auto_projects:
                _auto(staging_id, proposal, row)
                if row["outcome"] == "published":
                    _remember(f, sha)
                    registry, _gen = librarian.load_registry()
            results.append(row)

    for f in audio:
        if _skip(f):
            continue
        project = _project_from_path(f.get("rel_path", []), prefix)
        ext = media.ext_of(f["name"])
        entry = {
            "group": group,
            "project": project,
            "folder": _folder_from_path(f.get("rel_path", []), prefix),
            "source_file_id": f["id"],
            "source_name": f["name"],
            "source_size": int(f.get("size") or 0),
            "source_modified": f.get("modifiedTime"),
        }

        with tempfile.TemporaryDirectory() as tmp:
            local = os.path.join(tmp, "candidate." + ext)
            try:
                drive.download(f["id"], local, svc)
            except Exception as exc:  # noqa: BLE001
                results.append({**entry, "outcome": "download_failed", "detail": str(exc)})
                continue

            sha = fingerprint.content_sha(local)
            size = os.path.getsize(local)
            entry["source_size"] = size
            if _same_file_same_bytes(f, sha):
                _remember(f, sha)
                results.append({**entry, "media": media.AUDIO, "outcome": "duplicate",
                                "why": "the same Drive file, unchanged"})
                continue
            proposal = librarian.match_audio(sha, f["id"], f["name"], group, project, registry)

            if proposal["decision"] == "duplicate":
                _remember(f, sha)
                results.append({**entry, "media": media.AUDIO, "outcome": "duplicate",
                                "why": proposal["why"]})
                continue

            inspected = media.inspected_for_audio(ext, size, sha)
            staging_id, store_error = _stage(entry, sha, inspected, proposal, local, ext)
            row = {
                **entry,
                "media": media.AUDIO,
                "outcome": "store_failed" if store_error else "staged",
                "staging_id": staging_id,
                "proposal": proposal,
            }
            if store_error:
                row["detail"] = store_error
                results.append(row)
                continue
            if auto_audio and proposal["decision"] in ("new_work", "new_edition"):
                _auto(staging_id, proposal, row)
                if row["outcome"] == "published":
                    _remember(f, sha)
                    # The next file in this folder must see this one, or two
                    # uploads with one name would both claim it.
                    registry, _gen = librarian.load_registry()
            results.append(row)

    try:
        store.write_json(store.CURSORS_PATH, cursors, cursor_gen)
    except store.Conflict:
        pass  # another scan updated it; the cache is an optimisation, not truth

    # Only this scan's own folders can be judged: a file of ANOTHER concert in
    # the same group was never going to be seen by this walk.
    if prefix:
        def _in_scope(project: str) -> bool:
            return project == prefix or project.startswith(prefix + "/")
    else:
        scope = set(folders_in_scan)

        def _in_scope(project: str) -> bool:
            return project in scope
    flagged = librarian.mark_missing_at_source(group, seen_ids, _in_scope)

    return jsonify(
        {
            "ok": True,
            "group": group,
            "pdfs_in_folder": len(pdfs),
            "audio_in_folder": len(audio),
            "project_prefix": prefix,
            "folders_in_scan": folders_in_scan,
            "lossless_skipped": [f["name"] for f in lossless_skipped],
            "ignored": [
                {"name": f.get("name"), "mimeType": f.get("mimeType")} for f in ignored
            ],
            "unchanged_since_last_scan": counters["unchanged"],
            "examined": counters["examined"],
            "not_examined_this_run": counters["remaining"],
            "published_now": sum(1 for r in results if r.get("outcome") == "published"),
            "store_failed": sum(1 for r in results if r.get("outcome") == "store_failed"),
            "moved": moved,
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
            ppath = store.work_published_path(work)
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
            allow_large=bool(body.get("allow_large")),
        )
    except KeyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result})


@app.post("/works/merge")
def works_merge():
    """
    Two works that are one piece become one history (HUB-53). Dry run unless
    dry_run is false, and `current` must be said: "keep", "merge" or a number.

    Body: {keep, merge, current, dry_run?, actor?}
    """
    if not _authorised():
        return _deny()
    body = request.get_json(force=True, silent=True) or {}
    try:
        result = librarian.merge_works(
            keep=str(body.get("keep") or ""),
            merge=str(body.get("merge") or ""),
            current=str(body.get("current") or ""),
            actor=str(body.get("actor") or "unknown"),
            dry_run=body.get("dry_run", True) is not False,
        )
    except KeyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except (ValueError, RuntimeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result})


@app.post("/staging/resolve")
def staging_resolve():
    """
    Close waiting items whose exact file (same Drive id, same bytes) is
    already published (HUB-53). Dry run unless dry_run is false.

    Body: {group?, dry_run?, actor?}
    """
    if not _authorised():
        return _deny()
    body = request.get_json(force=True, silent=True) or {}
    return jsonify({"ok": True, **librarian.resolve_already_published(
        group=str(body.get("group") or ""),
        actor=str(body.get("actor") or "unknown"),
        dry_run=body.get("dry_run", True) is not False,
    )})


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
        ppath = store.work_published_path(work)
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
