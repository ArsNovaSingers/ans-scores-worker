# ans-scores-worker

Phase 1 of the Ars Nova Singers Hub device-sync system. Spec:
`claude/portal/Device_Sync_Spec.md` in the Ars Nova Claude project.

This service watches Tom's Google Drive folders and **publishes** what it finds
into a mirror on Google Cloud Storage that singers' iPads read from. It exists
for one reason: in Drive a rename is a rename and a delete is a delete,
instantly, for everyone — and fifty singers' annotation layers are bound to
filenames that therefore must never change.

## The one idea

**The published name is not derived from the uploaded name.**

Every piece of music is a *work* with a permanent id and one canonical
published filename, frozen at first publication. Incoming files are **matched
to a work**, never parsed into one. `ANS-Margutti-Rivrs-0818.pdf` does not
become a new piece called *Rivrs*; it becomes the next version of the work
published as `ANS-Margutti-Rivers.pdf`, and every singer's markings hold.

Nothing in `fingerprint.py` looks at a filename. `naming.py` exists only to
*propose* names and to *measure* similarity so the service can ask a sensible
question. Bytes and structure decide; names propose.

## How a file is recognised

| Signal | Catches | Strength |
|---|---|---|
| SHA-256 of the bytes | an identical re-upload | certain |
| Page count + page dimensions | the change that would move annotations | hard gate |
| Edition key — hashed page text, or a visual fingerprint of rendered pages when there is no text layer | the same edition re-scanned or re-exported under **any** filename | very high |
| Normalised name similarity, scoped to the project folder | the missing letter, the dropped composer | **proposes only** |

Only bytes and structure ever auto-decide. A wrong auto-merge writes one
piece's revision over another piece's identity and fifty people's markings land
on the wrong music, so anything less than certain becomes a question.

## Invariants

- **R1** Nothing is ever deleted from the mirror. There is no delete call in
  `store.py`. A file gone from Drive is flagged `source_missing`, not removed.
  The bucket also has object versioning on as a second net.
- **R2** The published name is frozen at first publication.
- **R3** Every version is kept. Publishing writes to `_versions/` **first** and
  moves the published pointer only after that succeeds, so a crash halfway
  leaves the previous version serving.
- **R4** `/scan` never publishes. It produces questions; a human answers them
  at `/publish`.

## Endpoints

All require `Authorization: Bearer <ANS_SCORES_TOKEN>`. Deliberately not a
`?key=` query parameter — that writes the token into the Cloud Run request log
in plaintext on every call, which the rest of the Ars Nova connector fleet
currently does and this service does not.

| Method | Path | Does |
|---|---|---|
| GET | `/health` | liveness, no auth |
| GET | `/whoami` | which identity Drive is being read as, and which bucket |
| POST | `/scan` | walk a group's folder, stage anything new or changed |
| GET | `/staging` | what is waiting for a human |
| POST | `/publish` | turn a staged item into a published version |
| GET | `/works` | the registry |
| GET | `/manifest/<group>` | rebuild and return the manifest |
| POST | `/verify` | R6 — every current version exists where it should |
| GET | `/url` | short-lived signed read URL for one published score |
| POST | `/propose-name` | what would this filename publish as |
| POST | `/rollback` | put a previous version back on the published path |
| GET | `/library/<group>` | the singer-facing view: what is published, by project |
| POST | `/publish-batch` | decide several staged items in one call |

### Scanning

```
POST /scan  {"group": "chamber-singers", "folder_id": "<drive folder id>", "limit": 25}
```

Unchanged files are skipped without downloading — the cursor cache keys on
Drive's own md5 and modifiedTime, which matters when a folder holds
hundreds of megabytes. `limit` bounds one run; the response reports how many
were left for the next one.

### Publishing

```
POST /publish  {"staging_id": "...", "decision": "new_edition",
                "work_id": "...", "actor": "jon@arsnovasingers.org"}
```

`decision` is `new_work`, `new_edition` or `reject`. A `new_edition` whose page
count or page dimensions differ from the published version is **refused** and
says why; repeating with `accept_structure_change: true` is a deliberate act
that comes with an obligation to tell the choir.

### Rolling back

```
POST /rollback  {"work_id": "...", "to_version": 1, "actor": "jon@arsnovasingers.org"}
```

R3 promised that every version is kept and that a bad publish is recoverable.
Until v0.2.0 nothing actually exercised that promise, which testing found and
review did not. Rollback copies the chosen version's bytes back over the
published path and moves `current`; it deletes nothing and renumbers nothing,
so the version that was current stays where it is and a rollback can itself be
rolled back. The published filename does not move — that is the point.

### The singer-facing view

```
GET /library/chamber-singers
```

Returns what is published for a group, grouped by project: canonical name, page
count, version number, when it last changed, and whether it has ever been
revised. It is deliberately not the registry dump, and it knows nothing about
who a singer is — WordPress calls this server-side and owns the question of who
may see what. Keeping those two apart is what stops a second permission system
growing here to disagree with the first one.

## Configuration

| Env | Meaning |
|---|---|
| `SCORES_BUCKET` | GCS bucket. Default `ars-nova-scores` |
| `ANS_SCORES_TOKEN` | bearer token. **No default — with it unset every authorised route returns 401**, which is the correct failure |
| `DRIVE_SUBJECT` | optional. An `@arsnovasingers.org` address to act as via domain-wide delegation. Unset means the service account's own identity, which then has to be a member of the shared drive |

## Known limits, stated rather than discovered later

- **Files are downloaded to the instance's temp space**, which on Cloud Run is
  memory. A 268 MB score needs headroom; the service is deployed with 2 GiB and
  a 900-second request timeout. A score several times larger than anything
  currently in Drive would need a streaming rework.
- **`max-instances=1`.** The registry writes use if-generation-match
  preconditions and would be correct without it, but one instance removes a
  whole class of race entirely while the system is young.
- **Project is inferred from the folder path**, dropping container-ish trailing
  segments (`PDFs`, `Scores`, `Sheet Music`). That is a heuristic and it is
  meant to be replaced by an explicit mapping once the Hub owns it.
- **PDFs only.** Rehearsal audio and video share the folders and are ignored
  here; they need the same pipeline without any of the naming machinery.
- **Staged candidates are left in `_staging/`** after publishing. A lifecycle
  rule should age them out; there isn't one yet.

## Not in this phase

Optimisation and Tom's approval queue (Phase 2), singer-facing delivery
(Phase 3), the pilot (Phase 4), WebDAV (Phase 5), quiet hours (Phase 6).
