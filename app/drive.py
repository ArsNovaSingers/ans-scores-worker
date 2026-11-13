"""
Reading Tom's folders. Read-only, by construction - no scope here can write.

Two credential paths, because which one works depends on how the service
account was set up and that is not something to guess at from a sandbox:

  * Domain-wide delegation - set DRIVE_SUBJECT to a real @arsnovasingers.org
    address and the worker acts as that person. This is how the existing
    arsnova-google-* services reach Drive.
  * Plain service account - leave DRIVE_SUBJECT unset and the SA's own
    identity is used, which requires the SA to be a member of the shared drive.

/whoami reports which path is live so this is never a mystery in production.
"""

import os

import google.auth
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

FIELDS = "nextPageToken, files(id, name, mimeType, size, md5Checksum, modifiedTime, parents, trashed)"


def _credentials():
    subject = os.environ.get("DRIVE_SUBJECT", "").strip()
    creds, _project = google.auth.default(scopes=SCOPES)
    if subject and hasattr(creds, "with_subject"):
        creds = creds.with_subject(subject)
    return creds


def service():
    return build("drive", "v3", credentials=_credentials(), cache_discovery=False)


def whoami() -> dict:
    creds = _credentials()
    creds.refresh(Request())
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    about = svc.about().get(fields="user(emailAddress,displayName)").execute()
    return {
        "acting_as": about.get("user", {}).get("emailAddress"),
        "delegated": bool(os.environ.get("DRIVE_SUBJECT", "").strip()),
    }


def walk(folder_id: str, svc=None) -> list[dict]:
    """
    Every non-trashed file under a folder, recursively, with its path.

    Folders are followed depth-first and the relative path is carried down, so
    a file's project is knowable from where it sits rather than from its name.
    """
    svc = svc or service()
    out: list[dict] = []
    stack = [(folder_id, [])]
    seen: set[str] = set()

    while stack:
        current, path = stack.pop()
        if current in seen:
            continue
        seen.add(current)

        token = None
        while True:
            resp = (
                svc.files()
                .list(
                    q="'{}' in parents and trashed = false".format(current),
                    fields=FIELDS,
                    pageSize=200,
                    pageToken=token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for item in resp.get("files", []):
                if item["mimeType"] == "application/vnd.google-apps.folder":
                    stack.append((item["id"], path + [item["name"]]))
                else:
                    item["rel_path"] = path
                    out.append(item)
            token = resp.get("nextPageToken")
            if not token:
                break
    return out


def list_folders(parent_id: str, svc=None) -> list[dict]:
    """
    The immediate subfolders of one folder. One level, never recursive.

    This is what a person browses with, and browsing is a different job from
    walk(): walk() reads an entire tree to find work, which on the Singers Hub
    is hundreds of files and several seconds. A picker that did that per click
    would be unusable, and would read the whole drive to show six names.
    """
    svc = svc or service()
    out: list[dict] = []
    token = None
    while True:
        resp = (
            svc.files()
            .list(
                q=(
                    "'{}' in parents and trashed = false "
                    "and mimeType = 'application/vnd.google-apps.folder'"
                ).format(parent_id),
                fields="nextPageToken, files(id, name)",
                orderBy="name",
                pageSize=200,
                pageToken=token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        out.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return out


def folder_info(folder_id: str, svc=None) -> dict:
    """
    Name and parent of one folder, so a picker can show where it is.

    The root of a shared drive is a special case worth handling rather than
    living with: files().get() on one returns the literal name "Drive", so a
    breadcrumb at the top of "Singers Hub (Choir Members Shared)" would read
    "Drive" and give no clue which drive you are in. The drives() endpoint
    knows the real name, so ask it when the id turns out to be a drive.
    """
    svc = svc or service()
    meta = (
        svc.files()
        .get(fileId=folder_id, fields="id, name, parents, mimeType, driveId", supportsAllDrives=True)
        .execute()
    )

    name = meta.get("name")
    parents = meta.get("parents", [])
    if not parents:
        # No parent means this is the top of something. If that something is a
        # shared drive, its own record carries the name people recognise.
        try:
            drive_meta = svc.drives().get(driveId=folder_id, fields="id, name").execute()
            if drive_meta.get("name"):
                name = drive_meta["name"]
        except Exception:  # noqa: BLE001 - an ordinary folder is not a drive
            pass

    return {
        "id": meta.get("id"),
        "name": name,
        "parents": parents,
        "is_folder": meta.get("mimeType") == "application/vnd.google-apps.folder",
    }


def shared_drives(svc=None) -> list[dict]:
    """The shared drives this service account can see — the top of the tree."""
    svc = svc or service()
    resp = svc.drives().list(pageSize=100, fields="drives(id, name)").execute()
    return resp.get("drives", [])


def download(file_id: str, dest_path: str, svc=None) -> None:
    """Stream a file to disk. These run to hundreds of MB - never into memory whole."""
    svc = svc or service()
    request = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _status, done = downloader.next_chunk()


def export_csv(file_id: str, svc=None) -> str:
    """
    A native Google Sheet's FIRST TAB as CSV text. Read-only: files.export needs
    no Sheets API scope, and Drive only ever exports the first tab to CSV.
    """
    svc = svc or service()
    raw = svc.files().export(fileId=file_id, mimeType="text/csv").execute()
    return raw.decode("utf-8-sig", "replace") if isinstance(raw, bytes) else str(raw)


# ---------------------------------------------------------------------------
# v0.12.0 (HUB-78): Drive push. Read-only like everything above: changes.watch,
# changes.list and channels.stop all accept the drive.readonly scope.
# ---------------------------------------------------------------------------

CHANGE_FIELDS = (
    "nextPageToken, newStartPageToken, "
    "changes(fileId, removed, time, file(id, name, mimeType, parents, trashed))"
)


def start_page_token(drive_id: str, svc=None) -> str:
    """Where "from now on" begins for one shared drive."""
    svc = svc or service()
    resp = svc.changes().getStartPageToken(driveId=drive_id, supportsAllDrives=True).execute()
    return resp["startPageToken"]


def watch_changes(drive_id: str, page_token: str, channel_id: str, address: str,
                  token: str, expiration_ms: int, svc=None) -> dict:
    """
    Open a push channel: Drive POSTs to `address` whenever something in the
    drive changes. The POST carries no file details, only the channel id and
    `token` in headers - the receiver then asks list_changes() what happened.
    """
    svc = svc or service()
    body = {
        "id": channel_id,
        "type": "web_hook",
        "address": address,
        "token": token,
        "expiration": str(int(expiration_ms)),
    }
    return (
        svc.changes()
        .watch(
            pageToken=page_token,
            driveId=drive_id,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            body=body,
        )
        .execute()
    )


def stop_channel(channel_id: str, resource_id: str, svc=None) -> None:
    svc = svc or service()
    svc.channels().stop(body={"id": channel_id, "resourceId": resource_id}).execute()


def list_changes(drive_id: str, page_token: str, svc=None, max_pages: int = 20) -> tuple[list[dict], str, bool]:
    """
    Every change since page_token. Returns (changes, token to resume from,
    complete). complete is False when max_pages ran out first; the returned
    token then resumes where this stopped rather than skipping ahead.
    """
    svc = svc or service()
    out: list[dict] = []
    token = page_token
    for _ in range(max_pages):
        resp = (
            svc.changes()
            .list(
                pageToken=token,
                driveId=drive_id,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                includeRemoved=True,
                pageSize=1000,
                fields=CHANGE_FIELDS,
            )
            .execute()
        )
        out.extend(resp.get("changes", []))
        if resp.get("newStartPageToken"):
            return out, resp["newStartPageToken"], True
        token = resp.get("nextPageToken")
        if not token:
            return out, page_token, True
    return out, token, False


def parents_of(file_id: str, svc=None) -> list[str]:
    svc = svc or service()
    meta = svc.files().get(fileId=file_id, fields="id, parents", supportsAllDrives=True).execute()
    return meta.get("parents", []) or []


def ancestors(parents: list[str], svc=None, cache: dict | None = None, limit: int = 12) -> list[str]:
    """
    Every folder above a file, nearest first, up to the drive root. `cache`
    maps folder id -> its parents and is shared across one batch of changes,
    because six click tracks in one folder share every ancestor.
    """
    svc = svc or service()
    cache = cache if cache is not None else {}
    out: list[str] = []
    frontier = list(parents or [])
    while frontier and len(out) < limit * 4:
        current = frontier.pop(0)
        if current in out:
            continue
        out.append(current)
        if current not in cache:
            try:
                cache[current] = parents_of(current, svc)
            except Exception:  # noqa: BLE001 - the drive root and unreadable folders end the walk
                cache[current] = []
        frontier.extend(cache[current])
    return out
