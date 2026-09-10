# Operations runbook

Production readiness item 16/20. Covers backup/restore, migrations, health checks, logging,
and incident recovery for `ai-placement-backend`.

**No production database exists yet** (see `PROJECT_HANDOFF.md` / the README's status log —
item 20 covers actual production deployment). Nothing in this document is "active" anywhere;
it's the procedure to follow once a real production database exists. Don't read any part of
this as a claim that backups are currently running against production data, because there is
no production data yet.

## Backup and restore

`scripts/backup_db.py` and `scripts/restore_db.py` work against whatever `DATABASE_URL` is
currently configured — the same scripts for local dev (SQLite or Postgres) and, later, a real
production Postgres instance. Nothing here requires a paid service.

### Backup

```bash
python scripts/backup_db.py                    # writes into ./backups/
python scripts/backup_db.py --out-dir /path/to/dir
```

- **PostgreSQL**: runs `pg_dump -Fc` (custom format — compressed, and the format
  `pg_restore --clean --if-exists` needs). Requires `pg_dump` on `PATH`, or set
  `PG_DUMP_PATH` to its full path (needed on this dev machine's Windows install, for example).
- **SQLite** (the dev default): copies the database file directly — a file copy *is* a
  correct backup for a single-file database; `pg_dump` doesn't apply.
- Output filenames are timestamped (`<db>_<UTC timestamp>.dump` / `.db`) so repeated backups
  never collide or silently overwrite each other.
- `backups/` is gitignored — a database dump must never be committed.

### Restore

```bash
python scripts/restore_db.py backups/ai_placement_20260824T010000Z.dump --yes
```

- Destructive by design (it replaces the target database's contents) — requires `--yes` to
  actually run; without it, the script only prints what it would do.
- Refuses to run if `ENVIRONMENT=production` unless `--allow-production` is also passed. This
  is a guard against a local restore accidentally pointed at a real production
  `DATABASE_URL` — not a recommended way to perform an actual production restore (see
  [Incident recovery](#incident-recovery) below for that).
- **PostgreSQL**: `pg_restore --clean --if-exists --no-owner`. Requires `pg_restore` on
  `PATH`, or `PG_RESTORE_PATH`.
- **SQLite**: copies the backup file back over the live database file.

### Verified locally, for real (not just read from the script)

Ran the exact backup → destroy → restore → verify cycle against a real local PostgreSQL 17
instance:

1. Created a fresh database, ran `alembic upgrade head`, signed up a real user through the
   running API (not inserted directly).
2. `python scripts/backup_db.py` → produced a real `.dump` file.
3. `DROP DATABASE` + `CREATE DATABASE` (the same name) — genuinely destroyed all data;
   confirmed the `users` table no longer existed.
4. `python scripts/restore_db.py <the dump> --yes` → completed without error.
5. Verified via direct SQL: the user row was back, `alembic_version` was back at the exact
   same head revision, and the seeded role taxonomy was back (11 rows).
6. Booted the app against the restored database and logged in with the original signup
   credentials — succeeded. This is the strongest check: it proves the password hash and
   every dependency the login path touches survived intact, not just that some rows exist.

### What this doesn't cover yet

- **No automated/scheduled backups anywhere** — there's no production database to schedule
  them against. Once one exists (item 20), the recommendation is to use whatever the hosting
  platform provides natively first (managed Postgres providers — RDS, Cloud SQL, Supabase,
  Render, etc. — all offer automated daily snapshots as part of the base service, not a paid
  add-on) before reaching for a custom cron job running these scripts. These scripts exist so
  that story isn't purely "point at the platform's dashboard and hope" — they're the
  documented, tested, provider-independent fallback.
- **No off-site/redundant backup storage** — local backups only. A real deployment needs
  backups to live somewhere other than the same disk/host as the database.
- **No backup encryption at rest** — a `.dump` file contains everything, including password
  hashes and reset tokens (hashed, but still). Treat backup files with the same care as the
  database itself.

## Migrations: apply and roll back

```bash
python -m alembic current              # what revision is this database at
python -m alembic history               # full migration chain
python -m alembic upgrade head          # apply every migration up to the latest
python -m alembic downgrade -1          # roll back exactly one migration
python -m alembic downgrade <revision>  # roll back to a specific revision
python -m alembic downgrade base        # roll back everything (dev/test only — see below)
```

`downgrade -1` is the realistic "the last deploy's migration needs to come back out"
operation. `downgrade base` is a full teardown — useful for verifying the whole chain works,
not something to run against real data.

### Verified locally, for real

Ran `alembic upgrade head` → `alembic downgrade base` → `alembic upgrade head` against a
fresh real PostgreSQL 17 database. All 13 migrations downgraded cleanly in reverse order
(including the two seed-data migrations, which just delete the rows they inserted) down to an
empty schema (only the `alembic_version` bookkeeping table left), then all reapplied cleanly.
Confirms the full round trip actually works, not just that `downgrade()` functions exist.

**Before deploying any migration that will need to be rollback-able**, test its `downgrade()`
locally the same way: `upgrade head` → `downgrade -1` → `upgrade head` against a real
Postgres instance, not just SQLite (SQLite doesn't enforce foreign keys by default — see the
FK-safety note in the account-deletion item's history — so it can silently accept a downgrade
order that would fail against Postgres).

### A migration that already ran in production

Alembic downgrades reverse schema changes; they don't attempt to recover data an `upgrade()`
step deleted or transformed destructively (e.g. a column drop). For anything beyond a schema
rollback, restoring from a backup taken before that migration ran is the only reliable
recovery path — see [Incident recovery](#incident-recovery).

## Health and readiness checks

Two separate endpoints, deliberately not one — they answer different operational questions
and a monitoring/orchestration system (e.g. a container platform's liveness vs. readiness
probes) needs to ask them differently:

- **`GET /health`** — liveness only: is the process up at all. No dependency checks, so a
  brief database blip doesn't get this instance killed by an orchestrator that restarts
  processes failing liveness checks.
- **`GET /health/ready`** — readiness: can this instance actually serve traffic right now.
  Checks (in order, each gating the next):
  1. The database is reachable (`SELECT 1` through the same session dependency every real
     request uses).
  2. The database's schema is at the migration revision *this deployment's code* expects
     (compares the `alembic_version` table against `alembic/versions/`'s actual head) — new
     as of this item. Catches "deployed new code but forgot to run `alembic upgrade head`":
     without this check the process starts fine and `/health` passes, and the first sign of
     trouble is a request failing against a column/table that doesn't exist yet.
  A database with no `alembic_version` table at all (this project's own test databases, which
  are created directly from SQLAlchemy metadata, not through Alembic) is treated as
  "untracked", not "out of date" — it doesn't fail this check.

  **Verified live** against real PostgreSQL: confirmed `schema: "current"` right after
  `alembic upgrade head`, then ran `alembic downgrade -1` against the same database while the
  server (still expecting the newer schema) kept running — `/health/ready` correctly flipped
  to `503 {"status": "unavailable", "schema": "out_of_date", "current_revision": ...,
  "expected_revision": ...}`.

## Safe operational logging

- Logs go to stdout/stderr (`logging.basicConfig`, no file handler) — the correct default for
  a containerized/platform-hosted deployment, where the platform captures and rotates
  stdout/stderr itself; a local rotating file handler would write to ephemeral container disk
  and be wrong for that deployment shape. Not changed by this item, confirmed still correct.
- Reviewed every logging call site in `app/` for secrets/PII (continues item 15's audit,
  extended here specifically for *operational* log safety):
  - The readiness check's database-connectivity failure previously logged the raw exception
    (`logger.error(..., exc)`) — some DB drivers' connection-failure messages can echo back
    the DSN they failed to connect with, including its embedded password. Now logs only
    `type(exc).__name__`.
  - Item 15 already fixed the other real finding (password reset codes logged in plaintext by
    the dev-only console email provider) by failing startup if that provider is still active
    in production — see that item's README entry.
  - No other call site logs a password, token, JWT secret, or full request body (re-confirmed
    by re-reading every `logger.*` call in `app/`, not just the ones touched by this item).
- **Not implemented**: request correlation IDs (a per-request UUID threaded through logs and
  returned in a response header, so a user's bug report can be matched to the exact server
  log lines for that request). Genuinely useful for incident debugging and cheap to add, but
  not part of this item's explicit scope — flagged here as a good candidate for a future
  pass, not built speculatively.

## Incident recovery

Concrete "something is wrong, what do I actually do" scenarios. This is a starting runbook,
not exhaustive — extend it as real incidents happen (a runbook a team has actually used a
few times is worth more than one written entirely in advance).

### Database unreachable

1. `GET /health/ready` — confirms it's actually the database, not something else, and
   whether the process itself is still up (`/health`) even though the database isn't
   reachable.
2. Check the hosting platform's database status page/dashboard first — most "unreachable"
   incidents are the database side, not this app.
3. If the database process itself is down and needs to come back from a backup, see
   [Backup and restore](#backup-and-restore) above — restore into a fresh database, then
   repoint `DATABASE_URL` at it once confirmed good (don't repoint before confirming: keep
   the broken instance around until the restore is verified).

### Schema out of date (`/health/ready` returns `schema: "out_of_date"`)

Means code was deployed without running `alembic upgrade head` against the same database
first (or a downgrade ran that the deployed code doesn't expect). Fix: run
`alembic upgrade head` (or the specific `downgrade`/`upgrade` needed to reach the revision the
running code expects) against that database, then re-check `/health/ready`.

### A bad migration reached production

1. If it's purely a schema change (no destructive data loss — e.g. an added column, added
   table, added index): `alembic downgrade -1` (see [Migrations](#migrations-apply-and-roll-back)),
   redeploy the previous code version, fix the migration, try again.
2. If it destroyed data (e.g. dropped/renamed a column with data in it): a downgrade only
   undoes the *schema* change, not the lost data. Restore from the most recent backup taken
   *before* that migration ran, accepting the data-loss window between that backup and the
   incident (this is exactly why backup frequency is a real tradeoff to make deliberately once
   a production database exists, not an afterthought).

### JWT secret suspected compromised

Rotating `JWT_SECRET_KEY` immediately invalidates every access and refresh token for *every*
user (not just one account) — the correct, if blunt, response to a suspected secret leak.
Every logged-in user has to log in again. Compare with `POST /auth/logout` (item 15), which
does the same thing but scoped to one user (via `token_version`) — appropriate for "this one
user's session was compromised", not for "the signing secret itself may have leaked".

### Suspected unauthorized data access / account compromise

1. `POST /auth/logout` as that user (or directly increment their `token_version` in the
   database) — invalidates every outstanding token for that account immediately.
2. Confirm no cross-user data exposure via the IDOR protections already audited and tested in
   item 15 — every resource-owning endpoint filters by the authenticated user's
   `profile_id`/`user_id`.
3. Direct a password reset (`POST /auth/forgot-password`) — item 15 made this also invalidate
   any tokens still outstanding once the user completes it.

### Rate-limit / abuse incident (someone hammering an endpoint)

Item 15's rate limits (`app/core/rate_limit.py`) already throttle the auth endpoints
specifically. For a different endpoint being hammered, or an attacker rotating IPs to get
around the per-IP limit, the immediate mitigation is at the infrastructure layer (the hosting
platform's/reverse proxy's own request throttling or IP blocking — not something this app-level
code can fully solve alone) — flagged as a real limitation, not solved here.

## Job Hunt: two limits, and telling them apart

Job Hunt refuses a search for two completely unrelated reasons, and an operator looking at a
support ticket needs to know which one it was before doing anything else. They have different
status codes precisely so that this never requires reading a message.

| Code | What it means | What fixes it |
|---|---|---|
| `402` | The student has no subscription and has already run their one free search. | The student buying Premium. Nothing operational. |
| `429` | Either the deployment's daily provider budget is spent, or the provider itself answered 429. | Waiting for the UTC day to roll over, or a larger provider plan. |
| `403` | An advanced filter (salary / employment type / experience level / date posted) was used without Premium. | The student removing the filter, or buying Premium. |
| `503` | No provider is configured, or the provider rejected our credentials. | An operator. See below. |

A premium subscription buys unlimited *Job Hunt*. It does not buy unlimited *provider calls* —
that ceiling belongs to the API key and is shared by everyone on the deployment. A paying
student can therefore still see a 429, and the message says so rather than implying they should
buy something they already have.

### Where each limit lives

* Per-student entitlement: `job_hunt_usages`, one row per operation.
  `free_use_slot` holds 1 for the row that spent the free search and NULL for every other row,
  under `UNIQUE (profile_id, free_use_slot)`. That constraint *is* the enforcement — there is no
  check-then-act step to lose under concurrency. Service: `app/services/job_hunt/entitlement.py`.
* Per-deployment provider budget: `provider_call_budgets`, one row per (provider, UTC day),
  incremented by a conditional UPDATE. Service: `app/services/job_hunt/provider_budget.py`.

### Answering "did this student really use their search?"

```sql
SELECT usage_type, status, was_premium, created_at, consumed_at
FROM job_hunt_usages
WHERE profile_id = :profile_id
ORDER BY created_at;
```

`status = 'succeeded'` with a non-null `consumed_at` is a search that produced real results.
`failed` is an attempt that was released because nothing was produced — the provider was down,
timed out, or answered with something unusable — and it does not count against the student.
`abandoned` means a worker died between claiming and settling; the allowance was reclaimed
automatically after ten minutes.

Rows are never edited to grant a search back. If a student genuinely needs another one, that is
a product decision, not an operational one.

### How much of today's provider budget is left

```sql
SELECT provider, usage_date, call_count
FROM provider_call_budgets
WHERE usage_date = CURRENT_DATE;
```

Jobvetta's documented ceiling is 50 per key per UTC day. The count is incremented *before* the
outbound request, so a call that dies in the socket still counts — this errs toward under-using
the key, which is the correct direction when the other mistake is a terms violation.

### Configuring the Jobvetta provider

```
JOB_PROVIDER=jobvetta
JOBVETTA_API_KEY=<from the environment's secret store, never committed>
```

Both are read from the environment only. With the key unset or blank, Job Hunt answers 503 and
the app shows "Job Hunt isn't available yet" — a real, honest state, not a broken one. It must
never be made to return an empty result list instead, because that would be a claim about the
job market rather than about our configuration.

Before trusting a newly configured key, run one authenticated search and check the log for
`Jobvetta: skipped N of M results that could not be parsed`. Jobvetta's *response* schema was
never supplied to us, so the adapter's field names (`_FIELD_ALIASES` in
`app/services/job_hunt/providers/jobvetta.py`) are educated guesses. A wrong guess shows up as
an empty results page plus that warning — never as an invented job — and correcting it is a
one-table edit.

Coverage is India only. Do not describe Job Hunt as covering any other market while this is the
configured provider.

### Alerts do not run for free students

`run_alert` requires an active subscription and records `last_run_status = "requires_premium"`
otherwise. This is not a paywall on notifications: a free student may still create an alert and
see what it would do. It is there because an alert is a search that runs on its own, which on a
one-search plan would be unlimited Job Hunt with a timer attached.

Note also that nothing in this repository calls `run_alert` on a schedule yet — there is no
cron, worker or queue. Alerts are configurable and do not fire.
