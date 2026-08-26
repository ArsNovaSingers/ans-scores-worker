"""
The Librarian: decides what an incoming file IS, and publishes it under a name
that never changes.

The rule that governs everything below, from Device_Sync_Spec.md section 3.3:

    Only bytes and structure ever decide automatically.
    Names only ever propose.

A wrong auto-merge writes one piece's revision over another piece's identity,
and fifty singers' annotations land on the wrong music. Every path that is not
certain returns a question for a human instead of an answer.
"""

import time
import uuid

from . import fingerprint, naming, store

# A name this close, in the same project folder, is worth ASKING about.
# It is never enough to act on by itself.
NAME_ASK_THRESHOLD = 0.82


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_registry() -> tuple[dict, int]:
    reg, gen = store.read_json(store.REGISTRY_PATH, {"works": {}})
    reg.setdefault("works", {})
    return reg, gen


def load_staging() -> tuple[dict, int]:
    stg, gen = store.read_json(store.STAGING_PATH, {"items": {}})
    stg.setdefault("items", {})
    return stg, gen


def match(inspected: dict, content_sha: str, source_name: str, group: str,
          project: str, registry: dict) -> dict:
    """
    Work out what this file is. Returns a proposal, never a completed action.

    proposal.decision is one of:
        duplicate     bytes already published. Do nothing, tell nobody.
        new_edition   confident: same edition key, or same structure and a
                      near-identical name. Pre-answered yes, still confirmable.
        ask_edition   plausible but not certain. A question, with candidates.
        ambiguous     two works fit equally well. Always a question.
        new_work      nothing matched.
    """
    stem = source_name.rsplit(".", 1)[0]
    candidates = [
        w for w in registry["works"].values()
        if w["group"] == group and w["project"] == project
    ]

    # 1. Bytes. Certain, and the commonest case on a re-scan of a folder.
    for work in candidates:
        for version in work["versions"]:
            if version["content_sha"] == content_sha:
                return {
                    "decision": "duplicate",
                    "work_id": work["work_id"],
                    "canonical": work["canonical"],
                    "version": version["n"],
                    "why": "byte-identical to version {} already published".format(version["n"]),
                }

    # 2. Edition key. Same music on the pages, whatever the file is called.
    edition_hits = [
        w for w in candidates
        if any(v["edition_key"] == inspected["edition_key"] for v in w["versions"])
    ]
    if len(edition_hits) == 1:
        work = edition_hits[0]
        return {
            "decision": "new_edition",
            "work_id": work["work_id"],
            "canonical": work["canonical"],
            "confidence": "high",
            "why": "the pages match {} exactly ({} fingerprint), only the file differs".format(
                work["canonical"], inspected["edition_method"]
            ),
            "structure_change": not _structure_ok(inspected, work),
        }
    if len(edition_hits) > 1:
        return _ambiguous(edition_hits, "several works share these pages")

    # 3. Name similarity, scoped to the project folder. Proposes only.
    scored = sorted(
        (
            (naming.similarity(stem, w["canonical"]), w)
            for w in candidates
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    close = [(score, w) for score, w in scored if score >= NAME_ASK_THRESHOLD]

    if len(close) > 1 and abs(close[0][0] - close[1][0]) < 0.02:
        return _ambiguous([w for _s, w in close[:3]], "two works have equally similar names")

    if close:
        score, work = close[0]
        structure_ok = _structure_ok(inspected, work)
        return {
            "decision": "ask_edition",
            "work_id": work["work_id"],
            "canonical": work["canonical"],
            "confidence": "medium" if structure_ok else "low",
            "name_similarity": round(score, 3),
            "structure_change": not structure_ok,
            "why": (
                "the name is {}% the same as {} and it is in the same folder, "
                "but the pages do not match any version we hold"
            ).format(round(score * 100), work["canonical"]),
            "proposed_canonical": work["canonical"],
        }

    return {
        "decision": "new_work",
        "confidence": "n/a",
        "why": "nothing in this folder matches by bytes, by pages or by name",
        "proposed_canonical": naming.propose_canonical(stem),
    }


def _ambiguous(works: list[dict], why: str) -> dict:
    return {
        "decision": "ambiguous",
        "why": why,
        "candidates": [
            {"work_id": w["work_id"], "canonical": w["canonical"]} for w in works
        ],
    }


def _structure_ok(inspected: dict, work: dict) -> bool:
    current = next(v for v in work["versions"] if v["n"] == work["current"])
    return fingerprint.structure_matches(current["structure"], inspected)


def stage(entry: dict) -> str:
    """Park a decision for a human. Returns the staging id."""
    staging_id = uuid.uuid4().hex[:12]
    for _attempt in range(5):
        staging, gen = load_staging()
        entry = dict(entry)
        entry["staging_id"] = staging_id
        entry["staged_at"] = _now()
        entry["state"] = "pending"
        staging["items"][staging_id] = entry
        try:
            store.write_json(store.STAGING_PATH, staging, gen)
            return staging_id
        except store.Conflict:
            continue
    raise RuntimeError("staging is being written too fast to make progress")


def publish(staging_id: str, decision: str, work_id: str | None = None,
            canonical: str | None = None, accept_structure_change: bool = False,
            actor: str = "unknown") -> dict:
    """
    Turn a staged item into a published version.

    R3 order of operations, and it matters: the bytes go to _versions/ first
    and the published pointer is moved only after that succeeds. A failure
    halfway leaves the previous version serving rather than a half-written file.
    """
    staging, _gen = load_staging()
    item = staging["items"].get(staging_id)
    if item is None:
        raise KeyError("no staged item {}".format(staging_id))
    if item["state"] != "pending":
        raise ValueError("staged item {} is already {}".format(staging_id, item["state"]))

    registry, reg_gen = load_registry()

    if decision == "reject":
        return _close_staging(staging_id, "rejected", actor, {})

    if decision == "new_edition":
        if not work_id or work_id not in registry["works"]:
            raise ValueError("new_edition needs a work_id that exists")
        work = registry["works"][work_id]
        current = next(v for v in work["versions"] if v["n"] == work["current"])
        structure_ok = fingerprint.structure_matches(current["structure"], item["inspected"])
        if not structure_ok and not accept_structure_change:
            # Section 3.4. This is the one change that moves every singer's
            # markings, so it cannot happen as a side effect of pressing publish.
            raise ValueError(_structure_change_message(current["structure"], item["inspected"]))
        n = max(v["n"] for v in work["versions"]) + 1

    elif decision == "new_work":
        work_id = uuid.uuid4().hex[:12]
        proposed = canonical or item["proposal"].get("proposed_canonical")
        if not proposed:
            raise ValueError("new_work needs a canonical name")
        _assert_name_free(registry, item["group"], item["project"], proposed)
        work = {
            "work_id": work_id,
            "group": item["group"],
            "project": item["project"],
            "canonical": proposed,          # frozen from here. R2.
            "created": _now(),
            "versions": [],
            "current": 0,
            "source_missing": False,
        }
        registry["works"][work_id] = work
        n = 1
    else:
        raise ValueError("decision must be new_work, new_edition or reject")

    canonical_name = work["canonical"]
    vpath = store.version_path(work_id, n, canonical_name)
    ppath = store.published_path(item["group"], item["project"], canonical_name)

    meta = {
        "work_id": work_id,
        "version": n,
        "content_sha": item["content_sha"],
        "page_count": item["inspected"]["page_count"],
        "source_name": item["source_name"],
        "source_file_id": item["source_file_id"],
        "published_by": actor,
    }

    # 1. immutable version copy, taken from the staged object rather than local
    #    disk - the scan that produced it ran in a different request and may
    #    have run on a different instance.
    store.copy_object(item["staging_path"], vpath, meta)
    # 2. only now move what singers read
    store.copy_object(vpath, ppath, meta)

    work["versions"].append(
        {
            "n": n,
            "content_sha": item["content_sha"],
            "edition_key": item["inspected"]["edition_key"],
            "structure": {
                "page_count": item["inspected"]["page_count"],
                "page_dims": item["inspected"]["page_dims"],
            },
            "source_file_id": item["source_file_id"],
            "source_name": item["source_name"],
            "published_at": _now(),
            "published_by": actor,
            "object_path": vpath,
        }
    )
    work["current"] = n
    work["source_missing"] = False

    for _attempt in range(5):
        try:
            store.write_json(store.REGISTRY_PATH, registry, reg_gen)
            break
        except store.Conflict:
            registry, reg_gen = load_registry()
            registry["works"][work_id] = work
    else:
        raise RuntimeError("registry is being written too fast to make progress")

    _close_staging(staging_id, "published", actor, {"work_id": work_id, "version": n})

    return {
        "work_id": work_id,
        "canonical": canonical_name,
        "version": n,
        "published_path": ppath,
        "version_path": vpath,
    }


def _structure_change_message(published: dict, incoming: dict) -> str:
    """
    Say what moved, not that something moved. A person deciding whether to
    override this needs to know it is one cropped page rather than a reformat.
    """
    was, now = published["page_count"], incoming["page_count"]
    if was != now:
        detail = "the page count changed from {} to {}".format(was, now)
    else:
        moved = fingerprint.dim_differences(published["page_dims"], incoming["page_dims"])
        shown = ", ".join(
            "page {} {}x{} to {}x{}".format(n, b[0], b[1], a[0], a[1])
            for n, b, a in moved[:4]
        )
        if len(moved) > 4:
            shown += " and {} more".format(len(moved) - 4)
        detail = "{} of {} pages changed size ({})".format(len(moved), now, shown)
    return (
        "Refusing to replace the published version: {}. Singers' markings are "
        "positioned per page and will not line up. Publish this as a new piece, "
        "or repeat with accept_structure_change=true and tell the choir what "
        "changed.".format(detail)
    )


def _assert_name_free(registry: dict, group: str, project: str, canonical: str) -> None:
    for work in registry["works"].values():
        if work["group"] == group and work["project"] == project and work["canonical"] == canonical:
            raise ValueError(
                "{} is already the published name of another work in this folder. "
                "Publish this as a new edition of it, or give it a different name.".format(canonical)
            )


def _close_staging(staging_id: str, state: str, actor: str, extra: dict) -> dict:
    for _attempt in range(5):
        staging, gen = load_staging()
        item = staging["items"].get(staging_id)
        if item is None:
            raise KeyError(staging_id)
        item["state"] = state
        item["closed_at"] = _now()
        item["closed_by"] = actor
        item.update(extra)
        try:
            store.write_json(store.STAGING_PATH, staging, gen)
            return item
        except store.Conflict:
            continue
    raise RuntimeError("staging is being written too fast to make progress")


def mark_missing_at_source(group: str, seen_file_ids: set[str]) -> list[dict]:
    """
    R1. A file gone from Drive is never removed from the mirror - it is
    flagged, and the flag is what Tom sees. Nothing about a singer's device
    changes, and nothing about what the Hub serves changes either.
    """
    flagged = []
    for _attempt in range(5):
        registry, gen = load_registry()
        changed = False
        for work in registry["works"].values():
            if work["group"] != group:
                continue
            current = next((v for v in work["versions"] if v["n"] == work["current"]), None)
            if current is None:
                continue
            missing = current["source_file_id"] not in seen_file_ids
            if missing != work.get("source_missing", False):
                work["source_missing"] = missing
                changed = True
            if missing:
                flagged.append({"work_id": work["work_id"], "canonical": work["canonical"]})
        if not changed:
            return flagged
        try:
            store.write_json(store.REGISTRY_PATH, registry, gen)
            return flagged
        except store.Conflict:
            flagged = []
            continue
    raise RuntimeError("registry is being written too fast to make progress")


def rollback(work_id: str, to_version: int, actor: str = "unknown") -> dict:
    """
    Put an earlier version back in front of singers.

    The spec promised this in R3 - "rollback is a pointer change, not a
    restore" - and every version has been stored since day one to make it
    possible. Nothing exposed it until now, which meant the promise was true
    of the data and false of the system. Found by testing rather than review.

    Nothing is deleted and no version is renumbered. The bytes of the target
    version are copied back over the published path, and current moves. The
    version that WAS current stays exactly where it is, so a rollback can
    itself be rolled back.
    """
    registry, gen = load_registry()
    work = registry["works"].get(work_id)
    if work is None:
        raise KeyError("no work {}".format(work_id))

    target = next((v for v in work["versions"] if v["n"] == to_version), None)
    if target is None:
        have = ", ".join(str(v["n"]) for v in work["versions"])
        raise ValueError(
            "{} has no version {}. Versions on file: {}".format(
                work["canonical"], to_version, have
            )
        )
    if work["current"] == to_version:
        raise ValueError(
            "version {} is already the one singers are getting".format(to_version)
        )

    was = work["current"]
    ppath = store.published_path(work["group"], work["project"], work["canonical"])
    store.copy_object(
        target["object_path"],
        ppath,
        {
            "work_id": work_id,
            "version": to_version,
            "content_sha": target["content_sha"],
            "page_count": target["structure"]["page_count"],
            "rolled_back_from": was,
            "published_by": actor,
        },
    )

    work["current"] = to_version
    work.setdefault("rollbacks", []).append(
        {"from": was, "to": to_version, "at": _now(), "by": actor}
    )

    for _attempt in range(5):
        try:
            store.write_json(store.REGISTRY_PATH, registry, gen)
            break
        except store.Conflict:
            registry, gen = load_registry()
            registry["works"][work_id] = work
    else:
        raise RuntimeError("registry is being written too fast to make progress")

    return {
        "work_id": work_id,
        "canonical": work["canonical"],
        "now_serving": to_version,
        "was_serving": was,
        "published_path": ppath,
    }


def library(group: str) -> list[dict]:
    """
    What a singer in this group can actually be given, grouped by project.

    Deliberately NOT the registry dump. This is the singer-facing shape: the
    name they will see, how many pages, and when it last changed - which is
    what tells them whether the copy on their iPad is still the current one.
    """
    registry, _gen = load_registry()
    out = []
    for work in registry["works"].values():
        if work["group"] != group or not work["versions"]:
            continue
        current = next(v for v in work["versions"] if v["n"] == work["current"])
        out.append(
            {
                "work_id": work["work_id"],
                "canonical": work["canonical"],
                "project": work["project"],
                "pages": current["structure"]["page_count"],
                "version": current["n"],
                "published_at": current["published_at"],
                "revised": current["n"] > 1,
                "source_missing": work.get("source_missing", False),
                "object_path": store.published_path(
                    work["group"], work["project"], work["canonical"]
                ),
            }
        )
    out.sort(key=lambda w: (w["project"], w["canonical"]))
    return out


def publish_batch(decisions: list[dict], actor: str = "unknown") -> dict:
    """
    Publish several staged items in one call.

    Exists because the alternative is a human making the same decision eleven
    times, and a queue that is tedious to clear is a queue that does not get
    cleared. Each item is still an explicit decision - this batches the
    pressing, not the deciding.

    One item failing never stops the rest: every result is reported
    individually, so a page-structure refusal on one score does not silently
    take four others down with it.
    """
    results = []
    for item in decisions:
        staging_id = item.get("staging_id", "")
        try:
            outcome = publish(
                staging_id=staging_id,
                decision=item.get("decision", ""),
                work_id=item.get("work_id"),
                canonical=item.get("canonical"),
                accept_structure_change=bool(item.get("accept_structure_change")),
                actor=actor,
            )
            results.append({"staging_id": staging_id, "ok": True, **outcome})
        except (KeyError, ValueError) as exc:
            results.append({"staging_id": staging_id, "ok": False, "error": str(exc)})
    published = sum(1 for r in results if r["ok"])
    return {
        "published": published,
        "failed": len(results) - published,
        "results": results,
    }
