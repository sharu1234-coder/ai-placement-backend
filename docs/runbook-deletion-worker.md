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
