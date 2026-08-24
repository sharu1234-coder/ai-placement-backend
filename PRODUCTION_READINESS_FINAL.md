# AI Placement OS — Final Production Readiness Audit

**Date:** 2026-08-24
**Scope:** Item 20/20 of the SGLD Quant Labs production-hardening checklist — a full audit of
both repos (`ai-placement-backend`, `ai-placement-android`), verified against real tests/
builds/CI, not just read from documentation. **No deployment or publishing was performed.**

**Architecture reaffirmed (unchanged by this audit):** Android → FastAPI → PostgreSQL is the
primary system. Firebase is auxiliary only (FCM, Crashlytics, Analytics, App Check) — no
Firestore, no Firebase Auth, no Firebase Storage. No subscriptions/payments implemented.

## Overall status

**The codebase is READY for a production deployment to begin, gated on the manual actions
listed below.** Every one of the 19 prior checklist items was independently re-verified here
(not assumed from its own status entry), one small real gap was found and fixed (see
"Findings fixed in this audit"), and one small real gap was found and left open, honestly
flagged. There are **zero BLOCKERs in application code**. Every BLOCKER-level item is an
external resource or decision only the project owner can provide (a real Postgres instance,
a real domain, a real keystore, a chosen AI/storage/email vendor) — none of these can be
created by an agent working in this environment.

---

## Classification key

- **READY** — verified working now, nothing further needed for launch.
- **BLOCKER** — must be resolved before a real production launch; usually needs the project
  owner's action (credentials, a real external resource, a decision).
- **NEEDS MANUAL ACTION** — code is ready, but a one-time manual step by the project owner is
  required (generate a keystore, set a secret, etc.) — distinguished from BLOCKER in that
  there's a clear, already-documented procedure to follow, not an open decision.
- **FUTURE ENHANCEMENT** — real, honestly-flagged gap that does not block a first production
  launch (either low risk, or explicitly out of this project's current phase, e.g.
  subscriptions).

---

## Audit findings by area

### 1. Backend production configuration and environment variables — READY
All settings are centralized in `app/core/config.py` (`pydantic-settings`, reads `.env`).
`.env.example` documents every variable with dev-default vs. production-required guidance.
`validate_for_production()` (called at startup) fails fast if `ENVIRONMENT=production` and
the JWT secret is still the dev default, `EMAIL_PROVIDER=console`, or `DEBUG=true`.
**Fixed in this audit**: that check previously compared `environment == "production"` exactly,
so a typo'd value (`"Production"`, `"prod"`, …) fell into the "not production" branch and
silently skipped every check — fail-*open* on the one setting whose entire job is catching an
insecure production deploy. Inverted to skip only for the one known-safe value
(`"development"`), so anything else is validated. Regression test added
(`test_validate_for_production_still_validates_a_typoed_environment_value`, 4 typo variants).

### 2. JWT/security configuration and secret handling — READY (secret itself is a manual action)
`app/core/security.py` uses `pyjwt` with `HS256`, access + refresh tokens, a server-side
`token_version` claim (item 15) checked on every authenticated request and on `/auth/refresh`
— `POST /auth/logout` and a password reset both bump it, immediately invalidating every
previously-issued token. No hardcoded production secret exists anywhere in source; the dev
default is intentionally obviously-insecure (`"dev-only-insecure-secret-change-in-production"`)
and is rejected by `validate_for_production()`. **Manual action**: generate a real secret
before setting `ENVIRONMENT=production` (`python -c "import secrets; print(secrets.token_urlsafe(64))"`,
as the error message itself instructs) and store it in the deployment platform's secret store.

### 3. PostgreSQL production readiness — READY (instance itself is a BLOCKER, external)
`psycopg2-binary` is in `requirements.txt`; `app/database/session.py`'s engine is
driver-agnostic (SQLite-only `check_same_thread` arg is conditional on the URL scheme) with
`pool_pre_ping=True` (guards against a managed Postgres instance silently dropping idle
connections — real risk for RDS/Cloud SQL/Supabase-style managed databases). Item 3 verified
all migrations live against a real local PostgreSQL 17 instance. **BLOCKER (external)**: no
staging/production PostgreSQL instance exists yet — needs the project owner to provision one
(RDS, Cloud SQL, Supabase, or self-hosted) and set `DATABASE_URL`. **FUTURE ENHANCEMENT**:
`pool_size`/`max_overflow` are left at SQLAlchemy's defaults (5/10) — fine to start, worth
tuning once real concurrent load is observed (the new request-duration logging from item 18
is the tool to observe it with).

### 4. Database migrations and rollback readiness — READY
`alembic history`/`alembic heads` re-checked live in this audit: single linear chain, 13
migrations, one head (`b53876ba4675`), no branching. Item 16 already live-verified a full
`upgrade head` → `downgrade base` → `upgrade head` round trip against real PostgreSQL 17.
`GET /health/ready` checks the DB schema is at the code's expected migration head, not just
that the DB is reachable — catches "deployed code, forgot to migrate" before it causes a
500 on a real request.

### 5. Backup/restore procedures — READY (no active schedule yet, correctly so)
Full runbook in `OPERATIONS.md`: `scripts/backup_db.py`/`restore_db.py` (pg_dump/pg_restore
for Postgres, file copy for SQLite), live-verified in item 16 via a real
signup → backup → destroy → restore → verify cycle. **No backups are currently "active"
anywhere — correctly so, since no production database exists yet.** **Manual action once a
production database exists**: schedule the hosting platform's native automated snapshots
first (simpler, usually free/cheap on managed Postgres), with these scripts as the
tested fallback/manual-recovery path. **FUTURE ENHANCEMENT**: off-site backup storage,
backup encryption at rest — not needed until backups are actually being taken.

### 6. API health/readiness endpoints — READY
`GET /health` (liveness, no dependency checks) and `GET /health/ready` (checks real DB
connectivity + schema currency, 503 if either fails) both exist and are covered by tests
(`tests/api/test_health.py`). Suitable for a deployment platform's health-check/orchestrator
probe today.

### 7. Error handling and safe logging — READY
Global exception handlers for `RequestValidationError` (422) and bare `Exception` (500) never
leak stack traces to the client; both now include a `request_id` in the response body (item
18) so a user-reported error can be correlated with server logs. `RequestObservabilityMiddleware`
logs method/path/status/duration for every request, escalating to WARNING past 3s. Every
logging call site has been reviewed twice for accidental secret leakage (items 15 and 16) —
the one real issue found (a readiness-check DB error potentially echoing a DSN with an
embedded password via some drivers) was fixed in item 16.

### 8. CORS/security headers — READY (CORS not applicable to this architecture)
No CORS middleware is configured, and **this is correct, not a gap**: the only client is a
native Android app via OkHttp/Retrofit, which is not subject to browser CORS enforcement at
all. `TrustedHostMiddleware` (Host header allowlist), a body-size cap (12MB ASGI middleware),
and conditional HSTS (production + actual HTTPS only, checked fresh per request) are all
wired in and tested. TLS itself terminates at the reverse proxy/platform, not this app —
documented, with the required Uvicorn `--proxy-headers`/`--forwarded-allow-ips` flags spelled
out in README. **FUTURE ENHANCEMENT**: if a browser-based admin dashboard or web client is
ever added, CORS middleware will need to be added then, scoped to that client's real origin.

### 9. Rate limiting and abuse protection — READY
`slowapi`, in-memory storage (no Redis needed), IP-keyed. Auth endpoints (item 15): login
5/min, signup 3/hr, forgot-password 3/hr, reset-password 10/min, refresh 20/min. All 10
AI-calling endpoints (item 19): resume upload / roadmap generate+reassess at 10/hour, JD
parsing at 20/hour, six session-interactive endpoints (interview/assessment
next-question/answer/audio/evaluation) at 60/hour. Verified live in item 15 (5 wrong logins →
6th got 429 against a real running instance) and by 16 new automated tests across items 15
and 19.

### 10. File/resume/interview-audio upload validation — READY
Resume upload: content-type allowlist, 10MB streaming size cap (never buffers past the cap
regardless of what the client sends), and a `%PDF-` magic-byte check on the actual bytes
(item 15). Interview audio upload: content-type allowlist, 10MB cap, **and now also a
magic-byte check** — found missing during this audit's re-read of item 19 and closed then
(M4A/MP4's `ftyp` box marker at bytes 4:8). **FUTURE ENHANCEMENT**: no deep
structural/malware content scanning for either (would need libmagic or an external scanning
service — real paid/extra infra, out of scope without a specific request); no per-user
resume-count or total-storage quota (rate limiting bounds abuse *rate*, not unbounded
*accumulation* over time — a real gap, flagged in item 19, bigger design decision than that
item's rate-limiting scope).

### 11. AI endpoint protection and limits — READY
Covered together with #9 above — all 10 AI-calling endpoints are rate-limited. Text inputs
that reach an AI prompt already had length caps confirmed by reading each schema (not
assumed): `ParseJDRequest.raw_text` 20k chars, `student_answer`/`answer_text` 5k chars each.
`AIProvider`/`StorageProvider`/`EmailProvider` are all pluggable abstractions with safe local
defaults (Ollama/local disk/console-log) and a production implementation
(Anthropic/S3/SMTP) that fails loudly if selected without its required credentials — no
silent fallback to an insecure or broken state.

### 12. Firebase FCM, Crashlytics, Analytics, App Check configuration — READY (scope honestly bounded)
Wired in on Android only (item 17): one Firebase project (`ai-placement-os`), two registered
apps (`com.aiplacementos.app` prod / `.dev` dev). `firebase-bom` pinned to `34.1.0` for
Kotlin-version compatibility, verified against Google's real Maven metadata, not assumed.
`google-services.json` for both flavors is gitignored and was never committed at any point in
history (re-verified in this audit via `git log --all`, `git ls-files`, `git status`) — CI
gets them from two separate GitHub Actions secrets. FCM (`AppFirebaseMessagingService`),
Crashlytics, and Analytics auto-initialize. App Check installs a provider (Debug for debug
builds, Play Integrity for release) but **is not enforced anywhere** — the backend has no
Firebase Admin SDK integration and none is planned (Firebase stays auxiliary-only per the
locked-in architecture decision), so App Check tokens are generated but nothing currently
checks them. `ObservabilityInterceptor` (item 18) is the first real *use* of Crashlytics
beyond its automatic fatal-crash handling — HTTP 5xx and most `IOException`s are recorded as
non-fatals. **Manual action**: register a debug-token in Firebase Console for real-device App
Check debug testing (one-time, per physical test device) — not needed for CI/emulator builds.
**NEEDS MANUAL ACTION**: no Firebase Performance Monitoring — deliberately not added (new
Firebase product beyond the locked-in FCM/Crashlytics/Analytics/App Check scope); revisit only
if explicitly requested.

### 13. Dev vs production Android flavors/configuration — READY
`environment` product flavor dimension (`dev`/`prod`) drives: `API_BASE_URL`,
`usesCleartextTraffic` (dev=true, prod=false — verified by inspecting the *merged* manifest
output, not just source), `applicationIdSuffix`, and which `google-services.json` is picked up.
`PROD_API_BASE_URL` currently defaults to a deliberately-broken placeholder
(`https://api.aiplacementos.example.invalid/`) so a misconfigured prod build fails obviously
(DNS error) instead of silently talking to the wrong server. **BLOCKER (external, expected)**:
no real backend hosting URL exists yet — set `PROD_API_BASE_URL` in `local.properties`
(or the CI/build-time equivalent) once the backend is actually deployed and has a real domain.

### 14. Android release signing configuration — NEEDS MANUAL ACTION
`app/build.gradle.kts` reads release-signing credentials from a gitignored
`keystore.properties` (`keystore.properties.example` is the tracked template). Verified
end-to-end in item 12 with a real throwaway test keystore (unsigned baseline confirmed,
then a signed APK's certificate verified with `apksigner verify --print-certs`) — genuine
proof the wiring works, not just that Gradle config parses. **No real production keystore
exists in this environment and cannot be generated here** (it must be created and held by the
project owner — losing it means never being able to publish an update to an app already on
Play under the old key). **Manual action**: run the `keytool -genkeypair ...` command already
documented in `keystore.properties.example`, store the resulting `.keystore`/credentials
somewhere safe and backed up **outside version control**, and fill in `keystore.properties`
locally (or the CI secret equivalent) before a real release build.

### 15. R8/shrinking configuration — READY
`isMinifyEnabled`/`isShrinkResources` on for the release build type (item 13). No custom
ProGuard keep rules were needed — verified empirically (both back in item 13 and re-confirmed
in item 17 after adding Firebase) by inspecting R8's real `mapping.txt` output for the
specific things most likely to break under shrinking in this codebase (Retrofit interfaces,
`kotlinx.serialization` DTOs, the one reflectively-constructed ViewModel) — all survive via
their libraries' own bundled consumer rules or R8's own reflection tracing. **Re-verified in
this audit**: a fresh `assembleProdRelease` (with this audit's manifest/backup-rules changes
included) built successfully, R8 ran, `mapping.txt` was produced, unsigned release APK
~2.4MB.

### 16. GitHub Actions CI/CD — READY
Both repos have CI-only workflows (no CD/publish/signing step, matching "keep production
deployment/publishing disabled"), least-privilege (`permissions: contents: read`), with a
`concurrency`+`cancel-in-progress` group to stay within free Actions-minutes. Backend: ruff +
pytest, no external services needed (in-memory SQLite). Android: wrapper-validation (supply-
chain check) → unit tests (both flavors) → lint → debug builds (both flavors) → unsigned
release build (re-exercises R8 on every change). **Re-confirmed in this audit**: latest runs
on both repos' `master` are green (backend
[32718954592](https://github.com/sharu1234-coder/ai-placement-backend/actions/runs/32718954592),
Android commit `0071f08` — CI run for this exact commit watched below).
**FUTURE ENHANCEMENT (closed in this audit)**: Dependabot was missing entirely from both
repos (no automated dependency-vulnerability scanning) — added (`.github/dependabot.yml`:
pip + github-actions for backend, gradle + github-actions for Android). **Immediately opened
7 PRs on backend and 5 on Android**, confirming it's live. **Manual action / caution**:
several of these must NOT be merged without checking first — Dependabot doesn't know about
this project's deliberate pins. Notably: `anthropic` 0.125.0 → 1.0.0 (backend PR #5) is
exactly the major-version jump `requirements.txt`'s own comment says is incompatible (the 1.x
line needs Python ≥3.10; this project is pinned to 3.9); `bcrypt` 4.0.1 → 5.0.0 (PR #4) may
hit the same native-extension import issue that pinned it to 4.0.1 in the first place; `fastapi`
0.115.6 → 0.141.1 (PR #6) is a large jump worth its own regression pass, not a rubber-stamp
merge. On Android, `retrofit` 2.11.0 → 3.0.0 (PR #2) is a major version with potential
breaking API changes. Review each PR's CI result and, for anything crossing a pinned/major
version, re-read the reasoning in that dependency's original pin comment before merging.

### 17. Secrets and credentials — ensure nothing sensitive is committed — READY
Re-verified from scratch in this audit, not assumed from prior items' claims:
- Backend: only `.env.example` (placeholders only, every real value blank/obviously-fake) is
  tracked; `.env` is gitignored; no secret-shaped filenames anywhere in git history.
- Android: only `keystore.properties.example` (placeholders) is tracked;
  `keystore.properties`/`*.jks`/`*.keystore` gitignored; `google-services.json` for both
  flavors gitignored and confirmed absent from git history via three independent checks
  (`git status`, `git ls-files`, `git log --all`).
- GitHub repo secrets, re-checked live via `gh secret list`: backend repo has **zero**
  secrets configured (needs none — CI uses in-memory SQLite); Android repo has exactly
  `GOOGLE_SERVICES_JSON_DEV`/`GOOGLE_SERVICES_JSON_PROD` and nothing else. Both repos
  confirmed **private** via `gh repo view`.
- No API keys, passwords, or tokens found hardcoded anywhere in either app's source during
  this audit's review.

### 18. Android permissions and production configuration — READY (one real fix applied)
`AndroidManifest.xml` requests exactly three permissions, all justified by an actual shipped
feature: `INTERNET` (all API calls), `RECORD_AUDIO` (voice interviews), `POST_NOTIFICATIONS`
(FCM, API 33+ only, requested at runtime in `MainActivity`). No excessive/unused permissions.
**Fixed in this audit**: `android:allowBackup="true"` was set with no
`dataExtractionRules`/`fullBackupContent` restricting what Android's Auto Backup includes —
meaning `TokenStore`'s DataStore file (which holds the access/refresh JWTs **unencrypted**,
already a known, self-documented gap in that file) could have been swept into a cloud backup
or a device-to-device transfer, extending the token's exposure beyond the original device.
Added `app/src/main/res/xml/data_extraction_rules.xml` (API 31+) and `backup_rules.xml`
(API < 31, minSdk is 26) excluding that specific file from both cloud backup and
device-transfer; wired into the manifest; **re-verified by inspecting the actual merged/
packaged manifest output**, not just the source, that both attributes survive merging.
**NEEDS MANUAL ACTION / FUTURE ENHANCEMENT (real, still open)**: the token store itself
remains unencrypted at rest on-device (readable by an attacker with root or physical device
access, independent of the backup fix above). Migrating to
`androidx.security.crypto` (`EncryptedSharedPreferences`) or DataStore+manual crypto is a
real, moderately-sized change (new dependency, a migration path for already-installed users)
that was judged out of scope for this audit item — flagged clearly rather than silently
deferred, since the code's own existing comment already called it out as needed "before any
real user testing."

### 19. Test coverage and final test execution — READY
Both suites re-run in full during this audit (not assumed from a prior item):
- **Backend**: 230 tests passing (226 + 4 new from this audit's typo'd-environment fix),
  `ruff check` clean on every file this project has ever touched (20 pre-existing findings
  remain in one untouched Alembic auto-generated migration file, unrelated to any of this
  session's work).
- **Android**: 30 unit tests passing (`testDevDebugUnitTest`+`testProdDebugUnitTest`),
  `lintDevDebug`/`lintProdDebug` clean, `assembleDevDebug`/`assembleProdDebug` build, and a
  fresh `assembleProdRelease` (R8 + resource shrinking) builds successfully — all re-run
  after this audit's manifest/backup-rules change, confirmed via the actual merged manifest
  output, not just that the build didn't fail.
- **Coverage gap, honestly carried forward from item 9**: ~14 of the ~23 total Android
  ViewModels still have no unit tests (interview, job matching, resume upload, tests/
  assessments, roadmap, role selection), and there are zero Compose UI/instrumented
  (`androidx.test`) tests. Not a blocker for launch (the tested ViewModels cover
  auth/settings/legal/profile/home — the highest-risk, most security-sensitive flows), but a
  real, explicitly-flagged gap for anyone extending this further.

### 20. Documentation/runbooks required for future deployment — READY (this document + OPERATIONS.md)
`OPERATIONS.md` (backend) already covers ongoing operations: backup/restore, migration
rollback, health checks, safe logging, and incident recovery (DB unreachable, bad migration,
suspected JWT compromise, suspected account compromise, abuse/rate-limit incidents) — all
with real, previously-verified procedures, not theoretical ones. This document adds the piece
that was missing: a **first-time go-live runbook** (below) and a single consolidated list of
every manual action the project owner must perform, pulled together from 20 items' worth of
scattered status entries into one place.

---

## Manual actions the project owner must perform before a real launch

These cannot be done by an agent in this environment — no access to real cloud accounts,
billing, domains, or physical keys/devices.

1. **Provision a production PostgreSQL instance** (RDS, Cloud SQL, Supabase, or self-hosted)
   and set `DATABASE_URL` via the deployment platform's secret store. *(#3)*
2. **Generate a real JWT secret** (`python -c "import secrets; print(secrets.token_urlsafe(64))"`)
   and set `JWT_SECRET_KEY` via the deployment platform's secret store. *(#2)*
3. **Choose and configure production AI/storage/email vendors** — all three already have a
   working, pluggable production implementation that only needs credentials:
   - `AI_PROVIDER=anthropic` + `ANTHROPIC_API_KEY` (Anthropic Console)
   - `STORAGE_PROVIDER=s3` + an S3-compatible bucket's credentials (AWS S3, Cloudflare R2,
     Backblaze B2, DigitalOcean Spaces, or self-hosted MinIO — pick one)
   - `EMAIL_PROVIDER=smtp` + real SMTP credentials (any SMTP-speaking service)
   None of these were pre-selected — this session deliberately did not choose a paid vendor
   without being asked to. *(#1, #11 area)*
4. **Set `ENVIRONMENT=production`, `DEBUG=false`, and `ALLOWED_HOSTS`** to the real
   production domain(s) once one exists. *(#1, #8)*
5. **Run the backend behind a reverse proxy / platform that terminates TLS**, and start
   Uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy IP or "*">` so HSTS and
   scheme-detection work correctly (documented in backend README's "HTTPS / network
   security" entry). *(#8)*
6. **Generate a real Android release keystore** (`keytool -genkeypair ...`, command already in
   `keystore.properties.example`) and store it somewhere safe, backed up, **outside version
   control** — losing it means never being able to publish an update to an already-published
   app. Fill in `keystore.properties` (or the CI secret equivalent) before a real release
   build. *(#14)*
7. **Set `PROD_API_BASE_URL`** to the real backend's public URL once it's deployed and has a
   real domain (currently a deliberately-broken placeholder so a misconfigured build fails
   loudly rather than silently). *(#13)*
8. **Register an App Check debug token** in Firebase Console for each physical test device
   used before Play release (one-time per device; not needed for CI/emulator). *(#12)*
9. **Have Privacy Policy and Terms of Service reviewed by a real lawyer** for the actual
   publishing jurisdiction(s), and replace the placeholder contact emails
   (`privacy@sgldquantlabs.com`, `legal@sgldquantlabs.com`) with real monitored addresses —
   both documents' own drafting notes already flag this (items 6, 7).
10. **Decide on and configure automated database backups** once a production database
    exists — start with the hosting platform's native snapshot feature; `scripts/backup_db.py`
    is the tested manual fallback. *(#5)*
11. **Play Store requirements not covered by this checklist at all** (out of scope for a
    20-item hardening pass, listed here only so nothing is assumed done): a Play Console
    developer account, store listing content (screenshots, descriptions, content rating
    questionnaire), a privacy policy URL reachable from the listing, and Play's own signing
    key setup (Play App Signing vs. the upload keystore from #6 above) if publishing.
12. **Review the 7 (backend) + 5 (Android) Dependabot PRs already open** before merging any
    of them — several conflict with deliberate version pins documented in this project (see
    #16 above for specifics: `anthropic`, `bcrypt`, `fastapi`, `retrofit`). Dependabot doesn't
    know about those constraints; a human does need to check each one.

---

## Go-live runbook (first production deployment)

A condensed, ordered version of the manual actions above, for whenever the project owner is
actually ready to deploy:

1. Provision PostgreSQL → get `DATABASE_URL`.
2. Generate `JWT_SECRET_KEY`.
3. Pick and configure AI/storage/email vendors → get their credentials.
4. Set all of the above plus `ENVIRONMENT=production`, `DEBUG=false`, `ALLOWED_HOSTS` in the
   backend's deployment platform's secret/env store.
5. Deploy the backend behind a TLS-terminating reverse proxy/platform;
   start with `--proxy-headers --forwarded-allow-ips=...`.
6. Run `alembic upgrade head` against the real production database (or let the deployment
   platform's release step do it) — verify via `GET /health/ready` (`schema: "current"`).
7. Confirm `GET /health` and `GET /health/ready` both return 200 from the real public URL.
8. Set `PROD_API_BASE_URL` in the Android build to the real backend URL; rebuild
   `assembleProdRelease` (or `bundleProdRelease` for a Play `.aab`).
9. Generate (if not already done) and apply the real release keystore; verify the signed
   build with `apksigner verify --print-certs`.
10. Register App Check debug tokens for any physical test devices.
11. Do one real end-to-end smoke test against production: signup → login → resume upload →
    JD paste → an interview or assessment flow → logout — the same category of live
    verification this session has run at every prior item, just against the real deployment
    instead of a local one.
12. Set up automated backups (hosting platform snapshots, or a cron calling
    `scripts/backup_db.py`).
13. Only then: proceed to Play Store submission (separate process, not covered here).

---

## Tests and builds run in this audit (results)

- Backend: `pytest -q` → **230 passed**, 0 failed. `ruff check .` → 20 pre-existing findings
  in one untouched Alembic migration file, 0 in anything touched by this or any prior item.
- Android: `testDevDebugUnitTest testProdDebugUnitTest lintDevDebug lintProdDebug
  assembleDevDebug assembleProdDebug assembleProdRelease` → **BUILD SUCCESSFUL**, 30 unit
  tests passing, 0 lint errors, R8/shrinking re-confirmed working, merged manifest inspected
  directly to confirm the new backup-exclusion rules actually took effect.
- Both repos' git history re-audited for secrets (`git status`, `git ls-files`,
  `git log --all`) — clean.
- Both repos' GitHub secrets and visibility re-checked live via `gh` — as expected.
- Both repos' latest CI runs on `master` re-confirmed green before this item's own commit.

## Can the project proceed to deployment?

**Yes, when the project owner is ready** — pending only the manual actions listed above, all
of which require real-world resources/decisions (cloud accounts, a domain, a physical
keystore, vendor choices) that cannot be created inside this environment. No further code
changes are required to begin a production deployment; the go-live runbook above is the
path from here.

---

Committed backend `7558bd5`, Android `0071f08`, both pushed.
