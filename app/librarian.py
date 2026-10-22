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

from . import fingerprint, media, naming, store

# A name this close, in the same project folder, is worth ASKING about.
# It is never enough to act on by itself.
NAME_ASK_THRESHOLD = 0.82

# v0.9.0 (HUB-52). A PDF above this is optimised on intake and refused at
# publish unless allow_large is passed. 30 MB, because Cloud Run will not serve
# a single response over 32 MiB, so a bigger score cannot be synced whole.
BIG_FILE_BYTES = 30 * 1024 * 1024


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


def effective(work: dict, registry: dict) -> dict:
    """
    The work whose versions a work serves (v0.9.0).

    An ALIAS is a work in a second project that shares another work's music:
    the same piece used by two concerts, or two works that turned out to be
    one piece and were merged. It keeps its own frozen published path (R2 -
    singers' devices hold it) but has no versions of its own; every version
    lands on the work it points at and is copied to the alias's path too. So
    there is one version history, and approving an update from either
    project updates both.
    """
    target = work.get("alias_of")
    if target and target in registry["works"]:
        return registry["works"][target]
    return work


def aliases_of(work_id: str, registry: dict) -> list[dict]:
    return [w for w in registry["works"].values() if w.get("alias_of") == work_id]


def _version_matches_bytes(version: dict, sha: str) -> bool:
    """The published bytes, or (v0.9.0) the original they were optimised from."""
    return bool(sha) and sha in (version.get("content_sha"), version.get("source_sha"))


def _version_matches_edition(version: dict, key: str) -> bool:
    return bool(key) and key in (version.get("edition_key"), version.get("source_edition_key"))


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
    # v0.9.0: compared with the original too, so a file optimised on the way
    # in is still recognised when the unchanged original is scanned again.
    for work in candidates:
        for version in effective(work, registry)["versions"]:
            if _version_matches_bytes(version, content_sha):
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
        if any(_version_matches_edition(v, inspected["edition_key"])
               for v in effective(w, registry)["versions"])
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

    # 2b. v0.9.0 (HUB-53). The same score already published for ANOTHER
    # project of this group. Before this, a new concert re-using a piece minted
    # a second work for it ("Bernstein Somewhere" twice), and an update to one
    # never reached the other. Bytes or pages, never a name - and still only a
    # proposal: approving it links this project to the existing piece.
    elsewhere = []
    for work in registry["works"].values():
        if work["group"] != group or work["project"] == project or work.get("alias_of"):
            continue
        if not work["versions"]:
            continue
        hit_bytes = any(_version_matches_bytes(v, content_sha) for v in work["versions"])
        hit_pages = any(_version_matches_edition(v, inspected["edition_key"]) for v in work["versions"])
        if hit_bytes or hit_pages:
            elsewhere.append((hit_bytes, work))
    if len(elsewhere) == 1:
        same_bytes, work = elsewhere[0]
        return {
            "decision": "link_existing",
            "work_id": work["work_id"],
            "canonical": work["canonical"],
            "confidence": "certain" if same_bytes else "high",
            "why": "this score is already published as {} in {} ({}). Linking it here makes "
                   "the two projects share one piece and one version history.".format(
                       work["canonical"], work["project"],
                       "the same bytes" if same_bytes else "the same pages"),
            "proposed_canonical": work["canonical"],
            "structure_change": not _structure_ok(inspected, work),
        }
    if len(elsewhere) > 1:
        return _ambiguous([w for _b, w in elsewhere], "this score is published in several projects")

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


def match_audio(content_sha: str, source_file_id: str, source_name: str,
                group: str, project: str, registry: dict) -> dict:
    """
    Identity for a recording. See media.py for why this is not match().

    Bytes first, then the Drive file it came from. Never the name.
    """
    candidates = [
        w for w in registry["works"].values()
        if w["group"] == group and w.get("media") == media.AUDIO
    ]

    for work in candidates:
        if work["project"] != project:
            continue
        for version in work["versions"]:
            if version["content_sha"] == content_sha:
                return {
                    "decision": "duplicate",
                    "work_id": work["work_id"],
                    "canonical": work["canonical"],
                    "version": version["n"],
                    "why": "byte-identical to version {} already published".format(version["n"]),
                }

    same_file = [
        w for w in candidates
        if any(v.get("source_file_id") == source_file_id for v in w["versions"])
    ]
    if len(same_file) == 1:
        work = same_file[0]
        return {
            "decision": "new_edition",
            "work_id": work["work_id"],
            "canonical": work["canonical"],
            "confidence": "certain",
            "why": "the same Drive file as {}, with new audio in it".format(work["canonical"]),
            "structure_change": False,
        }
    if len(same_file) > 1:
        return _ambiguous(same_file, "several recordings came from this Drive file")

    proposed = media.audio_canonical(media.stem_of(source_name))
    taken = {
        w["canonical"] for w in registry["works"].values()
        if w["group"] == group and w["project"] == project
    }
    base, n = proposed, 2
    while proposed in taken:
        proposed = "{} ({})".format(base, n)
        n += 1

    return {
        "decision": "new_work",
        "confidence": "certain",
        "why": "a recording this folder has not published before",
        "proposed_canonical": proposed,
    }


def _structure_ok(inspected: dict, work: dict) -> bool:
    current = next((v for v in work["versions"] if v["n"] == work["current"]), None)
    if current is None:
        return True  # an alias: the caller compares against the work it points at
    return fingerprint.structure_matches(current["structure"], inspected)


def new_staging_id() -> str:
    return uuid.uuid4().hex[:12]


def stage(entry: dict, staging_id: str | None = None, state: str = "pending") -> str:
    """
    Park a decision for a human. Returns the staging id.

    v0.9.0: the caller may choose the id, so the file is stored BEFORE the
    item is recorded - an item is never written with an empty staging_path
    again (the 140.7 MB Ligeti case). state "error" records a file that could
    not be stored, so a person sees it instead of a silent 500.
    """
    staging_id = staging_id or new_staging_id()
    for _attempt in range(5):
        staging, gen = load_staging()
        entry = dict(entry)
        entry["staging_id"] = staging_id
        entry["staged_at"] = _now()
        entry["state"] = state
        staging["items"][staging_id] = entry
        try:
            store.write_json(store.STAGING_PATH, staging, gen)
            return staging_id
        except store.Conflict:
            continue
    raise RuntimeError("staging is being written too fast to make progress")


def publish(staging_id: str, decision: str, work_id: str | None = None,
            canonical: str | None = None, accept_structure_change: bool = False,
            actor: str = "unknown", allow_large: bool = False) -> dict:
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

    # v0.9.0 (HUB-52). Refuse to send singers a PDF over the ceiling. Every
    # path comes through here - the project screen, REST, a batch, a scan's
    # auto-publish - so this is the one place the rule cannot be skipped.
    item_media = item["inspected"].get("media", media.PDF)
    if item_media == media.PDF and int(item.get("source_size") or 0) > BIG_FILE_BYTES and not allow_large:
        raise ValueError(
            "Refusing to publish {}: it is {:.1f} MB, over the {} MB ceiling. Optimise it first "
            "(POST /optimise/staged), or repeat with allow_large=true to publish it as it is.".format(
                item["source_name"], int(item["source_size"]) / 1048576.0, BIG_FILE_BYTES // 1048576)
        )

    # v0.9.0 (HUB-53). A file already published must not be published again
    # as something new - a second click on a recording that auto-publish had
    # already placed would have minted a duplicate work.
    if decision in ("new_work", "link_existing"):
        for other in registry["works"].values():
            if other["group"] != item["group"]:
                continue
            for v in other["versions"]:
                if v.get("source_file_id") == item["source_file_id"] and (
                        _version_matches_bytes(v, item["content_sha"])
                        or _version_matches_bytes(v, item.get("source_sha", ""))):
                    raise ValueError(
                        "{} is already published as {} (version {}). Nothing to add.".format(
                            item["source_name"], other["canonical"], v["n"])
                    )

    if decision == "link_existing":
        return _link_existing(item, staging_id, work_id, canonical, actor, registry, reg_gen)

    if decision == "new_edition":
        if not work_id or work_id not in registry["works"]:
            raise ValueError("new_edition needs a work_id that exists")
        # An update proposed against an alias lands on the work it points at.
        work = effective(registry["works"][work_id], registry)
        work_id = work["work_id"]
        if any(v["content_sha"] == item["content_sha"] for v in work["versions"]):
            raise ValueError("{} already has these exact bytes. Nothing to add.".format(work["canonical"]))
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
        item_media = item["inspected"].get("media", media.PDF)
        work = {
            "work_id": work_id,
            "group": item["group"],
            "project": item["project"],
            "media": item_media,
            "ext": item["inspected"].get("ext", "pdf"),
            "canonical": proposed,          # frozen from here. R2.
            "created": _now(),
            "versions": [],
            "current": 0,
            "source_missing": False,
        }
        # v0.9.1: the real Drive folder, when it differs from the publishing name.
        if item.get("folder") and item["folder"] != item["project"]:
            work["folder"] = item["folder"]
        registry["works"][work_id] = work
        n = 1
    else:
        raise ValueError("decision must be new_work, new_edition, link_existing or reject")

    item_ext = item["inspected"].get("ext", "pdf")
    if item_ext != store.work_ext(work):
        # A published name is a filename. Swapping an mp3 for an m4a under
        # it would leave the old file serving at the old path with nobody
        # told, so a change of format is a new work, never a new version.
        raise ValueError(
            "{} is published as .{}; this file is .{}. Publish it as a new work.".format(
                work["canonical"], store.work_ext(work), item_ext
            )
        )

    canonical_name = work["canonical"]
    vpath = store.version_path(work_id, n, canonical_name, item_ext)
    ppath = store.published_path(item["group"], item["project"], canonical_name, item_ext)

    meta = {
        "work_id": work_id,
        "version": n,
        "content_sha": item["content_sha"],
        "page_count": item["inspected"]["page_count"],
        "source_name": item["source_name"],
        "source_file_id": item["source_file_id"],
        "published_by": actor,
    }

    if not item.get("staging_path") or not store.object_exists(item["staging_path"]):
        raise ValueError("the staged file for {} is missing, so there is nothing to publish".format(
            item["source_name"]))

    # 1. immutable version copy, taken from the staged object rather than local
    #    disk - the scan that produced it ran in a different request and may
    #    have run on a different instance.
    store.copy_object(item["staging_path"], vpath, meta)
    # 2. only now move what singers read - the work's own path, and every
    #    project that shares the piece (v0.9.0 aliases).
    store.copy_object(vpath, ppath, meta)
    for alias in aliases_of(work_id, registry):
        store.copy_object(vpath, store.work_published_path(alias), meta)

    work["versions"].append(
        {
            "n": n,
            "content_sha": item["content_sha"],
            "edition_key": item["inspected"]["edition_key"],
            "size": int(item.get("source_size") or 0),
            "structure": {
                "page_count": item["inspected"]["page_count"],
                "page_dims": item["inspected"]["page_dims"],
            },
            "source_file_id": item["source_file_id"],
            "source_name": item["source_name"],
            # v0.9.0: the original's identity when the published bytes were
            # optimised on the way in, so re-scanning the original matches.
            "source_sha": item.get("source_sha") or item["content_sha"],
            "source_edition_key": item.get("source_edition_key") or item["inspected"]["edition_key"],
            "optimised": bool((item.get("optimisation") or {}).get("applied_before_publishing")),
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


def _link_existing(item: dict, staging_id: str, work_id: str | None, canonical: str | None,
                   actor: str, registry: dict, reg_gen: int) -> dict:
    """
    Publish a staged file as this project's copy of a piece another project
    already has (v0.9.0). Creates an ALIAS work in this project's folder - its
    own frozen published path, no versions of its own - and puts the shared
    piece's current bytes there. The staged file itself is not published: it
    was matched as the same music, and the shared version history decides.
    """
    if not work_id or work_id not in registry["works"]:
        raise ValueError("link_existing needs the work_id of the piece to share")
    target = effective(registry["works"][work_id], registry)
    if target["group"] != item["group"]:
        raise ValueError("a piece can only be shared within one group")
    name = canonical or (item.get("proposal") or {}).get("proposed_canonical") or target["canonical"]
    _assert_name_free(registry, item["group"], item["project"], name)
    current = next(v for v in target["versions"] if v["n"] == target["current"])

    alias_id = uuid.uuid4().hex[:12]
    alias = {
        "work_id": alias_id,
        "group": item["group"],
        "project": item["project"],
        "media": target.get("media", media.PDF),
        "ext": store.work_ext(target),
        "canonical": name,
        "created": _now(),
        "versions": [],
        "current": 0,
        "alias_of": target["work_id"],
        "source_missing": False,
        # v0.9.3: the alias's own Drive file, so a folder rename moves it.
        "source_file_id": item["source_file_id"],
    }
    if item.get("folder") and item["folder"] != item["project"]:
        alias["folder"] = item["folder"]
    ppath = store.work_published_path(alias)
    store.copy_object(current["object_path"], ppath, {
        "work_id": target["work_id"], "alias": alias_id, "version": current["n"],
        "content_sha": current["content_sha"], "published_by": actor,
    })
    registry["works"][alias_id] = alias
    for _attempt in range(5):
        try:
            store.write_json(store.REGISTRY_PATH, registry, reg_gen)
            break
        except store.Conflict:
            registry, reg_gen = load_registry()
            registry["works"][alias_id] = alias
    else:
        raise RuntimeError("registry is being written too fast to make progress")
    _close_staging(staging_id, "published", actor, {"work_id": alias_id, "alias_of": target["work_id"]})
    return {
        "work_id": alias_id,
        "alias_of": target["work_id"],
        "canonical": name,
        "version": current["n"],
        "published_path": ppath,
    }


def merge_works(keep: str, merge: str, current: str = "", actor: str = "unknown",
                dry_run: bool = True) -> dict:
    """
    Two works that are one piece (HUB-53: "Bernstein Somewhere" was published
    twice). The work `keep` keeps its id and gains `merge`'s versions,
    renumbered after its own; `merge` becomes an alias of it, so its frozen
    published path still serves - every singer's device and every project
    map that holds either id keeps working - and from now on both paths get
    every new version.

    Which version is current is a person's decision, never inferred:
    current = "keep" (keep's current stays), "merge" (merge's current
    becomes current), or a version number in the merged history.

    Nothing is deleted (R1). Version objects stay where they were written.
    """
    registry, gen = load_registry()
    a = registry["works"].get(keep)
    b = registry["works"].get(merge)
    if a is None or b is None:
        raise KeyError("no such work: {}".format(keep if a is None else merge))
    if keep == merge:
        raise ValueError("keep and merge are the same work")
    if a.get("alias_of") or b.get("alias_of"):
        raise ValueError("merge two works, not an alias")
    if a["group"] != b["group"]:
        raise ValueError("works in different groups cannot be merged")
    if store.work_ext(a) != store.work_ext(b):
        raise ValueError("a .{} cannot be merged with a .{}".format(store.work_ext(a), store.work_ext(b)))
    if current not in ("keep", "merge") and not str(current).isdigit():
        raise ValueError("say which version becomes current: current='keep', 'merge', or a number")

    base = max([v["n"] for v in a["versions"]] or [0])
    moved = []
    b_current_new = 0
    for v in sorted(b["versions"], key=lambda x: x["n"]):
        nv = dict(v)
        nv["n"] = base + len(moved) + 1
        nv["merged_from"] = {"work_id": merge, "n": v["n"]}
        if v["n"] == b["current"]:
            b_current_new = nv["n"]
        moved.append(nv)

    history = a["versions"] + moved
    if current == "keep":
        new_current = a["current"]
    elif current == "merge":
        new_current = b_current_new
    else:
        new_current = int(current)
    chosen = next((v for v in history if v["n"] == new_current), None)
    if chosen is None:
        raise ValueError("version {} does not exist after the merge; versions: {}".format(
            new_current, ", ".join(str(v["n"]) for v in history)))

    plan = {
        "keep": {"work_id": keep, "canonical": a["canonical"], "project": a["project"]},
        "merge": {"work_id": merge, "canonical": b["canonical"], "project": b["project"]},
        "versions_after": [
            {"n": v["n"], "from": v.get("merged_from", {"work_id": keep, "n": v["n"]}),
             "size": v.get("size"), "published_at": v.get("published_at")}
            for v in history
        ],
        "current_after": new_current,
        "serves_both_paths": [store.work_published_path(a), store.work_published_path(b)],
    }
    if dry_run:
        return {"dry_run": True, **plan}

    b_versions_before, b_current_n = list(b["versions"]), b["current"]
    a["versions"] = history
    a["current"] = new_current
    a.setdefault("merges", []).append({"merged": merge, "at": _now(), "by": actor})
    b["versions"] = []
    b["current"] = 0
    b["alias_of"] = keep
    b["merged_into"] = keep
    # v0.9.3: remember the merged work's own Drive file before its versions
    # move to `keep`, so a later folder rename still moves this path.
    b_current = next((v for v in b_versions_before if v["n"] == b_current_n), None)
    if b_current and not str(b_current.get("source_file_id", "")).startswith("optimise:"):
        b["source_file_id"] = b_current.get("source_file_id", "")
    b["merged_at"] = _now()

    meta = {"work_id": keep, "version": new_current, "content_sha": chosen["content_sha"],
            "published_by": actor}
    for path in (store.work_published_path(a), store.work_published_path(b)):
        store.copy_object(chosen["object_path"], path, meta)
    for alias in aliases_of(merge, registry):
        alias["alias_of"] = keep  # an alias of the merged work now points at the kept one
        store.copy_object(chosen["object_path"], store.work_published_path(alias), meta)

    for _attempt in range(5):
        try:
            store.write_json(store.REGISTRY_PATH, registry, gen)
            break
        except store.Conflict:
            raise RuntimeError("the registry changed during the merge; nothing was written - run it again")

    # Anything waiting to update the merged work now updates the kept one.
    retargeted = []
    for _attempt in range(5):
        staging, sgen = load_staging()
        retargeted = []
        for item in staging["items"].values():
            if item.get("state") != "pending":
                continue
            prop = item.get("proposal") or {}
            if prop.get("work_id") == merge:
                prop["work_id"] = keep
                retargeted.append(item["staging_id"])
            opt = item.get("optimisation") or {}
            if opt.get("work_id") == merge:
                opt["work_id"] = keep
                if item["staging_id"] not in retargeted:
                    retargeted.append(item["staging_id"])
        if not retargeted:
            break
        try:
            store.write_json(store.STAGING_PATH, staging, sgen)
            break
        except store.Conflict:
            continue
    return {"dry_run": False, **plan, "retargeted_staging": retargeted}


def resolve_already_published(group: str = "", actor: str = "unknown", dry_run: bool = True) -> dict:
    """
    Close waiting items whose exact file is already published (HUB-53).

    The 16 Sound & Motion recordings on LIVE were published, and their queue
    items still said pending with an "Add this piece" button that would have
    minted duplicates. An item is resolved only on certainty: the same Drive
    file id AND the same bytes as a version already in the registry.
    """
    registry, _gen = load_registry()
    published = {}
    for work in registry["works"].values():
        for v in work["versions"]:
            published[(v.get("source_file_id"), v.get("content_sha"))] = (work, v)
            if v.get("source_sha"):
                published[(v.get("source_file_id"), v["source_sha"])] = (work, v)

    staging, sgen = load_staging()
    found = []
    for item in staging["items"].values():
        if item.get("state") != "pending" or (group and item.get("group") != group):
            continue
        hit = published.get((item.get("source_file_id"), item.get("content_sha")))
        if hit is None and item.get("source_sha"):
            hit = published.get((item.get("source_file_id"), item["source_sha"]))
        if hit is None:
            continue
        work, v = hit
        found.append({"staging_id": item["staging_id"], "source_name": item.get("source_name"),
                      "work_id": work["work_id"], "canonical": work["canonical"], "version": v["n"]})
    if dry_run or not found:
        return {"dry_run": dry_run, "count": len(found), "items": found}

    for row in found:
        _close_staging(row["staging_id"], "published", actor,
                       {"work_id": row["work_id"], "version": row["version"],
                        "resolved": "already published"})
    return {"dry_run": False, "count": len(found), "items": found}


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


def mark_missing_at_source(group: str, seen_file_ids: set[str], in_scope=None) -> list[dict]:
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
            if in_scope is not None and not in_scope(work["project"]):
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


def record_folders(moves: dict) -> list[dict]:
    """
    Note the Drive folder each work now sits in. {work_id: folder}.

    Display only. The published path is frozen (R2) and is not touched: this is
    what lets a person regroup files on the Hub by moving them in Drive without
    changing the file a singer's device already holds.
    """
    for _attempt in range(5):
        registry, gen = load_registry()
        done = []
        for work_id, folder in moves.items():
            work = registry["works"].get(work_id)
            if work is None:
                continue
            if folder == work["project"]:
                work.pop("folder", None)
            else:
                work["folder"] = folder
            done.append({"work_id": work_id, "canonical": work["canonical"], "folder": folder})
        try:
            store.write_json(store.REGISTRY_PATH, registry, gen)
            return done
        except store.Conflict:
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
    work = effective(work, registry)
    work_id = work["work_id"]

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
    ppath = store.work_published_path(work)
    rb_meta = {
        "work_id": work_id,
        "version": to_version,
        "content_sha": target["content_sha"],
        "page_count": target["structure"]["page_count"],
        "rolled_back_from": was,
        "published_by": actor,
    }
    store.copy_object(target["object_path"], ppath, rb_meta)
    for alias in aliases_of(work_id, registry):
        store.copy_object(target["object_path"], store.work_published_path(alias), rb_meta)

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
        if work["group"] != group:
            continue
        source = effective(work, registry)
        if not source["versions"]:
            continue
        current = next(v for v in source["versions"] if v["n"] == source["current"])
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
                # v0.6.0. Absent on a pre-audio consumer's reading is fine:
                # everything published before then is a PDF.
                "media": work.get("media", media.PDF),
                "ext": store.work_ext(work),
                "mime": media.content_type(store.work_ext(work)),
                "size": int(current.get("size") or 0),
                "source_name": current.get("source_name", ""),
                # Lets the Hub recognise a file someone already linked by hand
                # from Drive, and show it once rather than twice.
                "source_file_id": current.get("source_file_id", ""),
                # v0.7.1: where the file sits in Drive NOW, which can differ
                # from `project` (where it was first published) after a move.
                "folder": work.get("folder", work["project"]),
                "object_path": store.work_published_path(work),
                # v0.9.0: set when this project's copy shares another work's
                # music (link_existing or a merge). The Hub treats both ids as
                # the same piece.
                "alias_of": work.get("alias_of", ""),
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
                allow_large=bool(item.get("allow_large")),
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
