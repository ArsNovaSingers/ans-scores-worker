"""
The mirror, and the small amount of state that describes it.

Layout in the bucket:

    scores/{group}/{project}/{canonical}.pdf   what singers read. Name is frozen
    _versions/{work_id}/{n}/{canonical}.pdf    every version ever published
    _registry/works.json                       work records and their frozen names
    _state/staging.json                        what is waiting for a human
    _state/cursors.json                        where the last Drive walk got to
    _manifest/{group}.json                     names, hashes, sizes, page counts

Two invariants this module is responsible for, both from Device_Sync_Spec.md
section 4:

  R1  Nothing here ever deletes a published object. There is no delete call in
      this file. A file that vanishes from Drive is marked, not removed.
  R3  Publishing writes the new bytes to _versions/ FIRST and only then moves
      the published pointer. A crash halfway leaves the old version serving.

JSON documents are written with an if-generation-match precondition so two
concurrent writers cannot silently lose each other's changes. The service also
runs at max-instances=1 for now, which makes that belt and braces - but the
precondition is what keeps it correct if that ever changes.
"""

import json
import os
import threading

from google.cloud import storage

BUCKET_NAME = os.environ.get("SCORES_BUCKET", "ars-nova-scores")

REGISTRY_PATH = "_registry/works.json"
STAGING_PATH = "_state/staging.json"
CURSORS_PATH = "_state/cursors.json"

_client = None
_client_lock = threading.Lock()


def bucket():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = storage.Client()
    return _client.bucket(BUCKET_NAME)


class Conflict(Exception):
    """Another writer changed the document since it was read. Re-read and retry."""


def read_json(path: str, default):
    """Return (value, generation). generation 0 means the object does not exist."""
    blob = bucket().blob(path)
    try:
        raw = blob.download_as_bytes()
    except Exception:  # noqa: BLE001 - not-found and transient both mean "start fresh"
        if blob.exists():
            raise
        return default, 0
    return json.loads(raw.decode("utf-8")), blob.generation


def write_json(path: str, value, generation: int) -> int:
    """Write with a precondition. Pass the generation you read; 0 means create-only."""
    blob = bucket().blob(path)
    payload = json.dumps(value, indent=2, sort_keys=True).encode("utf-8")
    try:
        blob.upload_from_string(
            payload,
            content_type="application/json",
            if_generation_match=generation,
        )
    except Exception as exc:  # noqa: BLE001
        if "412" in str(exc) or "conditionNotMet" in str(exc):
            raise Conflict(path) from exc
        raise
    return blob.generation


def published_path(group: str, project: str, canonical: str) -> str:
    return "scores/{}/{}/{}.pdf".format(group, project, canonical)


def version_path(work_id: str, n: int, canonical: str) -> str:
    return "_versions/{}/{}/{}.pdf".format(work_id, n, canonical)


def upload_file(local_path: str, object_path: str, metadata: dict | None = None) -> str:
    blob = bucket().blob(object_path)
    if metadata:
        blob.metadata = {k: str(v) for k, v in metadata.items()}
    blob.upload_from_filename(local_path, content_type="application/pdf")
    blob.reload()
    return blob.md5_hash or ""


def copy_object(src_path: str, dst_path: str, metadata: dict | None = None) -> None:
    src = bucket().blob(src_path)
    copied = bucket().copy_blob(src, bucket(), dst_path)
    if metadata:
        copied.metadata = {k: str(v) for k, v in metadata.items()}
        copied.patch()


def staging_path(staging_id: str) -> str:
    """
    Where a candidate waits between the scan that found it and the human who
    decides about it. Cloud Run instances are ephemeral, so /tmp cannot hold it:
    a scan and its publish are different requests and may be different machines.
    """
    return "_staging/{}.pdf".format(staging_id)


def object_exists(object_path: str) -> bool:
    return bucket().blob(object_path).exists()


def list_published(group: str) -> list[dict]:
    prefix = "scores/{}/".format(group)
    out = []
    for blob in bucket().list_blobs(prefix=prefix):
        out.append(
            {
                "path": blob.name,
                "size": blob.size,
                "md5": blob.md5_hash,
                "updated": blob.updated.isoformat() if blob.updated else None,
                "metadata": blob.metadata or {},
            }
        )
    return out


def write_manifest(group: str, entries: list[dict]) -> None:
    path = "_manifest/{}.json".format(group)
    blob = bucket().blob(path)
    blob.upload_from_string(
        json.dumps({"group": group, "files": entries}, indent=2, sort_keys=True).encode("utf-8"),
        content_type="application/json",
    )


def signed_url(object_path: str, minutes: int = 15) -> str:
    """
    Short-lived read URL. The bucket has public access prevention on, so this is
    the only way a byte leaves it - access is always a decision the Hub made,
    never a URL someone found lying around.

    On Cloud Run there is no private key on disk: the credentials come from the
    metadata server and cannot sign anything locally. Signing therefore goes
    through IAM's SignBlob API, which needs the service account to hold
    roles/iam.serviceAccountTokenCreator ON ITSELF. Without that grant this
    raises, and the error names the missing role rather than failing vaguely.
    """
    from datetime import timedelta

    import google.auth
    from google.auth.transport.requests import Request

    creds, _project = google.auth.default()
    blob = bucket().blob(object_path)

    signer_kwargs = {}
    if getattr(creds, "signer_email", None) is None:
        # Metadata-server credentials: delegate signing to IAM.
        creds.refresh(Request())
        signer_kwargs = {
            "service_account_email": getattr(creds, "service_account_email", None),
            "access_token": creds.token,
        }

    try:
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=minutes),
            method="GET",
            **signer_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "could not sign a download URL. The service account almost certainly "
            "needs roles/iam.serviceAccountTokenCreator on itself: {}".format(exc)
        ) from exc
