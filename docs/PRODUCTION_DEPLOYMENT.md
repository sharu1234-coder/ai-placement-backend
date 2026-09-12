# Production deployment

How to take this backend from a repository to a running staging and production service.

**Status when this was written:** the application code is deployment-ready. Nothing has been
deployed. No hosting platform has been chosen. See [What is not verified](#what-is-not-verified)
before trusting any part of this as "done" — that section is deliberately specific about the
difference between "works" and "has been run".

This complements, and does not replace, [`../OPERATIONS.md`](../OPERATIONS.md) (backup/restore
runbook) and [`../PRODUCTION_READINESS_FINAL.md`](../PRODUCTION_READINESS_FINAL.md) (the earlier
20-item hardening audit).

---

## 1. Architecture

```
Android app  ──HTTPS──▶  FastAPI (uvicorn)  ──▶  PostgreSQL
                              │
                              ├──▶ Job provider (Jobvetta / Adzuna / Careerjet / none)
                              ├──▶ Razorpay (subscriptions)
                              └──▶ Firebase (FCM push, Crashlytics, Analytics, App Check)

Scheduler (cron / worker)  ──▶  scripts/run_due_alerts.py  ──▶  same PostgreSQL
```

Two processes, one database:

- **Web** — the API. Stateless, scale horizontally.
- **Scheduler** — runs `scripts/run_due_alerts.py` periodically. **Not** part of the web
  process: a scheduler inside the API would fire once per worker (N workers = N duplicate
  runs), vanish on restart or scale-to-zero, and tie up a request worker for the length of a
  provider call.

Firebase is auxiliary only — no Firestore, no Firebase Auth, no Firebase Storage. PostgreSQL is
the single source of truth.

**There is no CORS middleware, deliberately.** The client is a native Android app, and native
HTTP clients do not perform CORS preflight. Add CORS only if a browser client is ever
introduced, and then with explicit origins from configuration — never `allow_origins=["*"]`
alongside credentialed requests.

## 2. Required services

| Service | Required | Notes |
|---|---|---|
| PostgreSQL 15+ | **Yes** | Verified against 17.11. Managed instance recommended. |
| Backend host (FastAPI/uvicorn) | **Yes** | Python 3.12. |
| Scheduler (cron or worker) | For alerts | Without it, alerts are configurable but never fire. |
| Object storage (S3-compatible) | **Yes in production** | Resume uploads. Startup refuses `STORAGE_PROVIDER=local` in production — see section 7.1. |
| SMTP | **Yes in production** | Startup refuses `EMAIL_PROVIDER=console` in production. |
| Razorpay | For subscriptions | Live keys required; startup rejects `rzp_test_` in production. |
| Job provider | Optional | Absent → Job Hunt degrades honestly, no fabricated results. |

## 3. PostgreSQL setup

Create the database and a dedicated least-privilege user:

```sql
CREATE DATABASE aiplacement;
CREATE USER aiplacement_app WITH PASSWORD '<generated>';
GRANT ALL PRIVILEGES ON DATABASE aiplacement TO aiplacement_app;
-- PostgreSQL 15+: the app user also needs the public schema
\c aiplacement
GRANT ALL ON SCHEMA public TO aiplacement_app;
```

Do not use the `postgres` superuser as the application user.

**Connection URL shape** (the driver pinned in `requirements.txt` is `psycopg2-binary`):

```
postgresql+psycopg2://aiplacement_app:<password>@<host>:5432/aiplacement?sslmode=require
```

`sslmode=require` for any connection crossing a network. Most managed providers require it.

### Connection pooling

Configured in `app/database/session.py`, applied only when the URL is not SQLite:

| Setting | Value | Why |
|---|---|---|
| `pool_size` | 10 | Steady-state connections per process. |
| `max_overflow` | 20 | Burst headroom; 30 max per process. |
| `pool_pre_ping` | `True` | Managed Postgres silently drops idle connections; without this the first query on a stale one fails outright. |

**Multiply by your worker count before choosing an instance size.** 4 uvicorn workers × 30 =
120 connections, which exceeds the ~100 default `max_connections` on small managed instances.
Either raise `max_connections`, lower the pool, or put PgBouncer in front. This is the most
common first-deploy outage and it is entirely predictable.

## 4. Database creation and migrations

```bash
export DATABASE_URL='postgresql+psycopg2://...'
alembic upgrade head      # create/advance schema
alembic check             # must print: No new upgrade operations detected
```

Run migrations as a **release step, before** the new code starts serving. `/health/ready`
returns 503 when the schema revision does not match what the code expects, so a forgotten
migration fails the deploy loudly instead of producing runtime errors.

Verified on PostgreSQL 17.11: full `base → head → base → head` round-trip, `alembic check`
clean at both ends. Every migration has a working downgrade.

## 5. Environment variables

All 42 settings live in [`../.env.example`](../.env.example), kept in exact sync with
`app/core/config.py` — verified programmatically: no undocumented setting, no orphan key.

**Nothing in this application is a *required* setting in the Pydantic sense.** Every one has a
default, so a missing variable never stops the process by itself — it silently selects the
development value. That is precisely why the startup guards below exist, and why "it started,
so it must be configured" is not a safe inference.

`R?` = required in production · `S?` = secret (never commit, never paste into chat) ·
`Gen?` = you must generate it yourself

| Variable | R? | S? | Gen? | Purpose / used by | Safe example |
|---|:--:|:--:|:--:|---|---|
| `PYTHON_VERSION` | **Yes** | — | — | The interpreter Render builds with. Nothing in the repo pins it — see the note below. | `3.12.8` |
| `ENVIRONMENT` | **Yes** | — | — | Enables every guard below. Anything but `development` counts as production. | `production` |
| `DATABASE_URL` | **Yes** | **Yes** | — | `app/database/session.py`. **Startup refuses a `sqlite:` URL.** Render's `postgres://` is accepted and rewritten. | `postgres://user:pass@dpg-x.oregon-postgres.render.com:5432/db` |
| `JWT_SECRET_KEY` | **Yes** | **Yes** | **Yes** | Signs every access/refresh token (`app/core/security.py`). Rejects published placeholders and <32 chars. | `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `DEBUG` | **Yes** | — | — | DEBUG-level logging. Rejected when true. | `false` |
| `ALLOWED_HOSTS` | **Yes** | — | — | `TrustedHostMiddleware`. `*` rejected. | `api.example.com` |
| `STORAGE_PROVIDER` | **Yes** | — | — | `app/storage/`. **`local` is refused** — container disk loses every resume on redeploy. | `s3` |
| `STORAGE_S3_BUCKET` | **Yes** | — | — | Resume storage. Bucket must be **private**. | `aiplacement-resumes` |
| `STORAGE_S3_REGION` | **Yes** | — | — | `auto` for Cloudflare R2. | `auto` |
| `STORAGE_S3_ACCESS_KEY_ID` | **Yes** | **Yes** | — | From your storage provider. | `<from R2/S3 console>` |
| `STORAGE_S3_SECRET_ACCESS_KEY` | **Yes** | **Yes** | — | From your storage provider. | `<from R2/S3 console>` |
| `STORAGE_S3_ENDPOINT_URL` | R2 only | — | — | Leave unset for AWS S3. | `https://<acct>.r2.cloudflarestorage.com` |
| `EMAIL_PROVIDER` | **Yes** | — | — | `console` logs reset codes in plaintext and is refused. | `smtp` |
| `SMTP_HOST` / `_PORT` / `_USERNAME` | **Yes** | — | — | Password reset delivery. | `smtp.example.com` / `587` |
| `SMTP_PASSWORD` | **Yes** | **Yes** | — | From your SMTP provider. | `<from provider>` |
| `AI_PROVIDER` | If AI on | — | — | `anthropic`, or self-hosted `ollama`. Loopback ollama is refused. | `anthropic` |
| `ANTHROPIC_API_KEY` | If AI on | **Yes** | — | Fails loudly on first use if absent. | `<from console.anthropic.com>` |
| `JOB_PROVIDER` | — | — | — | `none` degrades Job Hunt honestly. See section 14. | `none` |
| `JOBVETTA_API_KEY` | If Jobvetta | **Yes** | — | Absent → adapter raises on first use, never fabricates. | `<from Jobvetta>` |
| `RAZORPAY_KEY_ID` | If charging | — | — | A `rzp_test_` key is refused in production. | `rzp_live_...` |
| `RAZORPAY_KEY_SECRET` | If charging | **Yes** | — | Payment verification. | `<from Razorpay>` |
| `RAZORPAY_WEBHOOK_SECRET` | If charging | **Yes** | — | Required once a key is set, or paid users never activate. | `<from Razorpay webhooks>` |

**On `PYTHON_VERSION`.** There is no `render.yaml`, `Procfile`, `runtime.txt` or
`.python-version` in this repository, so nothing tells Render which interpreter to build with,
and it will use whatever its current default happens to be. That default changes over time.

This matters more here than it usually would. `requirements.txt` opens with the history: this
project once shipped ~10 packages with known CVEs for no reason other than being resolved on
Python 3.9, because every fix release required >=3.10. The current pins are chosen for 3.12,
and CI runs 3.12. A build on a different interpreter either fails on a missing wheel (loud, and
recoverable) or succeeds and runs production on a version nothing was ever tested against.

Set it as an environment variable rather than committing a version file: an env var cannot fail
the build by naming a patch release the platform does not stock, and it is trivially visible and
changeable in the dashboard. Use the 3.12 patch release Render currently offers.

Everything not listed has a working default (`APP_NAME`, `UPLOAD_DIR`, token lifetimes, rate
limits, Adzuna/Careerjet, Firebase App Check). See `.env.example`.

### The four ways a development default could have reached production

Each is now refused at startup rather than discovered later. All four verified by booting the
app with the wrong value and confirming it exits (not by reading the code):

| Default | What it would have done | Now |
|---|---|---|
| `DATABASE_URL=sqlite:///./app.db` | Started cleanly on a container-local file; `/health/ready` reports `reachable`/`current`; every account lost on the next deploy, with the managed PostgreSQL never connected to | **Refused** |
| `STORAGE_PROVIDER=local` | Accepted uploads onto container disk; `Resume` rows survive pointing at files the next deploy destroys | **Refused** |
| `JWT_SECRET_KEY=change-me-in-production` | A signing key published in this repository — anyone who could read it could mint a token for any user | **Refused** |
| No `alembic_version` table | Readiness answered 200, so the platform would route traffic to a service with no tables | **503** |

## 6. Secret setup

Secrets go in the platform's secret store (Render environment groups, Railway variables, Fly
secrets, AWS Secrets Manager) — **never** in the repository, an image layer, or a log line.

Secret-shaped: `DATABASE_URL`, `JWT_SECRET_KEY`, `RAZORPAY_KEY_SECRET`,
`RAZORPAY_WEBHOOK_SECRET`, `JOBVETTA_API_KEY`, `SMTP_PASSWORD`, `STORAGE_S3_SECRET_ACCESS_KEY`,
`ANTHROPIC_API_KEY`, Firebase service-account JSON.

`RAZORPAY_KEY_ID` is the only one the Android client legitimately receives. The secret never
leaves the server.

## 7. Backend deployment

Entrypoint:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 4         --proxy-headers --forwarded-allow-ips="<your platform's proxy IP or CIDR>"
```

Never `--reload` in production. Choose `--workers` against your database connection budget
(section 3), not against CPU count alone.

### The two proxy flags are not optional, and `"*"` is not a safe shortcut

Every managed platform terminates TLS at its edge and forwards plain HTTP to your container.
That single fact drives two behaviours that both fail **silently**, so neither shows up in a
smoke test:

- **HSTS disappears.** `HSTSMiddleware` adds `Strict-Transport-Security` only when
  `scope["scheme"] == "https"` (correct — the header is meaningless over plain HTTP). Behind
  an edge proxy the scheme is `http` unless uvicorn is told to trust `X-Forwarded-Proto`. The
  site then serves every response without HSTS while `/health` returns a cheerful 200.
  Measured on uvicorn 0.52.4: with a forwarded proto from a trusted peer the header is
  present (`max-age=63072000; includeSubDomains`); without one it is absent.

- **Rate limiting collapses into one bucket.** `get_remote_address` reads the socket peer, so
  with no trusted forwarding *every* request appears to come from the proxy. Login then caps
  at 5/minute across the entire user base — one attacker locks out every student, and the
  per-attacker brute-force protection protects nobody.

`proxy_headers` already defaults to `True` in uvicorn 0.52.4; `forwarded_allow_ips` defaults
to `None`, which uvicorn resolves to `127.0.0.1`. So the setting that actually decides this is
`--forwarded-allow-ips`.

**Do not set `--forwarded-allow-ips="*"`.** It makes uvicorn rewrite the client address from
an `X-Forwarded-For` header sent by *anyone*, which hands the rate-limit key to the attacker:
rotating one header value per request defeats every limit in the app, including login and
password reset. `app/core/rate_limit.py` carries this warning at the code itself. Set it to
the platform's actual proxy address or CIDR. If a platform cannot tell you that range, treat
it as a question to answer before launch, not a detail to leave at the default.

### 7.1 Why local file storage is refused in production

`STORAGE_PROVIDER` defaults to `local`, which writes uploads to the container filesystem. On
every platform compared in section 23 that filesystem is destroyed on each deploy and restart, while the
`Resume` row committed alongside it survives, still pointing at the vanished key. Nothing
fails at the time: `save()` creates the directory, writes the file, and the API returns 200.
It surfaces later, to a student, as a resume that can no longer be opened or re-analysed —
with the original upload gone for good, and every ATS check, JD match and resume-derived
evidence signal resting on it.

Startup therefore refuses it, in the same fail-closed style as the JWT and email checks.
`STORAGE_PROVIDER=s3` works against AWS S3 or any S3-compatible service (Cloudflare R2, MinIO,
Backblaze B2, DigitalOcean Spaces); set `STORAGE_S3_ENDPOINT_URL` for a non-AWS one.

Startup calls `validate_for_production()`, which **refuses to start** on an insecure
configuration rather than running in a degraded state. A failed boot with a clear error is the
intended behaviour — read the message, fix the variable, redeploy.

## 8. Worker / scheduler deployment

```bash
python scripts/run_due_alerts.py
```

Run **hourly**. The script selects alerts whose `is_due` returns true, which already accounts
for quiet hours, weekday selection and the minimum gap between runs.

Safe to over-run: `run_alert` stamps `last_run_at` before doing any work, so an overlapping or
double-firing schedule finds nothing due. Delivery rows are additionally protected by a unique
constraint whose `IntegrityError` is treated as "already told them".

- **Exit 0** — batch completed (including individual alerts that failed and were recorded).
- **Exit 1** — the batch could not run at all (e.g. database unreachable). **Alert on this.**

Options: `--dry-run` (report, change nothing), `--limit N` (cap the batch), `--verbose`.

Platform mapping: Render → Cron Job; Railway → cron service; Fly.io → scheduled machine;
Kubernetes → CronJob; plain VM → crontab (`0 * * * *`).

## 9. Health checks

| Endpoint | Meaning | Use for |
|---|---|---|
| `GET /health` | Process is up. No dependency checks. | Liveness probe |
| `GET /health/ready` | Database reachable **and** schema at the expected migration revision. | Readiness probe, load-balancer gate |

`/health/ready` returns 503 with `{"status":"unavailable","database":"unreachable"}` or
`{"schema":"out_of_date", ...}`. It logs only the exception *type*, never the message, because
driver connection errors can echo the DSN and its password.

Point the load balancer at `/health/ready` so an instance with a stale schema never receives
traffic.

## 10. Logs

Configured in `app/core/logging.py`; `RequestObservabilityMiddleware` adds request logging.
`DEBUG=false` in production (enforced).

Watch for: startup validation failures, `Readiness check failed`, alert batch exit 1, provider
errors, repeated 401/403.

Never logged, by design: passwords, API keys, JWTs, authorization headers, payment secrets, or
database URLs with credentials.

## 11. Backups

Covered in detail by [`../OPERATIONS.md`](../OPERATIONS.md). Minimum for production:

- **Automated daily** managed backups (enable in the provider console).
- **Retention** 7–30 days.
- **PITR** if the provider offers it.
- **Restore rehearsal** against a scratch database before you need it — an untested backup is a
  hypothesis.

Manual: `python scripts/backup_db.py` (uses `pg_dump -Fc`; needs `pg_dump` on `PATH` or
`PG_DUMP_PATH`).

## 12. Restore

```bash
python scripts/restore_db.py backups/<file>.dump --yes
```

Destructive by design. Refuses to run when `ENVIRONMENT=production` unless
`--allow-production` is also passed. For a real production restore, prefer the provider's own
snapshot restore into a **new** instance, verify it, then cut over.

## 13. Rollback

1. **Code only** — redeploy the previous image/commit. Safe when no migration ran.
2. **Code + migration** — `alembic downgrade <previous_revision>`, then deploy the previous
   code. Every migration here has a tested downgrade, but a downgrade that drops a column
   **destroys the data in it**. Take a backup first, always.
3. **Catastrophic** — restore from backup into a new instance and repoint `DATABASE_URL`.

Deploy order that makes rollback cheap: migrate → deploy → verify `/health/ready`.

## 14. Jobvetta

Set `JOB_PROVIDER=jobvetta` and `JOBVETTA_API_KEY=<key>` in the secret store.

Free tier: 50 requests per key per UTC day, max 10 results per search, India coverage. The
adapter enforces a call budget.

Absent or failing provider degrades honestly — a provider outage is **never** rendered as
"0 jobs found". 401/404/429/5xx/timeout/malformed responses each have distinct handling.

**Live Jobvetta has never been called.** See [What is not verified](#what-is-not-verified).

## 15. CORS

Not configured, deliberately — see section 1.

## 16. HTTPS and domain

Terminate TLS at the platform's load balancer. `HSTSMiddleware` adds HSTS; it is only
meaningful over HTTPS. Set `ALLOWED_HOSTS` to the real hostnames — `TrustedHostMiddleware`
rejects Host headers outside it, and `*` is refused at startup.

## 17. Android configuration

See [Android handoff](#android-handoff) below.

## 18. Smoke tests

Run against **staging** first, in this order. Steps 3–19 use a throwaway account created for
this purpose — never a real user's.

Response bodies below are the ones actually returned by the app (checked against a running
server, not read off the schema).

| # | Action | Expected | PASS | FAIL |
|---|---|---|---|---|
| 1 | `GET /health` | `200 {"status":"ok","app":"AI Placement OS Backend","environment":"production"}` | 200 **and** `environment` says `production` | Any non-200; or `environment` still `development` (the guard was skipped — stop and fix) |
| 2 | `GET /health/ready` | `200 {"status":"ok","database":"reachable","schema":"current"}` | Both `reachable` **and** `current` | `503`, `database:"unreachable"`, or `schema:"out_of_date"` (migrations not run — do not send traffic) |
| 3 | Register a test account | `201`, tokens returned | Account created, token usable | 4xx; or a stack trace in the body |
| 4 | Log in as that account | `200`, access + refresh token | Token decodes and is accepted in step 5 | `401` with correct credentials → check `JWT_SECRET_KEY` |
| 5 | `GET /users/me` **without** a token | `401 {"detail":"Not authenticated"}` | Exactly 401 | 200 — authentication is broken; **stop the rollout** |
| 6 | `GET /users/me` with the token | `200`, that account's profile | Returns the caller's own data | Any other account's data — **stop immediately, this is an isolation failure** |
| 7 | Create/update the profile | `200`/`201` persisted | Re-reading returns what was written | Silent loss of a field |
| 8 | `GET /roadmap` | `200`, empty or generated | Honest empty state | 500 |
| 9 | `GET /career-strategy` | `200`, honest empty state for a new account | Says there is not enough evidence yet | Any fabricated score or invented readiness number |
| 10 | Job Hunt search | `200` with results, **or** a typed provider error | With no key: a clear "not configured"; never invented jobs | Fabricated listings, or a 500 |
| 11 | Save a job | `201`, appears in the tracker | Tracker lists it | Saved but missing from the tracker |
| 12 | `GET /job-hunt/saved` | `200`, the saved job with its match | Score present and versioned | Stale `score_version` |
| 13 | Premium endpoint as a **free** user | `403` | Exactly 403 | 200 — server-side entitlement is not enforced; **stop the rollout** |
| 14 | Premium endpoint as a **premium** user | `200` | Full response | 403 for a genuinely subscribed account |
| 15 | Create an alert | `201`, `is_enabled` true | Row created | 500 |
| 16 | Update that alert | `200`, change persisted | Re-read shows the change | Change silently dropped |
| 17 | `python scripts/run_due_alerts.py --dry-run` | Exit `0`, logs what *would* run | Exit 0 and nothing written | Exit 1 → the batch cannot reach the database |
| 18 | Run it for real, then immediately again | Second run finds nothing due | No duplicate delivery row | A second delivery for the same job — deduplication is broken |
| 19 | Delete the test account | `204`, rows gone | Related rows removed, no FK error | `500` → a foreign key has no cascade; **fix before launch** |
| 20 | Re-read any error body from 1–19 | No internals | No `Traceback`, no `sqlalchemy`/`psycopg2`, no DSN, no key | Any of those appearing → **stop, this leaks configuration** |
| 21 | Android release build against production | App reaches the API over HTTPS | Login and one authenticated screen work | Cleartext blocked (correct) but URL is `http://` → fix `PROD_API_BASE_URL` |
| 22 | PostgreSQL persistence | Restart the service, re-read step 7's data | Data survives the restart | Data gone → you are on ephemeral storage, not the database |

Never run step 19 against a real user account.

## 19. Monitoring

Minimum: uptime check on `/health/ready`; alert on scheduler exit 1; alert on 5xx rate;
database connection-count and disk-usage alarms (connection exhaustion is the predicted first
failure — section 3).

## 20. Incident response

| Symptom | First check |
|---|---|
| App will not start | Startup validation error in logs — a refused boot is intentional |
| `/health/ready` 503, `out_of_date` | `alembic upgrade head` was not run |
| `/health/ready` 503, `unreachable` | Database down, credentials rotated, or connections exhausted |
| 500s under load | Connection pool vs `max_connections` (section 3) |
| Job Hunt empty for everyone | Provider outage vs genuinely no jobs — the API distinguishes these; check provider status |
| Alerts silent | Did the scheduler run? Check its logs and exit code |
| Payments not activating | `RAZORPAY_WEBHOOK_SECRET` unset fails webhooks closed, by design |

## 21. Common failures

- Copying `.env.example` and not generating `JWT_SECRET_KEY` → refused at startup (the file
  ships it empty on purpose).
- `ENVIRONMENT` typo (`Production`, `prod`) → guards still apply; they fail closed.
- Pool × workers > `max_connections` → intermittent 500s under load.
- Forgetting the release-phase migration → `/health/ready` 503.
- Running the scheduler inside the web process → duplicate alert deliveries.

## 22. Security checklist

- [ ] `JWT_SECRET_KEY` freshly generated, ≥32 chars, never committed
- [ ] `DEBUG=false`, `ENVIRONMENT=production`
- [ ] `ALLOWED_HOSTS` set to real hostnames, not `*`
- [ ] Database user is not a superuser; `sslmode=require`
- [ ] All secrets in the platform secret store
- [ ] HTTPS enforced; HSTS active
- [ ] Razorpay **live** keys; webhook secret set
- [ ] Rate limiting active (default on)
- [ ] Backups enabled and a restore rehearsed
- [ ] No `.env`, dump, or key in the repository

## 23. Hosting platform

No platform has been chosen. This section exists so the choice is made against this
application's actual requirements rather than a general impression.

### What this app actually needs

| Requirement | Where it comes from | Rules out |
|---|---|---|
| Long-lived HTTP process | uvicorn, in-memory rate-limit counters | Pure request-scoped serverless |
| Managed PostgreSQL 15+ | Single source of truth | — |
| A **scheduled job** | `scripts/run_due_alerts.py`; without it alerts never fire | Anything with no cron primitive |
| S3-compatible object storage | Resume uploads; local disk is refused (7.1) | — |
| A **knowable proxy IP/CIDR** | `--forwarded-allow-ips` (section 7) | Platforms that will not tell you |
| Env-var secret store | ~12 production secrets | — |
| Automatic HTTPS | HSTS, Android cleartext ban | — |

The two that narrow the field are the scheduled job and the proxy range — most comparisons
ignore both, and both are load-bearing here.

### Comparison

| | Render | Railway | Fly.io | DO App Platform | Cloud Run + Cloud SQL |
|---|---|---|---|---|---|
| Native Python build | Yes | Yes | Dockerfile | Yes | Dockerfile |
| Managed PostgreSQL | Yes | Yes | Yes | Yes | Cloud SQL |
| **Scheduled job** | **First-class Cron Job** | Cron service | Scheduled machine | Scheduled job | Cloud Scheduler (separate) |
| Automatic HTTPS | Yes | Yes | Yes | Yes | Yes |
| Secret store | Env groups | Variables | Secrets | App-level env | Secret Manager |
| Scale to zero | Free tier only | No | Yes | No | Yes — **breaks in-memory rate limits** |
| Setup burden | Lowest | Low | Medium | Low | Highest |
| Rough monthly cost | ~$7 web + ~$7 PG | usage-based | ~$5–10 | ~$5 web + ~$7 PG | usage + ~$10 Cloud SQL |

### Recommendation

**Render**, as the primary option.

- Its **Cron Job** is a first-class object that runs a command on a schedule in the same
  environment as the service. That is exactly the shape `run_due_alerts.py` needs, and it is
  the requirement most likely to be discovered late somewhere else.
- **Native Python build** means no Dockerfile. That matters concretely here: Docker is not
  installed on this machine, so any Dockerfile shipped today would be configuration that has
  never been built — see [What is not verified](#what-is-not-verified). Choosing a platform
  that does not need one removes an unverifiable artifact instead of adding one.
- Managed PostgreSQL with automated daily backups, and point-in-time recovery on paid plans —
  which section 11 otherwise has to arrange by hand.
- Env groups map cleanly onto the section 5 variable matrix.

**Cloud Run is the one to avoid here without further work**, despite being otherwise capable:
scale-to-zero plus multiple instances breaks the in-memory rate limiter
(`app/core/rate_limit.py`), which is per-process by design. Fixing that needs a shared store —
real work, not a setting.

**The open question to resolve with the platform, before launch:** its proxy IP or CIDR, for
`--forwarded-allow-ips`. Section 7 explains why `"*"` is not an acceptable answer.

**Cost expectation:** roughly **$14–20/month** for a starter web service plus managed
PostgreSQL, before object storage (Cloudflare R2's free tier comfortably covers resume-sized
uploads at this stage).

This is a recommendation, not a decision. Nothing has been created, and no account, project or
billable resource exists.

## 24. Deployment runbook

Who does what, and what a mistake at each step costs. **DANGER** rates the consequence of
getting it wrong, not the difficulty of doing it.

| # | Step | Who | Danger | Notes |
|---|---|---|---|---|
| 1 | Choose the hosting platform | **You** | LOW | Section 23. Cheap to change now, expensive later. |
| 2 | Create the account / project | **You** | LOW | Nothing exists yet. |
| 3 | Create managed PostgreSQL | **You** | MEDIUM | Billable. Keep it in the same region as the web service. |
| 4 | Create the S3-compatible bucket | **You** | LOW | Private, not public-read. |
| 5 | Generate `JWT_SECRET_KEY` | **You** | **HIGH** | `python -c "import secrets; print(secrets.token_urlsafe(64))"`. Never paste it into chat, a commit or a screenshot. Rotating it later logs every user out. |
| 6 | Collect the remaining secrets | **You** | **HIGH** | SMTP, Razorpay live + webhook, Jobvetta, S3 keys — straight into the platform's secret store. |
| 7 | Set every variable from section 5 | **You** | MEDIUM | A miss fails the boot loudly, which is the intended behaviour. |
| 8 | Obtain the platform's proxy IP/CIDR | **You** | MEDIUM | Section 7. Ask support if it is not documented. |
| 9 | Configure build and start commands | Claude | LOW | Section 7, once the platform is known. |
| 10 | Deploy the web service | **You** | MEDIUM | First boot refuses to start if any section 5 variable is wrong. |
| 11 | Run `alembic upgrade head` | **Both** | **HIGH** | Section 4. Back up first (step 15) even though the first run is against an empty database. |
| 12 | Verify `/health` and `/health/ready` | Claude | LOW | `ready` must report `schema: current`. |
| 13 | Create the scheduled job | **You** | LOW | `python scripts/run_due_alerts.py`, hourly. Try `--dry-run` first. |
| 14 | Point Android at the production URL | Claude | LOW | Section 17. |
| 15 | Verify the backup **and a restore** | **You** | MEDIUM | A backup nobody has restored is not a backup. See `OPERATIONS.md`. |
| 16 | Run the smoke checklist | **Both** | LOW | Section 18. |
| 17 | Build and ship the release APK | **You** | MEDIUM | Signing keys never enter the repository. |
| 18 | Turn on monitoring and alerting | **You** | LOW | Section 19. |

Steps 5, 6 and 11 are the ones to slow down for. Everything else is recoverable in minutes.

## 25. Rollback playbook

| Failure | First move | Danger | Notes |
|---|---|---|---|
| Bad deploy, schema unchanged | Redeploy the previous commit | LOW | Stateless web tier; nothing to undo. |
| Bad deploy, schema changed | **Do not auto-downgrade.** Restore-test first | **HIGH** | See the note below. |
| Migration failed mid-run | Check `alembic current`; restore from backup if partial | **HIGH** | Every downgrade here was verified base→head→base→head, but a *verified* downgrade still drops columns and the data in them. |
| Provider outage (Jobvetta) | Nothing — it degrades honestly | LOW | Set `JOB_PROVIDER=none` if it is noisy. No fabricated jobs either way. |
| Database unreachable | `/health/ready` fails; the load balancer pulls the instance | MEDIUM | Check the connection budget against `--workers` before blaming the platform. |
| Scheduler failing | Exit 1 is the alert. Alerts pause; nothing corrupts | LOW | Safe to re-run: `run_alert` stamps `last_run_at` before doing any work. |
| Auth broken after a config change | Re-check `JWT_SECRET_KEY` **before** rotating it | **HIGH** | Rotating invalidates every live session. A wrong value is far likelier than a compromised one. |
| Bad Android release | Halt the staged rollout in Play Console | MEDIUM | The backend is versioned independently; keep older clients working. |

**On downgrading a migration:** a downgrade that runs cleanly is not the same as a downgrade
that is safe. Dropping a column succeeds and destroys the data in it. Restore a backup into a
scratch database and test the downgrade there before running it against production data.

## 26. Render deployment design

Render is the chosen target. This section is the concrete design: three Render objects plus one
external bucket, and the exact settings each needs.

```
┌─ Render ────────────────────────────────────────────────┐
│  Web Service  (Python 3.12, native build — no Docker)   │
│      uvicorn app.main:app                               │
│      health check: /health/ready                        │
│            │                                            │
│            ├──────────────┐                             │
│  Cron Job  │              │                             │
│      python scripts/run_due_alerts.py  (hourly)         │
│            │              │                             │
│  PostgreSQL 17 ◀──────────┘                             │
└────────────┼────────────────────────────────────────────┘
             │
      S3-compatible bucket (Cloudflare R2 or AWS S3) — resumes
```

Resume files deliberately live outside Render. See 26.4.

### 26.1 Web Service

| Setting | Value |
|---|---|
| Environment | Python 3 |
| Build command | `pip install -r requirements.txt` |
| Start command | see below |
| Health check path | `/health/ready` |
| Python version | `3.12` (set `PYTHON_VERSION=3.12.*` or commit a `.python-version`) |
| Auto-deploy | Your choice; off is safer while migrations are manual |

Start command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 2 \
        --proxy-headers --forwarded-allow-ips="*"
```

Three parts of that are not arbitrary:

- **`$PORT`** — Render assigns it and requires the process to bind it. Hard-coding 8000 fails
  the health check with no useful error.

- **`--workers 2`, not 4.** Two limits meet here. Each worker takes `pool_size=10 +
  max_overflow=20` = 30 connections (section 3), so 2 workers is 60 against a Starter
  PostgreSQL's cap — 4 would be 120 and exhaust it under load. Memory is the other: each
  worker loads the whole app. Raise this only after checking both numbers, not by CPU count.

- **`--forwarded-allow-ips="*"` is safe *here specifically*, and nowhere else.** Section 7
  warns that `"*"` hands the rate-limit key to the attacker, because uvicorn will then trust
  `X-Forwarded-For` from anyone. That warning holds whenever the container is reachable
  directly. On Render it is not: the service is only addressable through Render's own proxy,
  which overwrites the forwarded headers, so "anyone" is exactly one host. Render does not
  publish a stable proxy CIDR to pin instead. **If you ever move off Render, or expose the
  container on a public port, this must change back to a pinned range.**

### 26.2 PostgreSQL

Create a Render PostgreSQL instance in the **same region** as the web service and use the
**Internal Database URL**.

**The scheme matters, and Render gives you the wrong one.** Render presents
`postgres://…`, which SQLAlchemy has refused since 1.4 — it answers `NoSuchModuleError: Can't
load plugin: sqlalchemy.dialects:postgres`, an error naming a plugin and never mentioning the
URL. `app/core/config.py` now rewrites `postgres://` to `postgresql+psycopg2://` on load, so
pasting Render's value verbatim works. Nothing else about the URL is altered, and the SQLite
guard still sees the real scheme.

Internal vs external: the Internal URL keeps traffic inside Render's network and is not
billed as egress. Use the External URL only from your own machine, for migrations or a dump.

Migrations are a **manual release step**, not part of startup — see 26.5.

### 26.3 Cron Job

| Setting | Value |
|---|---|
| Command | `python scripts/run_due_alerts.py` |
| Schedule | `0 * * * *` (hourly, UTC) |
| Environment | The same variables as the web service — **at minimum** `DATABASE_URL`, plus `JOB_PROVIDER` and its credentials |

A Render Cron Job is a separate object with its **own** environment. It does not inherit the
web service's variables; use a shared Environment Group, or the job will start, fail to reach
the database, and exit 1 every hour.

Working directory is the repository root, which is what `scripts/run_due_alerts.py` expects —
it inserts the repo root on `sys.path` itself, so `python scripts/run_due_alerts.py` works with
no `PYTHONPATH`.

- **Exit 0** — the batch completed, including alerts that individually failed and were
  recorded against their row.
- **Exit 1** — the batch could not run at all. **This is the one to alert on.**

**`hour_local` is a UTC hour, despite the name.** No timezone is stored anywhere — there is no
timezone column on the profile or the alert — and `is_due` compares `alert.hour_local` against
`datetime.now(timezone.utc).hour`. An alert configured for `9` therefore fires at 09:00 UTC,
which is 14:30 IST.

Nothing is broken by this: alerts fire reliably, once per period, with duplicate suppression
intact. But it is worth knowing before you tell students what "9" means, and it is a product
decision (store a per-profile timezone? assume one?) rather than a deployment fix, so it has
deliberately been left alone here. The Cron schedule itself is unaffected — run it hourly and
`is_due` decides.

Double-firing is safe by construction: `run_alert` stamps `last_run_at` before doing any work,
so a second invocation finds nothing due, and delivery rows are additionally protected by a
unique constraint whose `IntegrityError` is treated as "already told them". Overlapping runs
and a retried schedule are both fine.

### 26.4 Object storage — and why not a Render Disk

`STORAGE_PROVIDER=s3` is already implemented (`app/storage/s3_provider.py`, via boto3) and
speaks the plain S3 API, so **no new storage implementation is needed**. It works against AWS
S3 or any S3-compatible service; set `STORAGE_S3_ENDPOINT_URL` for a non-AWS one.

**Recommended: Cloudflare R2.** Its free tier covers resume-sized uploads comfortably at this
stage, and it charges no egress. Endpoint form:
`https://<account-id>.r2.cloudflarestorage.com`, with region `auto`.

A Render **Disk** is the tempting alternative and is the wrong choice here: a disk can only
attach to one instance, which forecloses running more than one and disables zero-downtime
deploys. Object storage keeps the web tier stateless, which is the property the whole design
rests on.

The bucket must be **private**. Files are served through the API, which checks ownership
first; a public bucket would make every resume readable by URL to anyone who guessed it.

### 26.5 Migrations

Render's native Python environment gives you a shell on the service, which is where migrations
belong:

```bash
alembic upgrade head
alembic check     # must print: No new upgrade operations detected
```

Deliberately **not** in the build or start command. Four workers starting at once would run
four concurrent `alembic upgrade head` against one database, and the losers fail in ways that
depend on timing. The app does no schema work at startup at all — there is no `create_all`
anywhere in `app/` — so the only thing that ever builds the schema is this command, run by a
person, once.

`/health/ready` is what makes that safe to get wrong: it compares the database's Alembic
revision against the one this deployment's code expects and answers **503** when they differ,
so an instance with a stale — or entirely absent — schema never receives traffic.

### 26.6 What Render does not give you

- **No automatic backups on the free PostgreSQL tier.** Paid tiers include daily backups and
  point-in-time recovery. On the free tier, backups are yours to run — see section 11 and
  `OPERATIONS.md`. Do not assume a managed backup exists because the database is managed.
- **No object-storage backup.** Enable versioning on the bucket (26.4); that is what protects
  resumes against an accidental delete, and it is a bucket setting, not a Render one.
- **Scale-to-zero on free web services.** The first request after an idle period pays the cold
  start, and in-memory rate-limit counters reset with the process.

## 27. Deployment checklist — step by step

Every step is marked **[YOU]** or **[CLAUDE]**. Nothing here has been done yet.

Anything marked [YOU] needs an account, a credential, or a billing decision, which is why it
cannot be done for you. Secrets go straight from where you generate them into Render's
environment settings — **never into a chat message, a commit, a screenshot, or a file in the
repository.**

---

### 1. Accounts and services **[YOU]**

| Service | What for | Cost |
|---|---|---|
| Render | Web service, PostgreSQL, Cron Job | ~$14/mo (Starter web + Starter PG) |
| Cloudflare R2 (or AWS S3) | Resume storage | R2 free tier covers this stage |
| An SMTP sender | Password-reset email | Free tiers exist |
| Razorpay | Subscriptions — only if charging at launch | — |
| Jobvetta | Job Hunt results — optional, see section 14 | — |

### 2. Create Render PostgreSQL **[YOU]**

Render → New → PostgreSQL. Note the **region**; the web service must match it. Copy the
**Internal Database URL**. It begins `postgres://` — paste it exactly as given; the app rewrites
the scheme on load (section 26.2).

### 3. Create the object storage bucket **[YOU]**

Create a **private** bucket (not public-read — files are served through the API, which checks
ownership first). Create an access key scoped to that bucket and keep the four values for
step 5: bucket name, region, access key id, secret access key. For R2 also note the endpoint,
`https://<account-id>.r2.cloudflarestorage.com`, and use `auto` as the region.

### 4. Generate the production secrets **[YOU]**

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

That is `JWT_SECRET_KEY`. Generate it yourself and paste it straight into Render.

- It must not be a value from this repository. Startup rejects the `.env.example` placeholder
  and anything under 32 characters (verified).
- Changing it later logs every user out. Treat it as permanent.
- I have not generated it and must not: a secret that passes through a chat log is not a secret.

### 5. Configure Render environment variables **[YOU]** · list prepared **[CLAUDE]**

Full matrix in section 5. The ones with no safe default:

| Variable | Value |
|---|---|
| `PYTHON_VERSION` | A 3.12 patch release — see section 5 |
| `ENVIRONMENT` | `production` |
| `DATABASE_URL` | Render's Internal Database URL (step 2) |
| `JWT_SECRET_KEY` | Step 4 |
| `STORAGE_PROVIDER` | `s3` |
| `STORAGE_S3_BUCKET` / `_REGION` / `_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY` | Step 3 |
| `STORAGE_S3_ENDPOINT_URL` | R2/MinIO only; leave unset for AWS |
| `EMAIL_PROVIDER` | `smtp` |
| `SMTP_HOST` / `_PORT` / `_USERNAME` / `_PASSWORD` | Your sender |
| `ALLOWED_HOSTS` | Your API hostname, comma-separated |
| `DEBUG` | `false` |
| `AI_PROVIDER` + `ANTHROPIC_API_KEY` | If AI features are on |
| `JOB_PROVIDER` | `none` until you have a Jobvetta key |
| `RAZORPAY_KEY_ID` / `_KEY_SECRET` / `_WEBHOOK_SECRET` | Only if charging at launch |

Put these in a Render **Environment Group** so the Cron Job (step 12) shares them.

Getting one wrong does not produce a broken service — startup refuses, with a message naming
the variable. That is deliberate.

### 6. Deploy the backend **[YOU]** · settings prepared **[CLAUDE]**

Render → New → Web Service → connect the repo → branch `deployment-readiness` (or whatever
you merge it into). Build and start commands are in section 26.1. Health check path
`/health/ready`.

The first deploy is **expected to fail its health check** — the database has no schema yet.
That is step 7.

### 7. Run migrations **[YOU]** (one command) · verification **[CLAUDE]**

Render → your service → Shell:

```bash
alembic upgrade head
alembic check      # must print: No new upgrade operations detected
```

Not automated on purpose: multiple workers starting at once would run concurrent migrations
against one database. Verified locally as base→head→base→head with `check` clean at both ends.

### 8. Verify health **[CLAUDE]**, once you give me the URL

```
GET /health        -> 200, "environment":"production"
GET /health/ready  -> 200, "database":"reachable","schema":"current"
```

A 503 with `"schema":"out_of_date"` means step 7 did not run. Verified locally that this is
exactly what an unmigrated database produces.

### 9–11. Verify authentication, database and resume upload **[CLAUDE]**, on your staging URL

Section 18's table, steps 3–7 and 19–22. Step 11 is the one that matters most: upload a
resume, **redeploy the service**, then re-open it. If it still opens, object storage is
genuinely wired up. If it 404s, `STORAGE_PROVIDER` is not `s3` — though startup should have
refused to boot in that case.

### 12. Configure the Cron Job **[YOU]**

Render → New → Cron Job → same repo → command `python scripts/run_due_alerts.py` → schedule
`0 * * * *` → attach the **same Environment Group** from step 5. A Cron Job does not inherit
the web service's variables; without them it exits 1 every hour.

Verify once with `--dry-run` in its shell before leaving it on.

### 13. Point Android at production **[CLAUDE]** · value from you

Add to `local.properties` (gitignored, never committed):

```properties
PROD_API_BASE_URL=https://<your-render-service>.onrender.com/
```

Until then the prod flavour builds against `https://api.aiplacementos.example.invalid/`, a
reserved TLD that can never resolve — a misconfigured build fails obviously instead of quietly
talking to the wrong server.

### 14. Build the signed release **[YOU]**

**No keystore exists in this repository, and I have not created one** — a signing key that
passes through a chat log is compromised, and losing it means never updating the app again.

```bash
keytool -genkeypair -v -keystore release.keystore -alias upload \
        -keyalg RSA -keysize 2048 -validity 10000
```

That is the command from `keystore.properties.example` verbatim -- follow that file rather
than this one if they ever disagree, since the build reads it. `storeFile` there resolves
relative to the project root.

Then create `keystore.properties` (gitignored — `keystore.properties.example` is the template)
with `storeFile`, `storePassword`, `keyAlias`, `keyPassword`, and run:

```bash
./gradlew assembleProdRelease     # or bundleProdRelease for Play
```

**Back the keystore up somewhere you will still have in five years.** Google cannot recover it.

### 15–16. Install and smoke test **[YOU]** installs · **[CLAUDE]** reviews results

Install the signed APK on a real device and work through section 18. This is the only place
real-device behaviour is checked at all — none of it has been verified here (no device).

### 17. Verify the backup **[YOU]**

Take a backup, restore it into a scratch database, and confirm the table count and Alembic
revision match. Commands in section 11 and `OPERATIONS.md`; the round-trip is verified locally.
**Do this before you have users, not after.**

### 18–19. Rollback and monitoring **[CLAUDE]** documents · **[YOU]** configures

Rollback is section 25. Monitoring is section 19: an uptime check on `/health/ready`, and an
alert on the Cron Job exiting 1.

### 20. If the deployment fails

| Symptom | Cause | Fix |
|---|---|---|
| Service will not start, log names a variable | A guard refused | Set that variable (step 5) |
| `NoSuchModuleError: sqlalchemy.dialects:postgres` | Running code older than this branch | Deploy this branch |
| Health check fails, `schema: out_of_date` | Migrations not run | Step 7 |
| Starts, but resumes vanish after a deploy | `STORAGE_PROVIDER` not `s3` | Step 3/5 — startup should have refused |
| Cron exits 1 hourly | No env group attached | Step 12 |
| Intermittent "too many connections" | Workers × 30 over the plan cap | Lower `--workers` (section 26.1) |

## Android handoff

Backend deployment gives Android exactly these:

| Item | Value |
|---|---|
| Production API base URL | `https://<domain>` — **you supply**; do not change the app until confirmed |
| Staging API base URL | `https://<staging-domain>` |
| Scheme | HTTPS only — cleartext must stay disabled |
| Auth | `Authorization: Bearer <JWT>` |
| Timeouts | Provider-backed Job Hunt calls are the slowest path; keep the existing generous client timeout |
| Razorpay key | `RAZORPAY_KEY_ID` only — never the secret |
| Firebase | Existing `google-services.json`; App Check must accept the production backend |

No Android feature behaviour changes as part of backend deployment.

Two things to set before the first Play upload, neither of which the backend controls:

- **`PROD_API_BASE_URL`** in `local.properties` (gitignored). Until it is set, the prod flavour
  builds against `https://api.aiplacementos.example.invalid/` — a reserved TLD that can never
  resolve, so a misconfigured release fails loudly instead of quietly reaching a dev server.
  Confirmed present in the built APK, so this is the value that actually ships.
- **`versionCode`** is `1` and **must increase on every upload** — Play rejects a duplicate, and
  it is not derived from `versionName` (`0.1.0`). `app/build.gradle.kts` lines 74-75.

## What has been verified, and how

Every line here is something that was actually executed on 2026-09-13, not read off the source.

### Against a real PostgreSQL 17.11 server

A disposable cluster was created with `initdb` on port 55432 — separate from any existing
instance, with its own data directory, and needing no existing credentials. No production or
pre-existing database was touched.

| What | Result |
|---|---|
| `alembic upgrade head` from an empty database | All migrations applied |
| `alembic check` | `No new upgrade operations detected` |
| Full `base → head → base → head` round-trip | Clean at both ends; every downgrade runs |
| Migration onto a second, independently created database | Clean, `check` clean |
| Savepoint / concurrent-insert recovery (`test_job_hunt_concurrency.py`) | 6 passed |
| Production-mode boot (`ENVIRONMENT=production`) | `/health` 200, `/health/ready` 200 `database: reachable`, `schema: current` |
| `GET /openapi.json` through the real server | 200, 237 KB — every route builds |
| `scripts/run_due_alerts.py --dry-run` and a real run | Exit 0 both times |

### Security probes against a running production-mode server

| Probe | Result |
|---|---|
| `Host: evil.example.com` | `400 Invalid host header` |
| Protected route without a token | `401 {"detail":"Not authenticated"}` |
| Error bodies | No traceback, no `sqlalchemy`/`psycopg2`, no DSN, no key |
| Malformed JSON to `/auth/login` | `422` structured validation error |
| HSTS over plain HTTP | Absent — correct, the header is scheme-conditional |
| HSTS with a trusted forwarded proto | `max-age=63072000; includeSubDomains` |

That last pair is the measurement behind section 7's insistence on `--forwarded-allow-ips`.

### Secrets

- Both repositories scanned across **full git history**, by filename and by content, for
  Anthropic keys, live Razorpay keys, AWS keys, GitHub tokens, private-key blocks, Google API
  keys and DSNs containing passwords. **No match, and no secret-bearing file was ever
  committed.** No history rewrite is needed.
- The **release APK was unpacked and scanned** (7.1 MB of dex, resources and manifest). No
  JWT secret, no database credential, no Jobvetta key, no Razorpay secret, no private key.
  What it does carry is the API base URL and the package name, which is correct.
- All 42 settings appear in `.env.example`; no undocumented setting, no orphan key.

### Data isolation

All 64 routes carrying an id path parameter were swept. Every owned-model lookup either sits
beside an ownership check, derives its id from a row already ownership-checked, or targets a
shared catalogue model with no owner column at all. The eight that a static sweep could not
settle were traced by hand to their creation sites, where `build.profile_id != profile.id` and
`jd.profile_id != profile_id` are enforced.

## What is not verified

Stated plainly so nothing here is mistaken for a completed step.

- **Live Jobvetta API: NOT VERIFIED — no API key supplied.** `JOBVETTA_API_KEY` is empty in the
  local (untracked) `.env`, `JOB_PROVIDER` is `none`, and the factory selects `NullJobProvider`
  — confirmed by reading the resolved settings, not the file. The adapter's handling of
  401/403, 429, timeouts, 404, malformed payloads and empty results is covered by 107 passing
  tests against recorded fixtures. **No live request has been made, and none will be without
  an explicit instruction and a real key.** Where the key goes: section 5, Render environment
  group — never the repository, never a chat message.

- **Physical-device QA: NOT VERIFIED.** No device is connected. The release APK builds, passes
  R8, and contains no secrets, but no screen has been exercised on hardware. Login, resume
  upload, resume re-open, premium gating, offline behaviour and push notifications are all
  unverified in the only environment that can actually settle them.

- **Docker: NOT BUILT, deliberately.** Docker is absent here — no binary, no install directory,
  no service, no WSL. No Dockerfile is included, because container configuration that has never
  been built is an unverified artifact dressed as a deliverable. Render's native Python build
  needs none (section 26).

- **Deployment itself: NOTHING HAS BEEN DEPLOYED.** No Render account, service, database, bucket
  or Cron Job exists. No domain, no DNS, no TLS certificate. No production secret has been
  generated — by design; see section 27 step 4.

- **Managed backups: NOT CONFIGURED.** The backup and restore *scripts* are verified end to end
  against real PostgreSQL (60 tables and an identical Alembic revision after a round-trip), but
  no schedule exists anywhere because no managed database exists. Render's free PostgreSQL tier
  includes no automatic backups at all — do not assume a managed database is a backed-up one.

- **Razorpay live mode: NOT VERIFIED.** Test-mode keys only. Startup refuses an `rzp_test_` key
  in production, so this cannot ship half-configured, but no live payment has been taken.

- **Multi-worker and load behaviour: NOT VERIFIED.** Every measurement here is single-process.
  The connection arithmetic in section 3 is arithmetic, not an observation: workers × 30
  connections against the plan's cap. Check it against the real instance before raising
  `--workers`.

- **Email delivery: NOT VERIFIED.** `EMAIL_PROVIDER=smtp` is enforced in production and the
  console provider is refused, but no message has been sent through a real SMTP server.

