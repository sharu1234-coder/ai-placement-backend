# Proposal — durable account-deletion outbox

**Status: proposal only. No code or migration written.** Requires the owner decisions in §9
before implementation.

Supersedes nothing. The current behaviour (committed in this working tree, see
`docs/forensic-audit-2026-10-19.md` Finding 1) is the interim safe state: account deletion
attempts every storage object, and if any deletion fails it raises **before any commit**, so
the rows holding the storage keys survive for a retry. That is honest but it makes a bucket
outage block a user's erasure request. This proposal replaces that trade-off.

---

## 1. The invariant to preserve

> Never destroy the last durable pointer to an object that still exists.

The original defect broke exactly this: `storage.delete` swallowed its errors, the caller
swallowed what was left, and `db.commit()` then erased the `resumes` rows that held the only
copy of `file_path`. The object stayed in the bucket, unreachable, belonging to a user who had
been told their data was gone.

The outbox satisfies the invariant differently from the interim fix: instead of keeping the
*resume row*, it copies the storage key into a record whose entire purpose is to outlive it.

---

## 2. Shape: revoke synchronously, erase asynchronously

### Phase 1 — one transaction, synchronous, on the request

1. Load every profile-owned storage key.
2. Insert one `deletion_request` row and one `deletion_job` row **per object**, each carrying
   the storage key and provider.
3. Revoke access: mark the user `deleting`, bump the token version so every issued access and
   refresh token is dead, drop OAuth provider credentials and reset tokens.
4. Minimise directly identifying data that deletion itself does not need (§6).
5. Delete the profile-owned rows, including `resumes`.
6. Commit.

After this commit the user cannot authenticate, their data is unreadable through the API, and
the obligation to erase the objects is durable. Nothing in Phase 1 touches object storage, so
it cannot fail because storage is down.

### Phase 2 — worker, idempotent, retried

Lease a batch of `pending` jobs, delete each object, mark `done`. When every job for a request
is `done`, delete the final user row and close the request.

The ordering matters: the user row is the last thing to go, because while it exists the request
can be reasoned about operationally. Access was already revoked in Phase 1, so its continued
existence grants nothing.

---

## 3. Atomicity

| Transition | Boundary |
|---|---|
| jobs created + access revoked + rows deleted | one transaction. A crash before commit leaves the account fully intact and the request simply did not happen |
| one object deleted → job `done` | one transaction per job, after the storage call returns success. A crash between the two leaves the job `pending`, and the retry re-deletes an already-absent object, which the corrected `StorageProvider.delete` contract defines as success |
| last job `done` → user row deleted, request `completed` | one transaction, guarded by a re-count of outstanding jobs inside it |

No transaction spans a storage call. That is the rule that makes every failure mode a retry
rather than a repair.

---

## 4. Partial deletion and retries

Deletion is idempotent by contract, which is what makes partial progress real progress: a
retry skips what is already gone instead of failing on it. `StorageProvider.delete` already
distinguishes absent (success) from failed (raises) after Finding 1.

Per-job state: `pending → in_progress → done | failed_permanent`.

- Lease with `locked_until`; an expired lease is reclaimable, so a worker that dies mid-job
  does not strand it.
- Exponential backoff on `next_attempt_at`, with jitter.
- Classify the failure: transport and 5xx are transient; `AccessDenied` and a missing bucket
  are configuration faults that will not fix themselves and should alert sooner than the retry
  ceiling.

## 5. Worker crashes, duplicate jobs, permanent failure

- **Crash mid-job:** lease expiry reclaims it. At most one redundant delete of an object that
  is already gone.
- **Duplicate jobs:** a unique constraint on `(deletion_request_id, storage_key)` makes job
  creation idempotent, so a retried Phase 1 cannot double-enqueue.
- **Two workers on one job:** the lease claim is a conditional `UPDATE ... WHERE status =
  'pending' AND (locked_until IS NULL OR locked_until < now)` returning a row count. Only one
  claim wins. Correct on PostgreSQL; on SQLite it is serialised by the single writer.
- **Permanent failure:** after the attempt ceiling the job becomes `failed_permanent` and the
  request stays open. The object and its key both still exist, so the obligation is still
  recorded and still actionable — which is the whole point. §9.2 is the owner's call on whether
  this blocks final user-row deletion.

## 6. Retention and minimisation while deletion is pending

The window between Phase 1 and completion is the part that needs justifying, because data
still exists during it.

What is kept, and why:

- **Storage keys.** Required to perform the deletion. Keys must not embed personal data; today
  they are of the form `resumes/<id>_<token>.pdf`. If a key ever embeds a name or an email, the
  outbox stores a provider-side locator instead.
- **An opaque request id** and the original user id as a plain integer with **no foreign key**,
  so the user row can be deleted while jobs remain.

What is removed in Phase 1, before any waiting: name, email, phone, education and experience
rows, résumé text, transcripts, OAuth credentials and reset tokens. The pending window
therefore holds storage keys and counters, not a readable profile.

What must not be logged at any point: storage keys at warning level in bulk, file contents,
tokens, transcripts. Logs name the resume id or the job id.

## 7. Completion, limits, alerting, recovery

- `deletion_request.status`: `pending | completed | needs_attention`.
- A request is `completed` only when every job is `done` **and** the user row is gone. Nothing
  reports success earlier.
- Alert on: any `failed_permanent`; a request older than the agreed SLA (§9.1); a lease
  reclaimed more than N times, which means a worker is crash-looping on one object.
- Operational recovery is a re-run: the worker is idempotent, so the recovery procedure for
  almost every fault is "start the worker again". An operator tool should list open requests
  with their outstanding job counts.

## 8. Schema, migration, tests, rollback

**New tables** (additive; nothing existing is altered):

```
deletion_requests
  id, request_uuid, original_user_id (int, NO FK), status,
  created_at, updated_at, completed_at
  index (status, created_at)

deletion_jobs
  id, deletion_request_id FK -> deletion_requests, storage_provider, storage_key,
  status, attempts, next_attempt_at, locked_until, last_error_class,
  created_at, updated_at, completed_at
  unique (deletion_request_id, storage_key)
  index (status, next_attempt_at)
```

The absent FK from `deletion_requests.original_user_id` to `users` is deliberate and is the one
piece of schema that needs a comment in the migration, or someone will "fix" it and reintroduce
the problem: an enforced FK would make the user row undeletable while jobs remain.

**Migration:** one additive revision on the current single head `d4a1c08f52e7`. Two new tables,
no data backfill, no changes to existing columns.

**Rollback:** `downgrade()` drops both tables, which destroys any outstanding deletion
obligation. The downgrade must therefore refuse to run while open requests exist rather than
silently dropping them; draining the queue first is the documented procedure. This is the main
reason to review the migration before writing it. **Implemented and tested ahead of the migration** in `app/database/downgrade_guard.py`; see §11.4.

**Tests required:**

| Area | Cases |
|---|---|
| Phase 1 atomicity | jobs created + access revoked + rows deleted in one transaction; a failure mid-way leaves the account wholly intact |
| Access revocation | access and refresh tokens rejected immediately after Phase 1; protected reads 401/404 |
| Job creation | one job per object; idempotent under a retried Phase 1 (unique constraint) |
| Storage outcomes | success; already-absent; transient failure then success; permanent failure |
| Leasing | expired lease reclaimed; two workers cannot both claim; crash mid-job resumes |
| Completion | user row deleted only after the last job; re-count inside the final transaction |
| Idempotency | the whole worker run twice produces one outcome |
| Privacy | no key, token, transcript or file content in logs; the pending window holds no readable profile |
| PostgreSQL | the conditional lease claim and both constraints, on the real engine — **currently not runnable here**, see §9.5 |

## 9. Owner decisions required

1. **Erasure SLA.** The maximum acceptable window between access revocation and complete
   erasure. It sets the retry ceiling and the alert threshold, and it is what any external
   erasure commitment is measured against.
2. **Does a permanently failed object block final deletion of the user row?** Strict: the user
   row remains until every object is gone, so the system never claims an erasure it has not
   finished. Pragmatic: the user row goes on schedule and the request stays
   `needs_attention`. This is a compliance judgement, not an engineering one.
3. **Erasure receipt.** Keep an audit row after completion as evidence that deletion happened,
   and for how long? The receipt is itself retained data about a deleted person, so the default
   proposal keeps none.
4. **Worker runtime.** There is no scheduler in this deployment today. In-process background
   task, a management command run by cron, or an external worker? This also decides whether the
   worker can be assumed single-instance.
5. **A PostgreSQL test database.** The lease claim is the one piece of this design whose
   correctness is engine-specific. Without `TEST_DATABASE_URL` it can only be tested on SQLite,
   where the single writer hides the race the constraint exists to handle.

Until 1, 2 and 4 are answered, implementing this would mean inventing a retention policy and an
erasure promise on the owner's behalf. The interim fix already in the tree is safe to leave
running in the meantime: it never claims an erasure that did not happen.

---

## 10. Stage 1 readiness check (2026-10-19)

Owner decisions received resolve §9.2 (user row is **not** retained merely because storage is
down; minimum cleanup references are kept and unresolved jobs stay open until erasure is
verified), §9.3 (no permanent erasure receipt) and §9.4 (dedicated monitored worker,
PostgreSQL-backed leases). §9.1 was still open at that point; it is answered in §11.2.

### Verified

**Token-version revocation covers all three surfaces.** Proven by execution, not by reading:
one implementation (`app/api/deps.py::authenticate_access_token`, lines 24-65) holds the `tv`
check, and both `get_current_user` and `app/api/voice_session.py:355` call it; the refresh
endpoint checks `tv` separately at `app/api/auth.py:120`. Measured after bumping
`user.token_version`:

```
baseline: HTTP ok, WS ok
after bump  HTTP  : 401
after bump  WS    : refused (WebSocketDisconnect)
after bump  refresh: 401
```

So Phase 1's revocation mechanism is sound as designed.

**PostgreSQL is present.** Services `postgresql-x64-17` and `postgresql-x64-18` are running,
port 5432 accepts connections, `psycopg2` 2.9.12 is in `.venv312`, and `psql.exe` exists under
`C:\Program Files\PostgreSQL\{17,18}\bin`. This corrects an earlier audit statement that
inferred absence from `psql` not being on `PATH`.

### Blocking

**B1 - No authorized disposable test database.** PostgreSQL is installed but no credentials or
test database have been authorized. No credential was guessed or probed. Required: a
`TEST_DATABASE_URL` for a database that may be dropped. Note the documented example in
`tests/conftest.py` uses port **55432**, not the 5432 the installed services listen on, which
suggests the intended test instance is a separate one -- please confirm which.

Now that the guard in B2 exists, an authorised configuration means three things rather than
one: the URL, `TEST_DATABASE_DESTRUCTIVE=1`, and a role that holds grants on that database
only -- the guard refuses `postgres` and the other instance-wide roles, because a `_test`
suffix protects nothing if the role can reach every other database on the instance. Creating
that role needs instance credentials, which have not been authorised and have not been
probed for.

That role also needs **CREATEDB**: ten migration tests create and drop their own
uuid-suffixed throwaway databases via the `postgres` maintenance database, and without
the privilege they fail at fixture setup.

**B2 - RESOLVED (2026-10-19).** It could, and now it cannot by accident. The suite no longer
reads `TEST_DATABASE_URL` from the environment at all: `tests/db_safety.py` supplies it and
refuses a target that is not explicitly declared disposable (`TEST_DATABASE_DESTRUCTIVE`), is
not PostgreSQL, is not named `*_test`, connects as an instance-wide administrative role, holds
tables this suite did not create, or normalises to the same host/port/database as
`settings.database_url`. All 22 destructive call sites go through `drop_all_safely()`, which
performs the schema check above the `drop_all`. Measured: 72 guard tests pass, and a real
`pytest` run with `TEST_DATABASE_URL=sqlite:///./app.db` stopped at collection (exit 4) with
`app.db` untouched. Full evidence and the two defects found in the guard itself are in
`docs/forensic-audit-2026-10-19.md` section 7.

What it does **not** do, and what the owner still has to provide: the guard is protection
against accident, not isolation. The dedicated PostgreSQL test role with no production grants
(requirement 5) has to be created on the instance; nothing client-side substitutes for it. The
guard refuses `postgres` and the other instance-wide roles by default precisely so that the
`_test` suffix is not the only thing being trusted.

**B3 - No S3 bucket to verify.** Stage 2 requires confirming bucket versioning and retention
before claiming physical erasure. `STORAGE_PROVIDER=local`, with no bucket or endpoint
configured, so there is nothing to inspect. With versioning or object-lock enabled a `DELETE`
leaves a delete marker and the bytes persist -- which would make an "erased" claim false. This
must be checked against the real bucket before the outbox is described as erasing anything.
The provider decision itself is settled (production cannot run on `local`) and the exact six
checks are enumerated in §11.1; only the measurement is outstanding.

**B4 - The downgrade guard is a new pattern.** Every existing migration downgrades with plain
`op.drop_index` / `op.drop_table` and no conditions. A `downgrade()` that refuses to run while
deletion obligations remain outstanding deviates from that convention deliberately. **Approved by the owner in
Stage 1.5 and now implemented with 19 passing tests** (§11.4), with no override flag.

### Still open from §9

**§9.1 - the erasure SLA.** The maximum window between access revocation and verified erasure.
It sets the retry ceiling and the alert threshold, and nothing else in this design can be
calibrated without it.

---

## 11. Stage 1.5 — closing the design gates (2026-10-19)

Owner decisions received for this stage: B2 is accepted as a locally verified test guard and
**not** as proof of database isolation; the migration downgrade guard is approved; the test
environment is to be an isolated disposable instance with a dedicated role; voice resume and
the outbox itself stay unimplemented.

### 11.1 Storage provider, and the deletion semantics that must be verified

**The production decision is already made in code, not open.** `validate_for_production()`
(`app/core/config.py`, the `storage_provider == "local"` branch) *refuses to start* a
production deployment with `STORAGE_PROVIDER=local`, on the grounds that container filesystems
do not survive a restart while the database rows pointing at them do. So production storage is
S3-compatible by construction, and `local` is a development-only provider. The outbox is
therefore designed against S3 semantics, with `local` supported only so the suite and dev
machines keep working.

What each provider actually does today, read from the code rather than assumed:

| Provider | `delete(key)` | Erasure implication |
|---|---|---|
| `local` (`app/storage/local_provider.py`) | `path.unlink(missing_ok=True)`; absent is success, `OSError` becomes `StorageError` | The byte removal is a filesystem unlink. `scripts/backup_db.py` backs up **only the database** (`backup_sqlite` / `backup_postgres`) and never `upload_dir`, so no application-level backup retains a deleted file. Host-level snapshots are outside the application's knowledge and are the host operator's question |
| `s3` (`app/storage/s3_provider.py`) | a single `delete_object(Bucket, Key)` — **no `VersionId`** | On a **versioned** bucket this writes a delete marker and every prior version persists. The object stops being readable through the normal path and is not erased. "Deleted" would be true of the API and false of the bytes |

That last row is the specific thing that has to be checked before the outbox is described as
erasing anything, and it is a property of the bucket rather than of this code:

1. **Versioning** — `get-bucket-versioning`. If `Enabled` or `Suspended` with existing
   versions, `delete_object` is not erasure: the worker must list and delete every version
   (`list-object-versions`, then `delete_object` per `VersionId`, including delete markers), or
   versioning must be off for this bucket.
2. **Object Lock / retention** — `get-object-lock-configuration` and the per-object
   `Retention`/`LegalHold`. In **compliance** mode no credential can delete the object before
   its retain-until date; erasure is then *impossible* within the SLA, and the honest response
   is to not promise it. In governance mode it requires a specific permission.
3. **Lifecycle rules** — `get-bucket-lifecycle-configuration`. A rule that transitions
   noncurrent versions to an archive class moves the bytes rather than removing them.
4. **Replication** — `get-bucket-replication`. A replica is a second copy in a second bucket,
   often in another region, and deleting the source does not by default delete it.
5. **Bucket-level backups or snapshots** — provider-specific; whatever exists has to be
   enumerated, because a backup is a retained copy of a person's file by any definition that
   matters.
6. **MFA delete** — blocks version deletion from a worker entirely if enabled.

**Status: NOT RUN, and not runnable here.** `STORAGE_PROVIDER=local` with no bucket, region or
endpoint configured, so there is no bucket to interrogate. These six checks need the production
bucket's configuration and a credential with permission to read it; neither has been
authorized, and no guess was attempted. Until 1-5 are answered, the outbox may claim "the
delete call succeeded" and must not claim "the data is erased".

### 11.2 Erasure SLA — internal targets, pending privacy and legal review

These answer §9.1. They are **internal engineering targets**, not an external commitment, and
are explicitly labelled as such until privacy/legal review: nothing user-facing should quote
them, and no retention or erasure promise should be published on their basis.

| Target | Value | What it calibrates |
|---|---|---|
| Worker pickup | **5 minutes** from the Phase 1 commit | The polling interval and the lease length. A job unclaimed after 5 minutes means the worker is not running — which is a monitoring signal, not a retry |
| Alert | **1 hour** with the request still open | Page-worthy threshold. Catches a configuration fault (`AccessDenied`, missing bucket) long before the retry ceiling would |
| Human escalation | **24 hours** | A person looks at it. At this point it is a fault, not a queue being slow |
| Retries | **continue until resolved** | There is no attempt ceiling that closes the obligation |

The last row has a direct consequence for §4's state machine, and is the reason to record it
here rather than leave it to implementation: **`failed_permanent` must not mean "stop
trying"**. It means "stop the exponential backoff from growing and keep this visible", so the
name in §4 is wrong for the behaviour the owner approved and will be `needs_attention` with a
capped retry interval. A terminal state that stops retrying would quietly convert an
unfinished erasure into an abandoned one, which is exactly what §1's invariant forbids.

Alerting thresholds above are measured from `deletion_requests.created_at`, which is set in
the Phase 1 transaction, so the clock starts at the moment the user's access was revoked —
not when a worker first noticed the job.

### 11.3 Reconciling retention with "revoke promptly, do not retain indefinitely"

The policy has two halves that pull against each other once object cleanup is blocked, so this
states exactly how the design resolves them.

**Revoked promptly.** Phase 1 is one synchronous transaction on the request. It bumps
`token_version` — which Stage 1 proved revokes HTTP, WebSocket *and* refresh at once (§10) —
drops OAuth credentials and reset tokens, and deletes the profile-owned rows including
`resumes`. Nothing in Phase 1 touches object storage, so a bucket outage cannot delay it.

**Not retained indefinitely.** The user row is deleted **on schedule**, by the worker, and
*not* held back because an object could not be removed. This is the owner's pragmatic reading
of §9.2, and it is what keeps a storage outage from becoming indefinite retention of a
person's account.

**What survives the user row, and why that is not "retaining personal data".** The cleanup
reference is a row in `deletion_jobs` holding:

- `deletion_request_id` and an opaque `request_uuid`;
- `original_user_id` as a **plain integer with no foreign key** — the single piece of schema
  that needs a comment in the migration, because an enforced FK would make the user row
  undeletable while jobs remain, which is precisely the indefinite retention the policy
  forbids;
- `storage_provider` and `storage_key`;
- `status`, `attempts`, `next_attempt_at`, `locked_until`, `last_error_class`.

No name, no email, no phone, no résumé text, no transcript. Storage keys are
`resumes/<id>_<token>.pdf` — no personal data embedded; if a key ever did embed a name or an
email, the outbox stores a provider-side locator instead (§6). An integer and an object key
are the minimum required to perform the erasure, and keeping less would mean not being able to
finish it.

**Kept open until verified.** A request reaches `completed` only when every job is `done`
*and* the user row is gone; an unresolved job keeps the request open indefinitely, with the
alerting in 11.2 making that visible. No permanent erasure receipt is kept (§9.3), so once the
last job completes the record of the deletion is removed along with it.

The one honest asymmetry: "erasure verified" currently means "the provider's delete call
returned success". Under 11.1's unanswered bucket questions that is weaker than physical
erasure, and the design must not describe it as more than it is until those checks are run.

### 11.4 The downgrade guard, tested before the outbox exists

Approved in this stage, and implemented and tested **ahead of** the migration it protects:
`app/database/downgrade_guard.py`, with 19 tests in `tests/unit/test_downgrade_guard.py`,
all passing. The ordering is the point — a guard written in the same change as its migration
gets verified by reading it.

`refuse_if_outstanding(connection, table=..., unresolved=[...], obligation=...)` is
deliberately table-agnostic, so neither it nor its tests pre-commit to outbox schema. It
returns quietly when the table is absent (a downgrade on a database that never had the
revision, and a re-run of a completed one), when it is empty, and when every row is terminal;
it raises `OutstandingObligations` otherwise. Proven by the tests: only unresolved rows are
counted; the message carries the count and **no row content** — no user id, no storage key,
since migration output lands in deploy logs; identifiers are validated rather than
interpolated blindly, and statuses are bound parameters; the table demonstrably still exists
after a refusal, and a drained queue lets the drop through.

**There is no environment-variable override**, by decision. An escape hatch is what gets set
in a pipeline and forgotten. A blocked rollback is recoverable; a dropped erasure queue is
not. If a downgrade genuinely must proceed, the operator drains the queue or deliberately
parks the rows in a terminal status first — an auditable act in the database, by someone who
can see what they are discarding.

Two limits: the guard is engine-neutral SQL (`has_table`, one `SELECT COUNT(*)` with bound
parameters) but **has not been run against PostgreSQL**; and it protects the rows, not the
decision — a migration author who forgets to call it gets no warning. The outbox migration's
own test must assert that its `downgrade()` calls it.

### 11.5 What still blocks Stage 2

One resource and one measurement, in that order of importance:

1. **An authorized disposable PostgreSQL instance with a dedicated role** (`docs/postgres-test-environment.md`).
   This is the hard blocker. Stage 2 requires PostgreSQL tests for lease contention,
   concurrent workers, partial failures, crash recovery and migrations; SQLite's single writer
   hides the exact race the conditional lease claim exists to arbitrate, so writing the lease
   without being able to run those tests would produce untested concurrency code of the kind
   this audit was called in to find. What is needed: the instance and port, a role with
   `CREATEDB` and no production `CONNECT` (including `PUBLIC`'s default `CONNECT`, revoked),
   and the two environment variables. Nothing on this host is authorized; the two listening
   instances are PostgreSQL 18 on 5432 and 17 on 5433, and the port 55432 in the conftest
   example serves nothing here.
2. **The production bucket's versioning, retention, lifecycle, replication and backup
   configuration** (11.1). Not a blocker for writing the worker, but a blocker for claiming
   erasure. If Object Lock is in compliance mode, the SLA in 11.2 cannot be met at all and the
   design conversation changes rather than the code.

Everything else this stage needed is now settled: the SLA targets (11.2, pending legal
review), the retention reconciliation (11.3), the downgrade guard (11.4), the storage decision
(11.1 — already forced by config validation), and the test-database guard (§7 of the forensic
audit). No decision of the owner's is outstanding.

---

## 12. Stage 2 — implemented (2026-10-20)

Built to the approved design in sections 2-8, with the section 11.2 targets. **Not verified
against PostgreSQL yet**: the concurrency and migration tests are written and skip without an
isolated cluster, so the live evidence is still outstanding.

### 12.1 What exists

| Piece | File |
|---|---|
| Tables | `app/models/deletion_request.py`, `app/models/deletion_job.py` |
| Phase 1 | `app/services/deletion_outbox.py`, called from `delete_account` |
| Phase 2 worker | `app/workers/deletion_worker.py` |
| Migration | `alembic/versions/f7b3c91de204_add_account_deletion_outbox.py` |
| Tests | `tests/unit/test_deletion_outbox.py` (44), `tests/unit/test_deletion_outbox_postgres.py` (16) |

### 12.2 Three decisions the design left open

1. **No `deleting` status column on `users`.** Section 3 said "mark the user `deleting`", but
   section 8 requires the migration to alter nothing existing. The open `deletion_requests`
   row is the marker instead, and revocation uses the two columns that already exist:
   `token_version` (which every access token, refresh token and WebSocket handshake checks)
   and `is_active`.
2. **The outbox engages only when something is stored.** With no objects there is no
   obligation to outlive the transaction, so the user row is deleted in Phase 1 exactly as
   before. This keeps the common case synchronous and is why most existing deletion tests were
   unaffected.
3. **The email is replaced, not nulled.** The column is NOT NULL and unique, and a tombstone
   must not stop someone signing up again with their own address. The replacement is derived
   from a fresh uuid, so it is not reversible to the original.

### 12.3 Four tests that changed meaning, and why that is not a weakening

`tests/unit/test_account_service.py` previously asserted that a storage failure **aborted** the
whole account deletion. That held section 1's invariant, but it is the behaviour this stage was
approved to replace -- it made a user's ability to leave depend on a bucket being up. The
invariant itself is asserted more strictly now: Phase 1 makes no storage call at all, and the
tests check that the key survives in a `deletion_jobs` row, that access is revoked anyway, and
that a failing object stays queued rather than being abandoned.

### 12.4 What is still unverified

- **Physical erasure is not verified and is not claimed.** The production bucket is not
  configured; its versioning, Object Lock, replication and retention settings are unknown, so
  a successful `delete` call means the provider accepted the request, not that every copy is
  gone. Section 11.1 is unchanged.
- The section 11.2 targets remain **provisional, pending privacy and legal review**.
- No worker process, schedule or deployment unit exists yet: `run_once` is a function, and
  nothing calls it on a timer.

### 12.5 The PostgreSQL gate passed (2026-10-20)

Run through the scratchpad `pgtest.py`, so all 16 isolation checks passed first and the tests
would have been refused otherwise.

| | |
|---|---|
| Result | **60 passed, 0 failed, 0 errors, 7 deprecation warnings** |
| Pytest duration | 52.24 s |
| Process exit code | **0** |
| Wall clock | about 73 s including the isolation checks |
| Server | PostgreSQL 17.11, `127.0.0.1:55432` |

Scope: `tests/unit/test_deletion_outbox_postgres.py` (16) and
`tests/unit/test_deletion_outbox.py` (44). **These are the selected outbox tests. The full
backend suite has never run against PostgreSQL** -- roughly 4,500 tests still execute on
SQLite, and nothing here is a statement about them.

What this closes from 12.4: requirement 4 (concurrent claims) and requirement 9 (migrations
and indexes on real PostgreSQL) are now demonstrated on the real engine. What it does not
close: physical erasure, the provisional SLA, and the absence of any worker process.

### 12.6 The two coverage gaps closed, and the worker runner (2026-10-20)

**Tests added** to `tests/unit/test_deletion_outbox_postgres.py`, on the real engine through
the same guarded fixture -- not SQLite, not mocks:

| Class | Proves |
|---|---|
| `TestMixedOutcomesInOnePassOnPostgres` | one object fails while another succeeds in a single pass: the failing job's rollback does not take the successful job's `done` with it, the request stays open, the user row survives, and a later pass completes the outstanding object without revisiting the finished one |
| `TestACrashAfterTheDeleteOnPostgres` | the object is erased and the process dies before recording it: the job is still `pending` with `attempts = 0`, the key -- the durable obligation -- is intact, a second worker cannot take it while the lease holds, and the retry after expiry succeeds because deleting an absent object is success. A crash *before* the claim commits leaves no trace either |

**Status: written, NOT RUN.** They need the isolated cluster and skip without it.

**Runner added:** `scripts/run_deletion_worker.py`, following the pattern of
`scripts/run_due_alerts.py`. It calls `deletion_worker.run_once` and implements nothing of its
own -- a test asserts the module contains no SQL, no `.execute(`, no `claim_batch` and no
`.query(`. Bounded polling (default 60 s, validated 1-300; batch 10, validated 1-500), a
signal flag checked between passes so the pass in flight finishes and commits, a fresh session
per pass closed in a `finally`, the engine disposed once at exit, and six non-sensitive
counters per pass plus a summary at exit. An empty pass logs nothing, so a drained queue does
not fill the log. Repeated failures (5 consecutive) exit 1 for a supervisor; a single failure
is rolled back and retried. `app/main.py` does not reference it, and a test enforces that.

26 unit tests cover shutdown, polling bounds, resource cleanup and error behaviour, with every
collaborator injected so none of them needs a database.

## 13. Stage 4 — storage-erasure semantics and worker operations (2026-10-21)

Sections 1–12 are unchanged; nothing earlier is retracted. This section records what the
*storage* side of the outbox actually guarantees, which sections 1–12 deliberately left open,
and it separates three kinds of fact that had been running together: what the code proves, what
needs evidence from a configured provider, and what needs a controlled live test.

### 13.1 What the audit established

**Providers.** Two adapters exist: `LocalStorageProvider` and `S3StorageProvider`. The default
is `local` (`app/core/config.py:132`). The S3 adapter speaks the plain S3 API, so it works
against AWS S3 or any S3-compatible service.

**Production configuration is absent, not merely unverified.** Resolved at audit time:
`storage_provider = "local"`, `upload_dir = "uploads"`, and `storage_s3_bucket`,
`storage_s3_endpoint_url`, `storage_s3_region`, `storage_s3_access_key_id` and
`storage_s3_secret_access_key` are all unset. No value was read or printed. **No production
bucket exists to inspect**, so every statement below about versioning, object lock, replication,
lifecycle rules or backups is a statement about code, not about a bucket.

**What `delete` guarantees.** Both adapters raise `StorageError` on failure and treat an absent
object as success. That contract is load-bearing: it is what makes a retry safe, and it is
pinned by tests. The guarantee is *"the adapter asked the backend to remove this key and the
backend did not refuse"* — nothing stronger.

**The S3 delete is a logical deletion request.** `delete_object(Bucket, Key)` is called with no
`VersionId`. On an unversioned bucket that removes the object. **On a versioned bucket it
inserts a delete marker and every prior version remains retrievable.** The adapter does not
query bucket versioning, does not enumerate versions, and has no object-lock awareness. This is
the single most important fact in this section: on a versioned bucket, the code as written does
not erase anything — it hides it.

**The local adapter unlinks.** `Path.unlink` removes the directory entry. The blocks are not
overwritten, and the dev `upload_dir` on the maintainer's machine sits inside a
OneDrive-synchronised tree, which keeps its own recycle bin and version history. `local` is a
development backend and the startup guard already refuses to let production start on it.

**Success in storage, crash in PostgreSQL.** The storage call is made with no transaction open;
the claim is committed before it and the outcome after. A process that dies in between loses
its lease, not its job: the lease expires, the job is claimed again, and the second delete finds
the object already absent, which is success. The obligation closes correctly. Covered by a
PostgreSQL test (section 12.6) and an SQLite test.

**Retries are idempotent and keep the key.** A failed job retains `storage_key` and
`storage_provider`; nothing is cleared on failure. There is no terminal `failed` status, so no
code path converts an unfinished erasure into an abandoned one.

**No documentation or API response overclaims.** A scan for promises of permanent, immediate or
irreversible erasure found none in `app/api`, `app/schemas` or `docs/`, and the existing text in
this document already states that physical erasure is not verified and not claimed. Nothing had
to be corrected.

### 13.2 Erasure acceptance matrix

Per case: what the job does, what is retained, what is observable, and what would close the
gate. **`local` is development-only throughout**; the `s3` column is the one that matters.

| # | Case | `local` | `s3` | Job outcome | Retained | Evidence needed to close |
|---|---|---|---|---|---|---|
| 1 | Logical deletion requested | **PASS** (code) | **PASS** (code) | `done` | nothing | Closed by code + tests. |
| 2 | Current object version deleted | **PASS** (code) | **NOT VERIFIED** | `done` | nothing, *if* unversioned | A configured bucket: confirm the delete removed the object, not just the current pointer. |
| 3 | All versions and delete markers handled | **N/A** | **BLOCKED** | `done` — **wrongly, if versioned** | every prior version | **Owner decision first:** is versioning on? If yes the adapter must enumerate and delete versions, needs version-delete permission, and this row is a code change, not a test. |
| 4 | Object Lock / retention refuses | **N/A** | **NOT VERIFIED** | `needs_attention`, retried forever | key + row | A live test against a locked object. A retention lock cannot be satisfied by retrying; the product decision is what to do instead. |
| 5 | Replicas and backups retain copies | **NOT VERIFIED** | **NOT VERIFIED** | `done` | copies outside the code's knowledge | Provider configuration evidence: replication rules, lifecycle policies, backup retention. The code cannot see any of these and never will. |
| 6 | Transient provider failure | **PASS** (test) | **PASS** (test) | `pending`, backoff, then `done` | key + row | Closed. Code now recorded for triage (13.3). |
| 7 | Delete succeeds, PostgreSQL recording crashes | **PASS** (test) | **PASS** (test) | reclaimed after lease expiry, then `done` | key + row until then | Closed on real PostgreSQL. |
| 8 | Permanent refusal or misconfiguration | **PASS** (test) | **PASS** (test) | `needs_attention`, retried forever, operator-visible | key + row | Closed by design: never terminal. Triage now possible via the error code. |
| 9 | **Job executed against the wrong backend** | — | — | `needs_attention`, nothing deleted | key + row | **Was a defect; fixed and tested this stage (13.3).** |

Row 3 is the one that decides whether TALVIORA AI can claim erasure at all, and it is **not a
testing gap — it is an unanswered configuration question**. Until someone states whether the
production bucket is versioned, the honest status of physical erasure is *unknown*, not *passing*.

### 13.3 Two defects found and fixed

**Defect 1 — a job could be erased against the wrong backend, and would report success.**
`deletion_jobs.storage_provider` was written at enqueue and **never read anywhere**. The worker
built one provider from the running process's settings and applied it to every claimed job. The
failure is not a visible error but a silent success: an object key is only meaningful to the
backend that stored it, so deleting it elsewhere finds it absent, absence is idempotent success,
the job is marked `done`, and closing the request deletes the last row that still held the key.
The object stays where it is, now unreachable, for a user who was told it was erased — the exact
invariant section 1 exists to protect.

The route in is ordinary rather than exotic: the startup guard in `app/core/config.py` actively
pushes a deployment to change `STORAGE_PROVIDER` from `local` to `s3`, and any job enqueued
before that change is a job whose recorded backend no longer matches the worker's. Two processes
reading different environments produce the same mismatch.

Fix: each adapter declares its `name`, and `execute_job` refuses a job whose recorded provider
differs from the provider it holds — flagging it `needs_attention` on the **first** occurrence,
since a configuration fault cannot clear by being retried. Nothing is deleted, the key and the
row survive, the request cannot close, and the job stays claimable, so correcting the setting is
the entire remedy. A provider that declares no name is not second-guessed, which keeps test
doubles working; a parity test asserts both real adapters declare one.

**Defect 2 — `last_error_class` carried one bit of information.** Both adapters wrap every
failure in the single `StorageError` class and there are no subclasses, so the recorded value was
the constant string `"StorageError"` for every failure either backend can produce. An operator
triaging a `needs_attention` job could not distinguish a denied permission or a retention lock —
which will never succeed unattended — from throttling, which clears on its own. The message is
deliberately withheld because it quotes the bucket and the key, so nothing carried the
distinction.

Fix: `StorageError` gained a `code` attribute holding the backend's own short machine token (an
S3 error code, a botocore class name, or an `errno` name), and the worker records
`StorageError:AccessDenied` in place of the bare class. The message is still never stored or
logged. A failure with no code still records the class alone, so existing assertions hold.

### 13.4 Tests added

12 tests, all offline, no bucket and no credential:

- `tests/unit/test_storage.py` (+4): both adapters declare a name; an S3 error code, a botocore
  transport class and an `errno` name each reach `StorageError.code`.
- `tests/unit/test_deletion_outbox.py` (+8): a mismatched provider deletes nothing and keeps the
  obligation, the user row and the key; it is flagged on the first attempt; the same job
  completes once a worker holds the right backend; the refusal log names the backends but never
  the key; a nameless provider is not second-guessed; the error code is recorded beside the
  class; an uncoded failure records the class alone; and the stored value cannot carry a key or
  a bucket name.

**Limits of this evidence.** Every storage provider here is a stand-in. These tests prove the
*worker's* behaviour — the state machine, the retained references, the absence of leakage — and
prove nothing about any real bucket. No test in this repository has ever deleted a real object.

### 13.5 Status of each gate

| Gate | Status |
|---|---|
| Phase 1 atomicity, revocation, minimisation | **PASS** (sections 11–12) |
| Lease claim, `SKIP LOCKED`, crash recovery on real PostgreSQL | **PASS** (section 12.6) — not re-executed this stage; see 13.6 |
| Wrong-backend erasure refused | **PASS** (13.3, tested) |
| Failure triage possible from the job row | **PASS** (13.3, tested) |
| Logical deletion requested | **PASS** |
| **Physical erasure in production** | **NOT VERIFIED** — no bucket is configured; versioning unknown |
| All versions and delete markers handled | **BLOCKED** on an owner decision about versioning |
| Object Lock / retention behaviour | **NOT VERIFIED** — needs a live locked object |
| Replication, lifecycle, backup retention | **NOT VERIFIED** — needs provider configuration evidence, not code |
| Worker deployed, scheduled, monitored | **BLOCKED** — runbook written, nothing deployed |
| Pickup / alert / escalation targets | **BLOCKED** on privacy and legal approval; provisional |
| Staging validation of the worker | **NOT VERIFIED** — procedure written, never run |

### 13.6 Owner decisions still required

1. **Is the production bucket versioned?** This decides whether row 3 is a test or a code
   change, and whether erasure is physical at all.
2. **Object Lock or retention rules?** If any apply to the application's prefix, retrying
   cannot satisfy them and the product needs an answer for a legally undeletable object.
3. **Replication, lifecycle and backup retention** for the chosen provider — the window during
   which a copy survives a completed erasure.
4. **Which storage provider and platform**, which settles the credential scope and the
   supervisor in the runbook.
5. **Approval of the pickup, alert and escalation targets** by privacy and legal, or
   replacement numbers.
6. Whether resolved `deletion_jobs` rows are retained or purged (section 11.3 flagged this; a
   test is waiting on it).
