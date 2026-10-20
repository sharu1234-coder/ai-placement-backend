# The PostgreSQL test environment — requirements, and what exists today

**Status: no authorized disposable instance exists. Nothing in this document has been
connected to.** No credential was guessed, no password file was read, no connection was
attempted. Everything under "What is actually here" was measured from service metadata,
listening sockets and configuration files on this host.

This document exists because the test suite is destructive: with `TEST_DATABASE_URL` set it
drops every table before each test, and ten migration tests create and drop whole databases.
`tests/db_safety.py` refuses a target that has not been proven disposable, but that guard
protects against *accident* only. Isolation is a property of the database, and this is the
list of what has to be true of it.

---

## 1. What is actually here

| Instance | Service | State | Port | Data directory |
|---|---|---|---|---|
| PostgreSQL 18 | `postgresql-x64-18` | Running (pid 5648, postmaster 8016) | **5432** | `C:\Program Files\PostgreSQL\18\data` |
| PostgreSQL 17 | `postgresql-x64-17` | Running (pid 5612, postmaster 7616) | **5433** | `C:\Program Files\PostgreSQL\17\data` |
| PostgreSQL 15 | none | **Not installed** — no service and, as §4.1 records, no `bin` directory either; only a leftover `data` folder | 5432 in that leftover config | `C:\Program Files\PostgreSQL\15\data` |

Measured: `Get-CimInstance Win32_Service -Filter "Name LIKE 'postgresql%'"` and
`netstat -ano | grep LISTENING`. Sockets 5432 and 5433 are listening; **nothing is listening
on 55432**, and `docker` is not installed on this host.

That last point corrects a standing assumption. `tests/conftest.py` documents its example as
`127.0.0.1:55432`, which is a port no process on this machine serves — so the example was
written for an environment that does not exist here (a container, most likely). **The ports
that do exist are not authorization to use them.** 5432 is PostgreSQL 18 holding whatever
databases it holds; what those are has not been looked at, because looking requires
credentials.

Both instances are configured `listen_addresses = '*'`, so each accepts TCP from any
interface the host firewall permits, not just loopback. `pg_hba.conf` (17) requires
`scram-sha-256` for local, `127.0.0.1/32` and `::1/128`. No trust-authenticated line exists,
which is the one thing here that is reassuring: an unauthenticated connection would not
succeed even by accident.

**Three major versions are installed side by side.** For a disposable test target that is a
liability rather than a convenience: `psql` and `pg_dump` resolve by `PATH` order, and
`tests/scripts/test_backup_restore_roundtrip.py` shells out to `pg_dump`/`pg_restore`. A
`pg_dump` from 15 against an 18 server fails; the reverse direction can silently produce an
archive the older `pg_restore` cannot read. Whichever instance is authorized, the matching
`bin` directory has to be the one on `PATH` for that run.

---

## 2. What an authorized disposable instance has to satisfy

### 2.1 Identity, stated explicitly

The owner names: the instance (one of the two above, or a new one), its port, and the database
name. The database name must end in `_test` — `tests/db_safety.py` enforces that and refuses
a URL that normalizes to the same host, port and database as `settings.database_url`.

A **separate cluster** is materially safer than a separate database on a shared one, and is
the recommendation. A role with `CREATEDB` on a shared instance can create and drop databases
*on that instance*, and `pg_database` is readable by any role, so a test role on the
production cluster can always enumerate what else is there. A second cluster on another port
costs one service and removes that entire class of question.

### 2.2 The role

```sql
-- Run by someone holding instance credentials. NOT run by this audit.
CREATE ROLE talviora_test WITH LOGIN PASSWORD '<generated>'
    CREATEDB            -- required: see 2.3
    NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT;
```

- **`CREATEDB` is required, not optional.** Ten migration tests
  (`test_migration_model_parity.py`, `test_career_goal_migration.py` and eight siblings)
  connect to the `postgres` maintenance database and run
  `CREATE DATABASE "<name>_test_<uuid8>"`, migrate it, then `DROP DATABASE ... WITH (FORCE)`.
  Each drops only the uuid-suffixed database its own fixture created moments earlier. Without
  `CREATEDB` those ten fail at fixture setup rather than at an assertion.
- **`NOSUPERUSER` is the requirement `tests/db_safety.py` cannot check for.** The guard
  refuses the role *names* `postgres`, `root`, `admin`, `sa`, `superuser` and `rdsadmin`, which
  catches a pasted connection string; it cannot see that a differently-named role was granted
  superuser. That has to be true at the database.
- **Role membership is where this quietly goes wrong.** `NOSUPERUSER` means nothing if the
  role is a member of a role that holds production grants, because `INHERIT` makes those
  privileges effective without any `SET ROLE`. The requirement is: `talviora_test` is a member
  of no role other than those created for testing. Verify, do not assume:

```sql
SELECT r.rolname AS member_of
  FROM pg_auth_members m
  JOIN pg_roles r ON r.oid = m.roleid
 WHERE m.member = 'talviora_test'::regrole;      -- expected: zero rows
```

### 2.3 Effective CONNECT privileges

This is the check most often skipped, because the default is permissive: **`PUBLIC` holds
`CONNECT` on every database unless it is revoked.** A freshly created, superuser-free,
no-membership role can therefore connect to the production database on the same instance. The
role's own grants prove nothing on their own.

```sql
-- On every database that is not the test target:
REVOKE CONNECT ON DATABASE <app_database> FROM PUBLIC;
REVOKE CONNECT ON DATABASE <app_database> FROM talviora_test;

-- Grant only what is needed:
GRANT CONNECT ON DATABASE <test_database> TO talviora_test;
GRANT CONNECT ON DATABASE postgres       TO talviora_test;  -- for CREATE DATABASE, 2.2
```

`GRANT CONNECT ON DATABASE postgres` is unavoidable given the migration tests' pattern. It is
the maintenance database, and it should hold nothing; if it holds anything, that is worth
knowing separately.

The verification that matters is the **negative** one — an attempted connection to the
application database by the test role that is *refused*:

```sql
SELECT has_database_privilege('talviora_test', '<app_database>', 'CONNECT');  -- expected: f
SELECT has_database_privilege('talviora_test', '<test_database>', 'CONNECT'); -- expected: t
```

A `false` from the first query is the only evidence in this document that would constitute
proof of isolation rather than an assertion of it. Nothing weaker should be recorded as
isolation, including the fact that the test suite's guard refused a bad URL.

Schema-level, on the test database: PostgreSQL 15 removed `PUBLIC`'s implicit `CREATE` on the
`public` schema, so the role needs `GRANT CREATE ON SCHEMA public TO talviora_test` — or it
owns the database, which is simpler and is what `CREATEDB`-created databases give it anyway.

### 2.4 Network isolation

Both installed instances are `listen_addresses = '*'`. A disposable test instance should be
`listen_addresses = 'localhost'`, or firewalled to the host, for the straightforward reason
that a database whose entire contents may be dropped at any moment by a `pytest` run should
not be reachable by anything that might mistake it for a real one. If the authorized instance
is one of the two above, changing `listen_addresses` is a config edit and a restart, which
affects whatever else uses that instance — so this is a decision, not a cleanup.

### 2.5 The variables, once it exists

```
TEST_DATABASE_DESTRUCTIVE=1
TEST_DATABASE_URL=postgresql+psycopg2://talviora_test:<password>@127.0.0.1:<port>/talviora_test
```

Both are required. `TEST_DATABASE_ALLOW_ADMIN_ROLE` must **not** be set for a long-lived
instance; it exists for CI's ephemeral container, whose single role is its superuser and whose
whole instance dies with the job.

---

## 3. Verification checklist

Items 1-6 ran on 2026-10-20 and passed. Section 5 records the superset of checks that
replaced this list and what was actually measured. **Item 7 has not run.**

1. `SELECT version()` — records which instance was actually reached, against which `bin` is on `PATH`.
2. `SELECT current_user, session_user, usesuper FROM pg_user WHERE usename = current_user;` — `usesuper` false.
3. The membership query in 2.2 — zero rows.
4. Both `has_database_privilege` queries in 2.3 — false for the application database, true for the test database.
5. An actual connection attempt to the application database as the test role — refused.
6. `SELECT datname FROM pg_database;` — records what else lives on this instance, which decides whether 2.1's separate-cluster recommendation was followed.
7. `pytest tests/api tests/unit/test_business_claim.py` with the two variables set — the first real PostgreSQL run of this suite.

Only after 1-6 should 7 be run, and only 7's output may be reported as PostgreSQL coverage.
1-6 are now done (section 5). Item 7 remains the only line here that would constitute
PostgreSQL *coverage*, and it is still outstanding.

---

## 4. Provisioning plan — a dedicated disposable cluster (Stage 1.6, 2026-10-20)

**Nothing in this section has been executed.** No `initdb`, no role, no database, no service
started or stopped, no firewall rule, no configuration file written, no connection made and no
credential read, guessed or probed. It is a plan awaiting authorization.

### 4.1 Intended version: PostgreSQL 17

Three sources agree, so this is not a preference:

| Source | Says |
|---|---|
| `.github/workflows/ci.yml` | `image: postgres:17` for the `test-postgres` job |
| `docs/PRODUCTION_DEPLOYMENT.md` | "PostgreSQL 15+ ... Verified against 17.11"; the migration round-trip is recorded as "Verified on PostgreSQL 17.11" |
| `OPERATIONS.md` | the backup → destroy → restore → verify cycle was run "against a real local PostgreSQL 17" |

Using 18.4 instead would test a different major version than CI and than the deployment
documentation, which is the opposite of what the PostgreSQL gate is for. **Correction to §1 of
this document:** PostgreSQL 15 is *not* installed — `C:\Program Files\PostgreSQL\15\` contains
a `data` directory and no `bin`, so it is a leftover data directory from an uninstalled
release, with no executables and no service. It is not a candidate.

### 4.2 Binaries — all 17.11, verified by `--version`

Directory: `C:\Program Files\PostgreSQL\17\bin\`

| Executable | Version reported |
|---|---|
| `initdb.exe` | initdb (PostgreSQL) 17.11 |
| `pg_ctl.exe` | pg_ctl (PostgreSQL) 17.11 |
| `postgres.exe` | postgres (PostgreSQL) 17.11 |
| `psql.exe` | psql (PostgreSQL) 17.11 |
| `pg_dump.exe` | pg_dump (PostgreSQL) 17.11 |
| `pg_restore.exe` | pg_restore (PostgreSQL) 17.11 |

The 18.4 set exists at `C:\Program Files\PostgreSQL\18\bin\` and must not be the one on `PATH`
for a test run: `tests/scripts/test_backup_restore_roundtrip.py` shells out to `pg_dump` and
`pg_restore` by name, and an 18 `pg_dump` against a 17 server fails outright while the reverse
can produce an archive the older `pg_restore` cannot read. Whichever shell runs the suite
should put the 17 `bin` first explicitly.

### 4.3 Current state, confirmed without connecting

| Cluster | Service | StartMode | Port | Confirmed by |
|---|---|---|---|---|
| PostgreSQL 18.4 | `postgresql-x64-18` | **Auto** | 5432 (pid 8016) | `Win32_Service`, `netstat -ano` |
| PostgreSQL 17.11 | `postgresql-x64-17` | **Auto** | 5433 (pid 7616) | same |

Neither is to be reused: §2.1 of this document explains why a separate cluster is materially
safer than a separate database, and reuse would require the authorization and the proof of
isolation that do not exist.

**Port 55432 is free and usable.** Nothing in the 55430-55439 range is listening, and the
Windows reserved TCP range on this host is only 50000-50059
(`netsh interface ipv4 show excludedportrange protocol=tcp`), so 55432 is not reserved. Using
it also makes `tests/conftest.py`'s documented example true for the first time.

### 4.4 Proposed cluster

| Property | Value | Why |
|---|---|---|
| Data directory | `C:\Users\deepi\AppData\Local\talviora-pgtest\data` | **Outside OneDrive.** The repositories live under `C:\Users\deepi\OneDrive\Desktop\AI_APP`, and a PostgreSQL data directory inside a synced folder invites a sync client to rewrite, lock or restore files underneath a running server. Neither this path nor `C:\pgtest` exists yet |
| Port | `55432` | Free, unreserved, and already the documented example |
| `listen_addresses` | `'localhost'` | Not `'*'`, which is what both existing clusters use. A database whose contents may be dropped by any `pytest` run should not be reachable off the host |
| Windows service | **none** | Both existing clusters are `StartMode=Auto`. This one starts only when someone starts it, so it cannot be mistaken for infrastructure and consumes nothing at boot |
| Bootstrap superuser | owner's choice of name; password supplied by the owner through `initdb --pwfile` | The password is never typed into a command line, never echoed, never stored by this audit. The `--pwfile` is deleted immediately after `initdb` |

### 4.5 Startup and shutdown

```
set PGBIN=C:\Program Files\PostgreSQL\17\bin
set PGDATA=C:\Users\deepi\AppData\Local\talviora-pgtest\data

"%PGBIN%\initdb" -D "%PGDATA%" -U <bootstrap> --pwfile=<file> --encoding=UTF8 ^
    --auth-local=scram-sha-256 --auth-host=scram-sha-256
"%PGBIN%\pg_ctl" -D "%PGDATA%" -l "%PGDATA%\..\server.log" -o "-p 55432" start
"%PGBIN%\pg_ctl" -D "%PGDATA%" status
"%PGBIN%\pg_ctl" -D "%PGDATA%" -m fast stop
```

Disposal is `pg_ctl stop` followed by deleting the data directory. There is nothing else to
undo: no service, no registry entry, no firewall rule, no PATH change.

### 4.6 Authentication

`scram-sha-256` for both local and host, set at `initdb` time rather than edited in afterwards.
`pg_hba.conf` is then trimmed to loopback only:

```
host    all   all   127.0.0.1/32   scram-sha-256
host    all   all   ::1/128        scram-sha-256
```

No `trust` line anywhere, including for local connections — the convenience of a passwordless
local superuser is exactly what makes a stray `psql` dangerous.

### 4.7 Role and privileges

```sql
-- As the bootstrap superuser, in the new cluster only.
CREATE ROLE talviora_test WITH LOGIN PASSWORD '<owner-supplied>'
    CREATEDB                                   -- required: see below
    NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT;

CREATE DATABASE talviora_test OWNER talviora_test;

REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;   -- PUBLIC holds it by default
GRANT  CONNECT ON DATABASE postgres TO talviora_test;
```

`CREATEDB` is not optional: ten migration tests connect to the `postgres` maintenance database
and run `CREATE DATABASE "<name>_test_<uuid8>"`, migrate it, then `DROP DATABASE ... WITH
(FORCE)`, each dropping only the uuid-suffixed database its own fixture created. The `GRANT
CONNECT ON DATABASE postgres` exists solely for that pattern; the maintenance database should
hold nothing.

Owning `talviora_test` gives the role `CREATE` on its `public` schema, which PostgreSQL 15
removed from `PUBLIC` — so no separate schema grant is needed.

### 4.8 Isolation checks

**The structural argument comes first, because it is stronger than any grant.** PostgreSQL
roles are **cluster-local**. `talviora_test` would exist only in this new cluster, so it cannot
authenticate to the 5432 or 5433 clusters at all — not because a privilege was revoked, but
because the role is not defined there. And the application database does not exist in this
cluster, so there is nothing in it to connect to by mistake. That is why a separate cluster was
chosen over a separate database on a shared one.

To be run with the owner **after** provisioning, each one a measurement:

| # | Check | Expected |
|---|---|---|
| 1 | `SELECT version();` | PostgreSQL 17.11 |
| 2 | `SHOW port; SHOW listen_addresses;` | `55432`, `localhost` |
| 3 | `SELECT usesuper FROM pg_user WHERE usename = current_user;` | **f** |
| 4 | `SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid WHERE m.member = 'talviora_test'::regrole;` | **zero rows** — no inherited grants |
| 5 | `SELECT datname FROM pg_database ORDER BY 1;` | exactly `postgres`, `talviora_test`, `template0`, `template1` — **no application database present** |
| 6 | `SELECT has_database_privilege('talviora_test', 'postgres', 'CONNECT');` | **t** (needed by the migration tests) |
| 7 | `SELECT has_database_privilege('talviora_test', d.datname, 'CONNECT') FROM pg_database d WHERE d.datname NOT IN ('postgres','talviora_test','template0','template1');` | **zero rows**, because no other database exists here |
| 8 | `tests/db_safety.py` acceptance | `postgresql+psycopg2://talviora_test:<pw>@127.0.0.1:55432/talviora_test` passes: PostgreSQL backend, host present, database ends `_test`, role not among `{postgres, root, admin, sa, superuser, rdsadmin}`, and not equal to `settings.database_url` (`sqlite:///./app.db`). Run with `TEST_DATABASE_DESTRUCTIVE=1` and **without** `TEST_DATABASE_ALLOW_ADMIN_ROLE` |
| 9 | `drop_all_safely`'s schema inspection | no stray tables in a fresh cluster |

Checks 8 and 9 are **not** isolation evidence and are listed last deliberately: a client-side
URL guard, a `_test` suffix and an empty schema are the three things §2 of this document says
must never be mistaken for isolation. Checks 3-7 plus the cluster-locality of roles are the
isolation argument; 8 and 9 only confirm the suite will agree to run.

If empirical confirmation that the role cannot reach the existing clusters is wanted, an
authentication-failure probe against 5433 would provide it — but that is a connection attempt
against an existing cluster and needs explicit authorization. It is not required: the role's
absence from that cluster's catalog is the proof.

### 4.9 Risks to accept before authorizing

- **Disk headroom: 12 GB free of 476 GB (98% full).** `initdb` needs roughly 50 MB, but
  PostgreSQL's default `max_wal_size` is 1 GB and each throwaway migration database is a
  `template1` copy of ~8 MB. It fits, but a disk-full during a WAL write can leave a cluster
  that will not start. Worth either freeing space or setting `max_wal_size = 256MB` and
  `min_wal_size = 32MB` in the new cluster's `postgresql.conf` before the first run.
- **PATH shadowing** between the 17 and 18 `bin` directories, for `pg_dump`/`pg_restore` (4.2).
- **The password is the owner's to create and hold.** This audit will not generate, read,
  store or print it, and `--pwfile` must be deleted after `initdb`.

### 4.10 Authorization needed to proceed

Explicit approval to: run `initdb` at the path in 4.4, start the cluster on 55432, create the
role and database in 4.7, and then run checks 1-9. Until that is given, PostgreSQL integration
remains **BLOCKED** and the two PostgreSQL-gated tests remain correctly skipped.

### 4.11 Deviations from this plan, recorded (D8)

Both are deliberate, and both are in the provisioning script's header comment as well.

1. **The server is started without `-o "-p 55432"`.** The port is set in `postgresql.conf`
   instead, which is equivalent at startup and additionally survives a later restart that
   forgets the flag. Because the flag is gone, the port and `listen_addresses` are **verified
   from the running server** rather than assumed -- once by the provisioning script
   immediately after startup, and again as checks 2a/2b of the isolation suite. A
   configuration that silently failed to take effect would therefore fail the checks rather
   than pass unnoticed.
2. **`initdb --locale=C`.** Byte-order collation is deterministic, which is what a test
   cluster wants, but it is **not necessarily production's collation**. Any test result that
   depends on collation -- `ORDER BY` on text, `LIKE` ranges, index ordering for text columns,
   `upper`/`lower` on non-ASCII -- must not be generalised to production from this cluster
   without separate validation against a production-collation database. Nothing in the suite
   currently asserts text ordering, which is why C was chosen over guessing at production's
   locale; if such a test is added, this is the note that explains why it may disagree.

---

## 5. Stage E � isolation verified against the live cluster (2026-10-20)

All 16 isolation checks passed against the provisioned cluster on `127.0.0.1:55432`, process
exit code **0**, run by the owner interactively through the scratchpad `pgtest.py` (which
prompts for the test-role password without echo and holds it in one process).

**This is isolation evidence only. No integration test ran in Stage E, and none had run when
this section was written.** Section 3 item 7 remains outstanding.

### 5.1 What was measured

| # | Check | Expectation |
|---|---|---|
| 1 | `SELECT version()` | contains `PostgreSQL 17.11` |
| 2a | `current_setting('port')` | `55432` |
| 2b | `current_setting('listen_addresses')` | `localhost` |
| 3 | role attributes as `boolean::int` (super/createdb/createrole/repl/bypassrls) | `0,1,0,0,0` |
| 4 | inherited role memberships | zero rows |
| 5 | database inventory | `postgres`, `talviora_test`, `template0`, `template1` |
| 6 | owner of `talviora_test` | `talviora_test` |
| 7a | `has_database_privilege(role, 'postgres', 'CONNECT')` | true � the migration tests need it |
| 7b | databases other than `postgres`, `talviora_test`, templates | zero rows |
| 7c | databases matching `talviora`, `aiplacement`, `placement`, `app` | zero rows |
| 7d | `has_database_privilege('public', 'postgres', 'CONNECT')` | false |
| 8a | `tests/db_safety.py` accepts the URL | exit 0 |
| 8b | the guard's own report | redacted, `<redacted>@127.0.0.1:55432/talviora_test` |
| 9 | tables in schema `public` | zero rows |
| 10a | `pg_dump --version` | `pg_dump (PostgreSQL) 17.11` |
| 10b | `pg_restore --version` | `pg_restore (PostgreSQL) 17.11` |

### 5.2 Two defects the live run exposed, both in the checking apparatus

Neither was a fault in the cluster, and both are worth recording because both had passed
every mocked run beforehand:

1. **The launcher could not start.** `subprocess.call(["bash", ...])` left the executable
   unresolved. On Windows the child executable is resolved from the *parent* process's PATH,
   which the `env` handed to the child cannot influence, and Git for Windows puts only its
   `cmd` directory on PATH -- `git.exe`, no `bash.exe`. It therefore worked from a Git Bash
   shell and raised `WinError 2` from the owner's PowerShell. `pgtest.py` now resolves a
   verified `bash.exe` *before* prompting, and exits 5 with `NOTHING has been verified` when
   it cannot find one.
2. **Check 3 compared the wrong spelling of a boolean.** psql *renders* a boolean column as
   `f`/`t`, but `boolean || text` casts through `boolean::text`, which spells it
   `false`/`true`. The check expected the rendered form and the server returned the cast
   form. The role was correct throughout; the assertion was not. The query now casts to
   `int`, which has one spelling. The mock had been written to agree with the assertion
   rather than with PostgreSQL, which is exactly why no mocked run caught it; it is now
   driven by `MOCK_ROLE_ATTRS`, with a case for each unsafe attribute.

### 5.3 What section 5 does not establish

- **No PostgreSQL integration coverage.** 16 isolation checks are not tests of the
  application.
- **Isolation from the clusters on 5432 and 5433 was never probed**, by instruction. It rests
  on the structural argument in 4.8: PostgreSQL roles are cluster-local, so `talviora_test`
  does not exist in those clusters and cannot authenticate to them. Those services were not
  contacted at any point in Stages A-E.
- **Collation is `C`**, per 4.11. Any future result that depends on text ordering must not be
  generalised to production from this cluster.

---

## 6. Stage F — the selected PostgreSQL integration tests passed (2026-10-20)

**Scope: these are selected tests, not the backend suite.** 207 of roughly 4,500 backend tests
ran against PostgreSQL. Everything else in the suite still runs on SQLite, and no statement
here extends to it.

### 6.1 The run

| | |
|---|---|
| Entry point | the scratchpad `pgtest.py`, which prompts for the test-role password without echo, runs the 16 isolation checks, and refuses to run tests on any failure |
| Server | PostgreSQL 17.11, `127.0.0.1:55432`, the isolated cluster in section 4 |
| Isolation checks | 16 of 16 passed, exit 0 |
| Result | **207 passed, 0 failed, 0 errors, 7 warnings** |
| Duration | 122.81 s |
| Process exit code | **0** |

A preceding single-test run of the connection-cleanup case passed 1/1, exit 0.

Command shape (paths abbreviated; the password is never an argument):

```
pgtest.py -q -rs --tb=short --durations=10 \
  tests/unit/test_migration_seed_integrity.py tests/unit/test_alembic_url_config.py \
  tests/unit/test_pg_client_args.py tests/scripts/test_backup_restore_roundtrip.py \
  tests/unit/test_*_migration.py tests/unit/test_migration_model_parity.py
```

### 6.2 What it covers, and what it does not

Covered: the destructive-database guard, Alembic URL configuration, the native PostgreSQL
client arguments, the `pg_dump`/`pg_restore` round trip against 17.11, and the eleven migration
modules -- which until this run had only ever executed against SQLite.

Not covered: every other part of the backend. Section 3 item 7 is now **done for this
selection**; it is not a claim about the whole suite, and nothing here says the application is
production-ready.

### 6.3 Three defects this run found, in order

Each was in the test apparatus, not the application, and each had passed every mocked run:

1. **Alembic percent interpolation** -- `config.set_main_option` fed a percent-encoded URL into
   ConfigParser, which raised and quoted the URL in the message. 59 tests.
2. **A SQLAlchemy URL handed to `pg_dump`** -- libpq read `postgresql+psycopg2://...` as a
   literal database name and fell back to `localhost:5432`. 1 test.
3. **Engines whose closure depended on garbage collection** -- `inspect(create_engine(url))`
   with the default `QueuePool` left `talviora_test` client backends holding their own
   temporary databases open, so teardown could not drop them. 7 tests, intermittently.
