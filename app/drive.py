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
    """Name and parent of one folder, so a picker can show where it is."""
    svc = svc or service()
    meta = (
        svc.files()
        .get(fileId=folder_id, fields="id, name, parents, mimeType", supportsAllDrives=True)
        .execute()
    )
    return {
        "id": meta.get("id"),
        "name": meta.get("name"),
        "parents": meta.get("parents", []),
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
