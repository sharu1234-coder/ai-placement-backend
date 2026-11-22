# Runbook — account-deletion worker

**Status: not deployed.** Nothing in this document has been run in production or staging. It
describes how the worker is intended to be operated; every procedure below is unexercised until
someone performs the staging validation in section 10. Written 2026-10-21 (Stage 4).

Audience: whoever runs the backend. Assumes no knowledge of the outbox's internals; the design
is in `proposal-account-deletion-outbox.md`, and the verified-vs-unverified split is section 13
of that document.

## 1. What this process is responsible for

Deleting a TALVIORA AI account happens in two phases. Phase 1 is synchronous, inside the user's
request: access is revoked, the account is minimised, and one durable obligation row is written
per stored object. **Phase 1 never calls storage.** Phase 2 is this worker: it erases those
objects, then deletes the last user row.

The consequence worth internalising before operating it: **between Phase 1 and this worker
finishing, the deletion request row is the only remaining pointer to objects the user has been
told are gone.** A stopped worker is not a stalled queue — it is an unfinished obligation that
nothing else in the system will pick up.

It follows that the worker has no "give up" state. A job that keeps failing is flagged
`needs_attention` and **keeps being retried**; there is no terminal `failed` status, because one
would convert an unfinished erasure into an abandoned one. Clearing a stuck job is always an
operator action, never a timeout.

## 2. Topology

One supervised long-running process per environment.

- **Run exactly one.** More than one is *safe* — the claim is `FOR UPDATE SKIP LOCKED` with a
  lease, so two workers never take the same job — but it buys nothing at this volume and
  multiplies the monitoring surface. Scale only if the claimable count persistently exceeds
  what one process clears per pass.
- **A supervised service, not a cron job.** The process already loops on its own interval
  (default 60 s) and shuts down cleanly on a signal. A scheduler that launches overlapping
  copies is strictly worse than one supervised process. Use systemd with `Restart=on-failure`
  on Linux, or the chosen platform's equivalent.
- **Do not run it inside the web application.** It is deliberately not wired into the FastAPI
  lifespan: the API scales on request load and recycles workers, neither of which is a sane
  trigger for an erasure queue, and a per-replica copy would multiply the process count by the
  replica count silently.
- **Not yet decided** (owner): the hosting platform, and therefore the supervisor. The process
  itself is platform-neutral.

## 3. Command

```
python scripts/run_deletion_worker.py [--once] [--interval SECONDS] [--batch N]
```

| Flag | Default | Bounds | Notes |
|---|---|---|---|
| `--interval` | 60 s | 1–300 | Rejected outside the range rather than clamped. |
| `--batch` | 10 | 1–500 | Jobs claimed per pass. |
| `--once` | off | — | One pass, then exit. This is the form to use for a manual check. |

`--once` is the only form anyone should run by hand. It makes exactly one pass, commits, and
exits, so it cannot be left running in a forgotten terminal.

The process exits non-zero after 5 consecutive failed passes, so a supervisor sees a crash loop
rather than a process that is up and achieving nothing.

## 4. Configuration and least privilege

The worker reads the same settings as the API. Two must match the API's values exactly:

- **`DATABASE_URL`** — the same database. A least-privilege role is sufficient: it needs
  `SELECT`, `UPDATE` and `DELETE` on `deletion_jobs`, `deletion_requests` and `users`. It does
  **not** need `CREATEDB`, `CREATEROLE`, `REPLICATION`, `BYPASSRLS` or superuser.
- **`STORAGE_PROVIDER`** — **must equal the value the API had when the jobs were enqueued.**
  Each job records the backend it was enqueued for, and the worker refuses to erase against a
  different one (section 7). If the two processes disagree, every job escalates to
  `needs_attention` and nothing is deleted. That is the safe direction, but it is a silent
  stall until someone reads the gauge.

Storage credentials need delete permission on the object prefix the application writes to, and
nothing else. Whether version-delete permission is also required is an **open question** — see
`proposal-account-deletion-outbox.md` section 13.2, case 3. Do not grant bucket-wide delete.

No credential, bucket name or object key is ever logged. Do not add one to a log line.

## 5. Startup checks

Run these before enabling the service, in this order. Each is a stop condition.

1. `python scripts/run_deletion_worker.py --once` completes and exits 0.
2. `STORAGE_PROVIDER` is identical in the API's and the worker's environment. Compare the two
   resolved values, not the two config files.
3. The database role can update `deletion_jobs` and delete from `users` — otherwise a
   permission error surfaces mid-erasure instead.
4. The storage credential can delete a scratch key the application itself wrote. Use a
   throwaway object, never a real user's.
5. The unresolved-obligation count is a number you expect. A surprisingly large one means jobs
   have been accumulating with no worker running.

## 6. Shutdown and restart

SIGINT or SIGTERM sets a flag; **the current pass finishes and commits, then the loop exits.**
There is no mid-pass abort, so a clean stop never leaves a half-written outcome.

A worker killed outright (SIGKILL, power loss, container eviction) loses a lease, not a job:
`locked_until` expires and the job becomes claimable again. Nothing has to detect the crash.
Because deleting an absent object is success, a job that died *after* its storage delete but
*before* its status write completes correctly on the retry.

**Restart procedure:** stop the service, start it. There is no queue to drain, no state file to
clear, and no manual requeue step. Do not reset `status`, `attempts` or `locked_until` by hand;
an expired lease is already reclaimable, and editing rows is how an obligation gets lost.

## 7. Retry, backoff, and what needs a human

Backoff is exponential from 30 s, capped at 30 minutes. After 5 attempts a job is flagged
`needs_attention` — which caps the backoff and makes it visible, and **does not stop the
retries**.

Triage starts from `deletion_jobs.last_error_class`. It holds the exception class plus the
backend's own error code, never a message (a message quotes the bucket and the key):

| Value | Meaning | Action |
|---|---|---|
| `ProviderMismatch` | The job was enqueued for a different backend than the worker holds. **Nothing was deleted.** | Fix `STORAGE_PROVIDER` to match the enqueueing environment. The job resumes by itself. |
| `StorageError:AccessDenied` | Permission refused. Will never succeed unattended. | Fix the credential's delete permission on that prefix. |
| `StorageError:InvalidRequest` | Often object-lock retention or a legal hold. | See proposal section 13.2 case 4. Needs a decision, not a retry. |
| `StorageError:SlowDown`, `:503`, `:RequestTimeout` | Throttling or a transient server fault. | None. It clears on its own. Watch queue age. |
| `StorageError:EndpointConnectionError` | The delete never reached the provider. | Check egress and endpoint configuration. |
| `StorageError:EACCES`, `:EROFS` | Local provider, filesystem refusal. | `STORAGE_PROVIDER=local` is not a production configuration. |
| `StorageError` (bare) | A failure the backend gave no code for. | Find the job id in the logs. |

A job flagged `ProviderMismatch` is the one case where **no deletion was attempted at all**.
Everything else in the table represents an attempt that failed.

## 8. Monitoring

Every signal below is a number or an id. None contains a key, a bucket, an email or a
credential.

**Per pass** (`log_pass`, silent when a pass does nothing, so quiet means quiet):
`claimed`, `deleted`, `already_absent`, `retried`, `needs_attention`, `requests_closed`.

**Gauges to scrape:**

| Signal | Source | Alert when |
|---|---|---|
| Claimable jobs | `deletion_worker.claimable_count(db)` | Rising across consecutive passes — the worker is not keeping up, or is not running. |
| Unresolved obligations | `deletion_worker.unresolved_obligations(db)` | Non-zero and not falling over a window longer than the backoff cap. |
| Jobs in `needs_attention` | `deletion_jobs.status` | Any, after one backoff cap (30 min). Each one is an erasure that is not progressing. |
| Oldest unresolved request age | `deletion_worker.overdue_requests` / `report_sla_breaches` | Past the provisional thresholds in section 9. |
| Worker liveness | supervisor | Process down for longer than one interval. |

**There is no HTTP health endpoint on the worker.** Liveness is the supervisor's view of the
process; *progress* is the queue-age gauge, and progress is the thing that matters. A worker
that is up while the oldest unresolved request keeps ageing is failing, and only the second
signal shows it.

## 9. Provisional targets — not SLAs

| Target | Value | Status |
|---|---|---|
| Pickup | 5 min | **Provisional** |
| Alert | 1 h | **Provisional** |
| Escalation | 24 h | **Provisional** |

These are engineering numbers chosen to be monitorable. **Privacy and legal owners have not
approved them.** They must not be quoted to a user, published in a privacy policy, or described
as a guarantee until that approval exists. Nothing user-facing references them today.

## 10. Staging validation (required before production)

Not yet performed. In a staging environment, against a staging bucket, with no real user data:

1. Create a test account, upload an object, delete the account. Confirm Phase 1 revoked access
   and wrote the obligation.
2. Run `--once`. Confirm the object is gone from the bucket and the request closed.
3. Revoke the credential's delete permission and repeat. Confirm `StorageError:AccessDenied`,
   status `needs_attention`, the retained key, and that the user row survives.
4. Restore the permission. Confirm the same job completes with no manual intervention.
5. Point the worker at the wrong `STORAGE_PROVIDER`. Confirm `ProviderMismatch` and that
   **nothing was deleted from either backend**.
6. Kill the process mid-pass. Confirm the lease expires and the job completes on restart.
7. If the bucket is versioned, list object versions after a completed erasure and record
   whether prior versions and delete markers remain. **This is the test that resolves whether
   physical erasure happens at all** (proposal section 13.2, case 3).

## 11. Stopping the worker, and rollback

Stopping is safe and reversible: send SIGTERM. Obligations stay in the database and are picked
up whenever a worker runs again. Erasures pause; nothing is lost.

**There is no override that drops outstanding jobs, and none may be added.** The downgrade
guard on the migration refuses to remove the tables while any request is `pending` or
`needs_attention`, for the same reason.

To roll back a worker *deployment*: stop the service, deploy the previous build, start it. The
queue belongs to the database, not the process, so a worker version is not a stateful upgrade.

## 12. A job held because its destination is unconfirmed

The worker will not erase an object unless the destination recorded on the job matches the
storage this worker holds. That check runs before the storage call is reachable, so a job whose
destination cannot be confirmed produces no deletion at all. It is flagged `needs_attention`
and stays claimable — it is still owed, and it resumes on its own once the configuration
matches.

Six reasons appear in `last_error_class`, and they are not interchangeable:

| `last_error_class` | What it means | What fixes it |
|---|---|---|
| `StorageIdentityUnknown` | The job predates identity capture, so no destination was ever recorded | A decision by a human — see below |
| `StorageIdentityIncomplete` | A destination was recorded but is unusable, e.g. `s3` with no bucket | Correct the configuration the job was created under |
| `StorageIdentityUndeterminable` | This worker's storage provider cannot state where it points | A provider misconfiguration in *this* process |
| `StorageIdentityMismatch` | The recorded destination and this worker's disagree | Point a worker at the storage the job was enqueued against |
| `StorageOwnerMismatch` | The job recorded an expected bucket owner and this worker is configured with a different one | Correct `STORAGE_S3_EXPECTED_BUCKET_OWNER`, or run a worker configured for the account the job was created under |
| `StorageOwnerUnconfirmed` | The job recorded an expected bucket owner and this worker has none, so it cannot assert ownership on the wire | Set `STORAGE_S3_EXPECTED_BUCKET_OWNER` on this worker |
| `StorageError:NoSuchBucket` | The configured bucket does not exist | Correct the bucket, or point the worker at the right one |

### Finding out what is held

```
python scripts/review_deletion_identity.py
```

Read-only: it deletes nothing, writes nothing, and opens no storage connection. It prints
counts, job ids and a short fingerprint of each recorded destination. It deliberately prints no
bucket, endpoint or storage key, so the output is safe to paste into an incident channel. More
than one fingerprint means the queue holds jobs enqueued under different storage
configurations, and a worker can only match one of them at a time.

### Recovering a mismatch

Point a worker at the storage the jobs were enqueued against and they drain by themselves. No
row needs editing and nothing needs re-queueing; that is what keeping the job claimable is for.
If two fingerprints both have real objects behind them, run a worker against each in turn.

### Recovering a job with no recorded destination

These are jobs created before identity capture existed. **Do not stamp the current
configuration onto them**, and note that no tooling here offers to: the only value available is
whatever this process is configured with, and if that is wrong the object becomes permanently
unreachable the moment the job is marked done, because closing the request deletes the last row
that held its key. An unrecoverable silent miss is strictly worse than a job that waits.

The decision needs someone who can answer "which bucket was configured when this job was
created", from deployment history, using the job's `created_at`. Once that is established,
setting the identity columns on those specific rows is a deliberate, auditable act — do it
narrowly, by job id, and run the review script again to confirm the result before letting a
worker claim them.

If the answer cannot be established, the honest position is that those objects cannot be
confirmed erased. Say so rather than closing the requests; §11.2 of
`docs/proposal-account-deletion-outbox.md` is where that is escalated.

## 13. A job held because the erasure could not be proved

A job reaches `done` only from a **verified** erasure: the worker deletes every object version
and every delete marker by explicit version id, then enumerates the key version history again
and requires it to come back empty. Anything short of that leaves the job unresolved and
claimable.

Why the check is an enumeration and not a 404. On a versioned bucket a delete without a version
id does not erase anything — AWS: "When versioning is enabled, a simple DELETE cannot
permanently delete an object. ... Instead, Amazon S3 inserts a delete marker". From that moment
HEAD and GET answer 404 while every byte is still stored. So a 404 is the symptom of the
failure, not evidence of success, and it is never used as evidence here. The single exception
is a bucket `GetBucketVersioning` reports as never having been versioned, where no history can
exist to be missed; the worker log line for a completed job says which kind of evidence was
used (`absence-where-no-history-can-exist` or `inventory`).

### Reasons that need a person on the first attempt

| `last_error_class` | What it means | What fixes it |
|---|---|---|
| `StorageError:ObjectLockActive` | A version is under an Object Lock retention that has not expired | Nothing, until it expires. **Do not** grant `s3:BypassGovernanceRetention` to work around it |
| `StorageError:LegalHoldActive` | A version is under a legal hold | The hold has to be released by whoever placed it, through the process that placed it |
| `StorageError:VersioningStateUnknown` | The bucket reported a versioning status this code does not recognise | Establish what the bucket's versioning actually is; the worker refuses to guess what a delete would mean |
| `StorageError:InventoryUnsupported` | The storage will not enumerate object versions, so no erasure can be proved against it | A vendor decision. Either the bucket is unversioned (and the service must say so), or this storage cannot be used for data subject to erasure |
| `StorageError:ErasureNotImplemented` | The configured provider has no version-aware erasure | A code fix in the adapter. The worker deliberately does not fall back to a plain delete |
| `StorageError:AccessDenied` | The request was refused. With an owner guard configured this is also what an owner mismatch looks like — the service answers 403 | Grant `s3:DeleteObjectVersion` and `s3:ListBucketVersions`, or correct `STORAGE_S3_EXPECTED_BUCKET_OWNER`. Check the worker log line for which call was refused |
| `StorageError:AllAccessDisabled`, `StorageError:MethodNotAllowed` | The service refuses the operation outright | A bucket policy or vendor capability question |
| `StorageError:NoSuchBucket` | The configured bucket does not exist | Correct the bucket. Never an erasure: a missing bucket says nothing about the object |

**A lock is not a bug and not a backlog.** The worker holding a locked version is the retention
policy working. What needs deciding is a conflict between two obligations — the user's erasure
request and the lock — and that is a decision for whoever owns the retention policy, not a
configuration change. Escalate it as a conflict; §11.2 of
`docs/proposal-account-deletion-outbox.md` is where that goes.

### Reasons that retry by themselves

`StorageError:VersionsRemain` means the deletes reported success and the second enumeration
still found something. The usual cause is a concurrent writer adding a version between the two
passes, which the next attempt erases — so it retries on the ordinary backoff and only becomes
`needs_attention` after `ATTENTION_AFTER_ATTEMPTS`. If it reaches that point the service is
reporting deletions it is not performing, which is a vendor escalation.

`StorageError:InventoryPaginationBroken` means a truncated version listing arrived with no
marker to continue from, or the markers stopped advancing. The history could only be read in
part, and a partial read must never be taken for an empty one. Retried, because a malformed
response can be transient; persistent means a vendor escalation.

Throttles, 503s and transport faults (`SlowDown`, `ServiceUnavailable`, `InternalError`,
`EndpointConnectionError`) all retry as before.

### Checking an erasure by hand

Read-only, and the only question worth asking:

```
aws s3api list-object-versions --bucket <bucket> --prefix <key> --max-items 1000
```

Empty `Versions` and `DeleteMarkers` for that exact key is what "erased" means. A `404` from
`head-object` is not — it is what a delete marker looks like. Note that `--prefix` matches
prefixes, so check the `Key` field of each entry before concluding anything about one object.

### What is not covered yet

`erase` deletes the versions and delete markers of one key in one bucket. These are known gaps,
not claims of completeness:

* **Incomplete multipart uploads** retain their uploaded parts and are not listed as object
  versions. A resume upload interrupted mid-flight can leave parts behind that this does not
  remove. `ListMultipartUploads` / `AbortMultipartUpload`, a later stage.
* **Replicas** in another bucket or region are separate objects with their own version
  histories; deleting a version here does not delete them. Replication of delete markers is
  configurable, and deleting a *version* is not replicated by default at all.
* **Backups and snapshots** of the bucket or the database are out of reach of this worker
  entirely.
* **Single resume deletion** (a user deleting one resume rather than their account) still goes
  through `delete`, so on a versioned bucket it leaves the previous content as a non-current
  version with no deletion job recording it. Only account deletion is version-aware today.

Until those are closed, "all your data is permanently deleted" is not a claim this system can
support in full, and §11.2 of the proposal is where that is recorded.
