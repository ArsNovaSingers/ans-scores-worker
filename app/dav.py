"""
Read-only WebDAV over the published mirror.

Why this exists: a singer with fifty scores does not want to tap fifty links.
WebDAV lets a file app mount the season and pull the lot, then later pull only
what changed - which is the difference between the Hub page (one tap per file,
no extra app) and a power user's route (one extra app, then one action).

WHY IT IS READ-ONLY, and why that is not laziness:

    R1 says nothing is ever removed from the mirror.
    R2 says a published filename never changes.

The cheapest way to guarantee both against a client that misbehaves - or a
singer who drags a folder the wrong way in a file browser - is a server with no
verb that could do it. There is no PUT, no DELETE, no MKCOL, no MOVE, no COPY,
no LOCK. A client that tries gets 405 and the mirror is untouched. If write
support is ever wanted it should be a separate, deliberate decision, not
something that arrives by adding a handler here.

The tree is built from what is ACTUALLY IN THE BUCKET rather than from the
registry. If those two ever disagree, the bytes are what a singer can download
and the registry is an account of them - so the bytes win. It also means
listings carry real sizes, real modification times and real checksums, which is
what lets a client re-pull only what changed.

Auth is HTTP Basic, because that is what file apps speak. Credentials come from
the ANS_DAV_USERS secret as {"username": {"password": "...", "groups": [...]}}
- so one shared credential per group works today, and per-singer credentials
are more entries in the same map rather than a rewrite.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
from email.utils import format_datetime
from datetime import datetime, timezone
from urllib.parse import quote, unquote
from xml.sax.saxutils import escape

from flask import Response, request, stream_with_context

from . import store

DAV_ROOT = "/dav"
CHUNK = 262144


def _users() -> dict:
    """
    The credential map, parsed fresh each call.

    Cloud Run injects the secret at start-up, so this is a dict lookup on an
    env var, not I/O. Parsing per call rather than at import means a bad JSON
    value produces 401s that are logged rather than a container that will not
    boot - a broken credential should lock people out, not take the service
    down for everyone.
    """
    raw = os.environ.get("ANS_DAV_USERS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _authenticate() -> list[str] | None:
    """
    Return the groups this request may read, or None.

    Compared with compare_digest so a wrong password takes the same time as a
    right one.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
    except Exception:
        return None
    if ":" not in decoded:
        return None
    username, password = decoded.split(":", 1)

    entry = _users().get(username)
    if not isinstance(entry, dict):
        return None
    expected = str(entry.get("password", ""))
    if not expected or not hmac.compare_digest(password, expected):
        return None

    groups = entry.get("groups")
    if not isinstance(groups, list):
        return None
    return [str(g) for g in groups if str(g).strip()]


def _challenge():
    return Response(
        "Authentication required.",
        401,
        {"WWW-Authenticate": 'Basic realm="Ars Nova sheet music"'},
    )


def _href(*parts: str) -> str:
    out = DAV_ROOT
    for part in parts:
        out += "/" + quote(part, safe="")
    return out


def _tree(group: str) -> dict:
    """
    {project: {filename: {size, updated, md5}}} for one group, from the bucket.

    Anything not directly under scores/<group>/<project>/<file> is skipped
    rather than guessed at - a stray object should be invisible, not a folder
    a singer can wander into.
    """
    projects: dict = {}
    prefix_len = len("scores/{}/".format(group))
    for blob in store.list_published(group):
        rest = str(blob.get("path", ""))[prefix_len:]
        parts = rest.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            continue
        projects.setdefault(parts[0], {})[parts[1]] = {
            "size": int(blob.get("size") or 0),
            "updated": blob.get("updated"),
            "md5": blob.get("md5") or "",
        }
    return projects


def _httpdate(iso: str | None) -> str:
    if not iso:
        return format_datetime(datetime.now(timezone.utc), usegmt=True)
    try:
        return format_datetime(datetime.fromisoformat(iso), usegmt=True)
    except ValueError:
        return format_datetime(datetime.now(timezone.utc), usegmt=True)


def _collection(href: str, name: str) -> str:
    return (
        "<D:response><D:href>{href}</D:href><D:propstat><D:prop>"
        "<D:displayname>{name}</D:displayname>"
        "<D:resourcetype><D:collection/></D:resourcetype>"
        "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
    ).format(href=escape(href), name=escape(name))


def _file(href: str, name: str, meta: dict) -> str:
    """
    A file entry, carrying the three properties that make an incremental pull
    possible: length, modification time and an ETag. Without them a client has
    to re-download the season every time to find out nothing changed.
    """
    return (
        "<D:response><D:href>{href}</D:href><D:propstat><D:prop>"
        "<D:displayname>{name}</D:displayname>"
        "<D:resourcetype/>"
        "<D:getcontentlength>{size}</D:getcontentlength>"
        "<D:getcontenttype>application/pdf</D:getcontenttype>"
        "<D:getlastmodified>{mtime}</D:getlastmodified>"
        "<D:getetag>&quot;{etag}&quot;</D:getetag>"
        "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
    ).format(
        href=escape(href),
        name=escape(name),
        size=int(meta.get("size") or 0),
        mtime=escape(_httpdate(meta.get("updated"))),
        etag=escape(str(meta.get("md5") or "")),
    )


def _multistatus(entries: list[str]) -> Response:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:multistatus xmlns:D="DAV:">' + "".join(entries) + "</D:multistatus>"
    )
    return Response(body, 207, {"Content-Type": 'application/xml; charset="utf-8"'})


def _propfind(segments: list[str], groups: list[str]) -> Response:
    # Depth: infinity is refused rather than served. On a season of hundreds of
    # scores it invites a client to ask for the whole tree in one request, and
    # every file app worth using walks depth 1 anyway.
    depth = request.headers.get("Depth", "1").strip().lower()
    if depth not in ("0", "1"):
        return Response("Depth infinity is not supported.", 403)
    deep = depth == "1"

    if not segments:
        out = [_collection(DAV_ROOT + "/", "Ars Nova sheet music")]
        if deep:
            for group in groups:
                out.append(_collection(_href(group) + "/", group))
        return _multistatus(out)

    group = segments[0]
    if group not in groups:
        return Response("Not found.", 404)
    tree = _tree(group)

    if len(segments) == 1:
        out = [_collection(_href(group) + "/", group)]
        if deep:
            for project in sorted(tree):
                out.append(_collection(_href(group, project) + "/", project))
        return _multistatus(out)

    project = segments[1]
    if project not in tree:
        return Response("Not found.", 404)

    if len(segments) == 2:
        out = [_collection(_href(group, project) + "/", project)]
        if deep:
            for name in sorted(tree[project]):
                out.append(_file(_href(group, project, name), name, tree[project][name]))
        return _multistatus(out)

    name = segments[2]
    if len(segments) > 3 or name not in tree[project]:
        return Response("Not found.", 404)
    return _multistatus([_file(_href(group, project, name), name, tree[project][name])])


def _get(segments: list[str], groups: list[str]) -> Response:
    if len(segments) != 3:
        return Response("Not found.", 404)
    group, project, name = segments
    if group not in groups:
        return Response("Not found.", 404)

    tree = _tree(group)
    meta = tree.get(project, {}).get(name)
    if meta is None:
        return Response("Not found.", 404)

    headers = {
        "Content-Type": "application/pdf",
        "Content-Length": str(meta.get("size") or 0),
        "Last-Modified": _httpdate(meta.get("updated")),
        "ETag": '"{}"'.format(meta.get("md5") or ""),
        "Accept-Ranges": "none",
    }

    # HEAD is deliberately NOT a separate branch. Returning an empty body
    # made Werkzeug recompute Content-Length as 0, so a client asking how big
    # a score is was told nothing - which is exactly what HEAD is for. The
    # streaming response below carries an explicit length that survives,
    # because a generator's size cannot be recomputed, and Werkzeug drops the
    # body itself for a HEAD request. Caught by a test, not by review.
    blob = store.bucket().blob("scores/{}/{}/{}".format(group, project, name))

    def stream():
        # Streamed rather than downloaded whole: a full score runs to hundreds
        # of megabytes and must never sit in this container's memory.
        with blob.open("rb") as handle:
            while True:
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                yield chunk

    return Response(stream_with_context(stream()), 200, headers)


def register(app) -> None:
    """
    Wire the WebDAV surface onto the Flask app.

    One handler for the whole tree, because the path decides what a request
    means and splitting that across routes would put the same authorisation
    check in four places.
    """
    methods = ["OPTIONS", "PROPFIND", "GET", "HEAD"]

    @app.route("/dav", defaults={"subpath": ""}, methods=methods)
    @app.route("/dav/", defaults={"subpath": ""}, methods=methods)
    @app.route("/dav/<path:subpath>", methods=methods)
    def dav(subpath: str):  # noqa: WPS430 - closure is the registration
        if request.method == "OPTIONS":
            # Answered without credentials: a client asks this first to find
            # out whether it is talking to a WebDAV server at all, and a 401
            # here reads to some file apps as "not WebDAV" rather than
            # "log in".
            return Response(
                "",
                200,
                {
                    "DAV": "1",
                    "MS-Author-Via": "DAV",
                    "Allow": ", ".join(methods),
                    "Content-Length": "0",
                },
            )

        groups = _authenticate()
        if groups is None:
            return _challenge()

        segments = [unquote(s) for s in subpath.split("/") if s]

        if request.method == "PROPFIND":
            return _propfind(segments, groups)
        return _get(segments, groups)
