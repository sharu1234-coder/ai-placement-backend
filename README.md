# AI Placement OS — Backend

FastAPI backend for AI Placement OS. See `../PROJECT_HANDOFF.md` for full architecture, schemas, and API contracts.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp .env.example .env          # optional for dev; required before real deployment
alembic upgrade head          # creates app.db (SQLite) with the current schema
```

## Run

```bash
uvicorn app.main:app --reload
```

Visit `http://127.0.0.1:8000/health` to confirm the app is running (liveness -- no
dependency checks). `http://127.0.0.1:8000/health/ready` additionally checks the database
is reachable (200 `{"status":"ok","database":"reachable"}`, or 503 if not) -- use this one
for deployment readiness/orchestrator probes.

## Production database (PostgreSQL)

SQLite (the default) is for local dev only. For staging/production, set `DATABASE_URL` to
a PostgreSQL URL:

```
DATABASE_URL=postgresql://<user>:<password>@<host>:5432/<dbname>
```

`psycopg2-binary` is already in `requirements.txt` and `app/database/session.py`'s engine
setup is already driver-agnostic (SQLite's `check_same_thread` connect arg is conditional
on the URL scheme; `pool_pre_ping=True` applies to both, guarding against a managed
Postgres instance silently dropping idle connections). Alembic migrations were verified
live against a real local PostgreSQL 17 instance -- see Status below. Never hardcode
credentials; set `DATABASE_URL` via your deployment platform's secret store.

## Test

```bash
pytest
```

## Migrations

```bash
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
```

## AI provider

Dev uses Ollama (`qwen3:8b` by default) via `app/ai/provider.py`'s `AIProvider` interface —
services never call Ollama or any vendor SDK directly. Requires Ollama running locally
(`ollama serve`, usually automatic) with the model pulled (`ollama pull qwen3:8b`).
A `CloudProvider` implementation is added when a production AI service is chosen; only
`AI_PROVIDER` in `.env` changes to switch.

`OllamaProvider` also implements `transcribe`/`synthesize_speech` (Stage 10) using local,
dev-only STT/TTS -- see Status below for details and why these must not ship to production
as-is.

## Status

**Production readiness item 8/20 — Forgot password / account recovery (2026-08-23).**
- New `POST /auth/forgot-password` (email) and `POST /auth/reset-password` (email, code,
  new_password). A short single-use code (8 hex chars, `secrets.token_hex(4)`, ~4.3B
  combinations) is generated, only its SHA-256 hash is stored (new `password_reset_tokens`
  table, migration `f9408c0e3622`, mirrors bcrypt-hashing passwords -- a DB leak alone must
  never yield a usable code), and it expires after `PASSWORD_RESET_TOKEN_EXPIRE_MINUTES`
  (15 default). Requesting a new code invalidates any previous unused one for that user, so
  only the newest code ever works.
- **Prevents email enumeration**: `/forgot-password` returns the exact same response
  ("If that email is registered, a reset code has been sent.") whether or not the email
  belongs to an account -- verified live (see below), not just asserted.
- New `app/email/provider.py` (`EmailProvider` ABC + `EmailError` + `get_email_provider()`),
  mirroring the `AIProvider`/`StorageProvider` pattern exactly. `ConsoleEmailProvider`
  (`EMAIL_PROVIDER=console`, dev default) logs the code instead of sending real email --
  never claims to have delivered anything. `SMTPEmailProvider` (`EMAIL_PROVIDER=smtp`) uses
  stdlib `smtplib` over plain SMTP, so it works with any SMTP-speaking service (Gmail app
  passwords, SendGrid/SES/Postmark/Mailgun SMTP relays, ...) -- same "no vendor lock-in,
  none chosen yet" reasoning as `S3StorageProvider` in item 4.
- A successful reset changes `password_hash`, which is what actually invalidates the code
  from being reused (plus the explicit `used_at` marker) -- a used or expired code fails
  with 400 either way, no distinction leaked to the caller.
- **Genuinely verified against real PostgreSQL 17**: ran every migration including the new
  one, then a live signup → forgot-password (known email) → forgot-password (unknown
  email, confirmed byte-identical response) → pulled the code from the server's own log
  (exactly how a real operator using the console provider would) → reset-password →
  confirmed old password now fails login and new password succeeds → confirmed the
  used code is rejected on a second attempt. Verification database dropped afterward.
- 12 new tests: 8 API (`tests/api/test_password_reset.py` -- unknown email, known email,
  full reset flow, wrong code, single-use enforcement, unknown email on reset, code
  invalidated by a newer request, expired code) + 4 unit (`tests/unit/test_email_providers.py`
  -- console logging, SMTP missing-config error, SMTP send via mocked `smtplib.SMTP`, SMTP
  send failure wrapped as `EmailError`). 181 passing total (was 169).
- **Not done**: no rate limiting on `/forgot-password` (a determined attacker could spam
  requests or brute-force an 8-hex-char code within its 15-minute window without one) --
  that's item 19 (File + AI abuse controls), not this item. No production email vendor
  chosen yet (same as storage in item 4) -- only SMTP credentials are needed, not a specific
  provider.

**Production readiness item 7/20 — Terms of Service (2026-08-23).**
- `GET /legal/terms-of-service` (public, no auth) added alongside item 6's privacy-policy
  endpoint — `app/api/legal.py` was refactored to a small shared `_read_legal_document()`
  helper rather than copy-pasting the read-and-wrap logic a second time. Content in
  `app/legal/terms_of_service_v1.txt`, same versioned-file convention as the privacy policy
  and the AI prompts.
- Written from the app's actual features, not boilerplate: section 3 ("AI-generated
  content — read this") is the load-bearing clause — it states plainly that every
  AI-produced score, question, and piece of feedback in this app (resume analysis,
  assessment grading, interview evaluation, roadmap) is practice guidance, not a placement
  guarantee or professional opinion, and can be wrong. Section 4 covers the content-license
  grant needed to send user text to the AI provider (items 1/4) without claiming ownership
  of it or using it for model training. Section 6 points at the item-5 account-deletion
  flow as how termination works on the user's side.
- Same drafting-note caveat as item 6: flags that sections 7-8 (warranty/liability
  disclaimers) need real legal review since enforceability varies by jurisdiction, that no
  governing-law clause is included yet, and that the contact email
  (`legal@sgldquantlabs.com`) is a placeholder.
- Android: new `TermsScreen`, reusing a newly-extracted shared `LegalDocumentViewModel`/
  `LegalDocumentContent` (the item-6 `PrivacyViewModel` was folded into this generic
  version rather than duplicating its fetch/Loading/Error/Success logic a second time —
  `PrivacyScreen` now uses the same shared pieces). New `Screen.Terms` route, wired from a
  new "Terms of Service" card in Settings next to the existing Privacy one.
- 2 new backend tests (`tests/api/test_legal.py`: public reachability, no-auth-required,
  mirroring item 6's tests). 169 passing total (was 167). Android:
  `compileDevDebugKotlin` BUILD SUCCESSFUL.
- **Not done**: legal review by a qualified lawyer, a real contact address, and a
  governing-law/dispute-resolution clause — all flagged in the document itself and in
  item 6's equivalent note.

**Production readiness item 6/20 — Real Privacy Policy (2026-08-23).**
- `GET /legal/privacy-policy` (public, no auth — must be reachable before signup, from a
  logged-out state, and from an app store listing) returns `{version, content}`. Content
  lives in `app/legal/privacy_policy_v1.txt`, versioned the same way this project already
  versions AI prompts (`app/ai/prompts/*_v1.txt`) — legal text changes without touching
  route code, and a future revision ships as `_v2.txt` without breaking old clients that
  cached the version number.
- The policy text was **written from this app's actual current data flows**, not a generic
  template: every data category in section 2 (account/profile/resume/JD/assessment/
  interview/roadmap data) maps to a real table introduced in prior stages; section 3 states
  plainly that raw interview audio is never persisted (matches `interview_service.py`'s
  existing "minimize stored voice data" design); section 4 names the real AI processor per
  environment (self-hosted Ollama in dev, Anthropic Claude API in production, added in item
  1) and the real storage backend (local disk in dev, S3-compatible in production, added in
  item 4); section 5 explicitly marks Firebase (FCM/Crashlytics/Analytics) as **not yet
  active** rather than claiming it's already collecting anything, since it isn't wired into
  the Android app yet (that's item 17); section 7 points at the account-deletion capability
  from item 5 as the mechanism for the deletion right it describes.
- **Explicitly flagged in the document itself** (a "drafting note" at the end, not part of
  the policy the user reads): this was generated from the code, not reviewed by a lawyer,
  and the contact email (`privacy@sgldquantlabs.com`) is a placeholder that needs to be a
  real, monitored address before commercial publishing. This is not a substitute for legal
  review — see that note for the full caveat.
- Android: `PrivacyScreen` (previously a `PlaceholderScreen` stub) now fetches and renders
  the policy via a new `LegalApi`/`LegalRepository`/`PrivacyViewModel`, with the same
  Loading/Error/Success handling every other data-driven screen already uses.
- 2 new backend tests (`tests/api/test_legal.py`: public reachability, no-auth-required).
  167 passing total (was 165). Android: `compileDevDebugKotlin` BUILD SUCCESSFUL.
- **Not done**: legal review by a qualified lawyer for the target jurisdiction(s) — flagged
  above and in the document itself. Terms of Service is a separate item (7), not covered
  here.

**Production readiness item 5/20 — Complete account deletion (2026-08-23).**
- `DELETE /users/me` (`app/api/users.py`, body: `{"password": "..."}`) requires the current
  password before deleting — a stolen/leaked access token alone can no longer trigger an
  irreversible wipe. Wrong password → 403, account untouched.
- `app/services/account_service.py`'s `delete_account()` erases the profile and **every**
  row derived from it, not just the `users` row: educations, certifications, experiences,
  skills, projects, resumes (+ analyses, + the underlying storage object via the
  `StorageProvider` from item 4), job descriptions (+ requirements), job matches (+ skill
  gaps), assessment blueprints/assessments (+ results/question attempts), interviews (+
  questions/answers/evaluations), roadmaps (+ tasks). The global `questions` bank (shared
  across all users) and `roles`/`role_skill_weights` catalogs are deliberately never
  touched — deleting one user's data must never affect another's.
- Deletes are ordered children-before-parents so it works on a real FK-enforcing database,
  not just SQLite's default (unenforced) FKs — verified two ways: a unit test
  (`tests/unit/test_account_service.py`) that turns on `PRAGMA foreign_keys=ON` for its
  in-memory SQLite engine (so an ordering bug would raise an `IntegrityError`, not just
  pass silently), and a **live run against real PostgreSQL 17** (signup → profile/skills/
  education → role-based assessment blueprint → delete → confirmed 0 rows in every child
  table via direct SQL, then successfully re-signed-up with the same email). Test database
  dropped afterward.
- File deletion (resume objects) is best-effort exactly like the existing single-resume
  delete: a stuck/unreachable storage object is logged and skipped rather than blocking the
  rest of the account wipe.
- The access token used to call `DELETE /users/me` stops working on the very next request —
  no separate token-revocation step needed, since `get_current_user`/`refresh` both do
  `db.get(User, user_id)` and the row is now physically gone (not just deactivated).
- 7 new unit tests (full-cascade row-by-row assertions, no-profile account, cross-user
  isolation) + 4 new API tests (auth required, wrong password, full delete-then-401 flow,
  email freed for re-signup). 165 passing total (was 158).
- **Not done**: no confirmation email / "are you sure" grace period (e.g. a 30-day
  soft-delete window before permanent erasure) — this is immediate, hard deletion. Add a
  grace period later if the product needs one; nothing here blocks that.

**Production readiness item 4/20 — Production file/resume storage (2026-08-23).**
- Added `app/storage/provider.py` (`StorageProvider` ABC + `StorageError` + `get_storage_provider()`),
  mirroring the existing `AIProvider` pattern exactly: services depend only on the
  interface, never a vendor SDK or the local filesystem directly.
- `LocalStorageProvider` (dev default, `STORAGE_PROVIDER=local`) preserves the exact prior
  behavior — writes under `UPLOAD_DIR`. `S3StorageProvider` (`STORAGE_PROVIDER=s3`) speaks
  the plain S3 API via `boto3`, so it works against real AWS S3 or any S3-compatible
  service (Cloudflare R2, MinIO, Backblaze B2, DigitalOcean Spaces, ...) by pointing
  `STORAGE_S3_ENDPOINT_URL` at that provider's endpoint — **no production vendor has been
  chosen yet**, and none needed to be for this item; any S3-compatible bucket works.
- `app/services/resume_service.py` and `app/api/resumes.py` updated to take an injected
  `StorageProvider` (`Depends(get_storage_provider)`) instead of writing to disk directly —
  upload (`_save_upload`) and delete (`delete_resume`) both route through it. Existing
  resume upload/parse/analysis flow is otherwise unchanged: same PDF validation, same size
  limit, same AI parsing pipeline, same response schema.
- `Resume.file_path` now stores an opaque **storage key** (e.g. `resumes/<profile_id>_<uuid>.pdf`)
  rather than an absolute filesystem path — a deliberate, minimal-blast-radius change since
  no endpoint ever returned this field to clients and there is no download endpoint reading
  it back. Column name unchanged (no migration needed).
- Delete is best-effort exactly as before: if the underlying object can't be removed (S3
  network error, permission error, already gone), it's logged and the DB row is still
  deleted — a stuck file must never block account/resume deletion.
- 7 new unit tests (`tests/unit/test_storage.py`): local save/delete round-trip, delete of a
  missing key is a no-op, missing-bucket config raises `StorageError`, S3 save/delete call
  the right boto3 calls with mocked `_client` (same mocking pattern as
  `test_anthropic_provider.py`), S3 failures raise/log without ever crashing the caller.
  158 passing total (was 151).
- **Not done**: no real S3-compatible bucket/credentials were exercised — this environment
  has none. To verify live: set `STORAGE_PROVIDER=s3`, `STORAGE_S3_BUCKET`, and credentials
  in `.env` (plus `STORAGE_S3_ENDPOINT_URL` for a non-AWS provider), then upload a resume
  and confirm the object exists in the bucket and deletion removes it.
- **Not done**: choosing the actual production storage vendor/bucket — that's part of item
  20 (final deployment readiness), not this one.

**Production readiness item 3/20 — PostgreSQL production support (2026-08-23).**
- Added `psycopg2-binary` driver, `pool_pre_ping=True` (guards against a managed Postgres
  instance silently dropping idle connections -- `app/database/session.py`), and
  `GET /health/ready` (checks real DB connectivity via the same `get_db` every request
  uses, 503 if unreachable -- distinct from the existing dependency-free `/health`
  liveness check).
- **Genuinely verified against a real PostgreSQL 17 instance**, not just structural
  driver/dialect checks: installed PostgreSQL 17 locally, ran every Alembic migration
  against it end to end (`alembic upgrade head` -- all 11 migrations applied cleanly,
  including the hand-fixed `server_default=CURRENT_TIMESTAMP` migration from the Progress
  dashboard work, confirming that syntax is genuinely Postgres-compatible and not just
  assumed to be), then ran the app against it live: signup, login (bcrypt + JWT), the
  seeded role taxonomy, profile read/update, and a JSON column round-trip
  (`AssessmentBlueprint.sections`) all worked correctly. Verification database dropped
  afterward -- nothing about this depends on that local instance still existing.
- `DATABASE_URL` was already the only thing standing between SQLite and Postgres (the
  session-setup code was already driver-agnostic) -- this item mainly *proved* that claim
  true rather than needing to change much to make it true.
- 1 new test (`tests/api/test_health.py::test_readiness_check_reports_database_reachable`)
  for the new endpoint. 151 passing total.
- **Not done**: no actual staging/production Postgres instance exists yet (that's item 20/
  deployment, and needs a real hosting decision from you -- RDS, Cloud SQL, Supabase,
  self-hosted, etc.). This item proves the app *works* against Postgres; it doesn't stand
  up a production one.

**Production readiness item 1/20 — Cloud AI provider (2026-08-23).** Part of a 20-item
production-hardening pass (subscriptions/payments explicitly out of scope for all 20
items). `app/ai/anthropic_provider.py` implements `AIProvider` using the Anthropic Python
SDK, selected via `AI_PROVIDER=anthropic` -- zero service/route changes needed, since every
service already depended only on the `AIProvider` interface.
- Uses `client.messages.parse(output_format=schema)` for structured output -- the SDK
  enforces the JSON schema server-side and validates the response against the Pydantic
  model for us. One retry with a correction prompt on `ValidationError`, exactly mirroring
  `OllamaProvider`'s existing contract (`generate_structured` retries once, then raises
  `AIGenerationError`) -- callers can't tell which provider they're talking to.
- Defaults to `claude-sonnet-5` (`ANTHROPIC_MODEL` overrides) -- Anthropic's own guidance
  recommends Sonnet-tier over Opus-tier for this kind of high-volume production workload
  (question generation, grading, JD/resume parsing, interview evaluation), at roughly 60%
  of Opus's per-token cost. Swap per-deployment, or per-task-type in a future pass, if a
  different quality/cost tradeoff is wanted.
- `anthropic==0.125.0` pinned in `requirements.txt` -- the newer `anthropic` 1.x line
  requires Python >=3.10; this project is pinned to 3.9 (see the `greenlet`/`bcrypt` pins
  already in the file for the same underlying reason).
- `transcribe()`/`synthesize_speech()` are **not** implemented on this provider (falls
  through to `AIProvider`'s `NotImplementedError` default) -- that's production-readiness
  item 2 (STT/TTS), scoped separately.
- 9 new mocked unit tests (`tests/unit/test_anthropic_provider.py`): structured-output
  success, retry-then-succeed, both-retries-fail, missing-parsed-output, API status/
  connection errors (verified *not* retried -- a network failure isn't a schema problem),
  and text-generation content-block filtering.
- **Not live-tested against the real Anthropic API** -- this environment has no
  `ANTHROPIC_API_KEY` (Claude Code sessions don't carry the user's own Anthropic billing
  key). To verify live: set `ANTHROPIC_API_KEY` and `AI_PROVIDER=anthropic` in `.env`, then
  run any endpoint that calls `generate_structured`/`generate_text` (e.g.
  `POST /jobs` with a real JD) against a running server.

**Stage 12 — Progress dashboard (2026-08-23)**, the last piece of V1 scope item #14
("Progress/Reassessment") not yet built, and the start of M12's end-to-end pass:
- `GET /progress/dashboard` (`app/services/progress_service.py`) aggregates three things
  the app already produces, deterministically (no AI call) -- same philosophy as
  `weakness_service`: a **readiness score** (average of the *most recent* assessment result
  and the *most recent* interview evaluation, not an all-time average, so one early bad
  attempt doesn't keep dragging down current standing), a **trend** (every assessment
  result + interview evaluation, chronological, merged from two tables), and the current
  **roadmap task completion** count (summed across all currently-`active` roadmaps).
  `top_weaknesses` reuses `weakness_service.compute_weaknesses` directly.
- Added `created_at` to `AssessmentResult` and `InterviewEvaluation` -- neither had *any*
  timestamp before this, so a "trend" was previously impossible to build honestly (only
  `User.created_at` existed anywhere in the schema). The autogenerated migration needed a
  manual fix: adding a `NOT NULL` column with no default fails against any table that
  already has rows (both SQLite and Postgres) -- added `server_default=CURRENT_TIMESTAMP`
  to backfill existing rows; the ORM's Python-side default still governs new rows.
- 10 new tests (7 unit tests directly exercising `get_dashboard` against a real in-memory
  schema, 3 API tests) -- 141 passing total.
- Verified live end-to-end against real Ollama in one script: signup -> role selection ->
  complete a full SDE assessment -> complete a technical interview -> generate a roadmap ->
  mark a task done -> `GET /progress/dashboard` -- confirmed the dashboard's counts, trend,
  and readiness score all matched what the flow actually produced.

**Architecture confirmation (2026-08-23)** — the user explicitly locked in the following,
all of which the backend already satisfies with no code changes needed:
- **FastAPI + SQLAlchemy + PostgreSQL stays the primary backend/database.** No migration of
  existing data/models to Firestore. `app/database/session.py`'s `create_engine` already
  takes its driver entirely from `DATABASE_URL` (the SQLite-only `check_same_thread`
  connect arg is already conditional on the URL scheme) -- switching to Postgres for
  staging/prod is `DATABASE_URL=postgresql://...` in `.env` plus adding `psycopg2-binary`
  to `requirements.txt` (not added yet since nothing runs against Postgres today), not an
  architecture change.
- **Firebase is for auxiliary services only** (FCM, Crashlytics, Analytics, App Check) --
  never a replacement for the above. Nothing to change here since no Firebase integration
  exists in this repo yet; if/when server-triggered push notifications are built, the
  intended shape is a `FirebaseAdmin`-wrapping service analogous to `AIProvider` (one
  interface, env-driven credentials), added when a concrete feature needs it -- not built
  speculatively now.
- **Existing JWT auth stays** (`app/core/security.py` -- HS256, `jwt_secret_key` from env)
  unless a specific technical reason to change it comes up. No Firebase Auth migration.
- **AI provider is already switchable without a rebuild**: `AI_PROVIDER` in `.env` selects
  the implementation in `app/ai/provider.py:get_ai_provider()`; every service depends only
  on the abstract `AIProvider` interface. Publishing swaps Ollama for a paid API by adding
  a `CloudProvider` implementation and changing one env var -- no service/route changes.

See `ai-placement-android/README.md`'s matching entry for the Android-side dev/prod Gradle
flavor split that makes the same "switch at publishing, don't rebuild" promise concrete
there too.

**Stage 11 — Weakness Engine + Roadmap (2026-08-23)**, the last V1 milestone
(`PROJECT_HANDOFF.md` M11), built on every signal the app already produces about a
student's weak spots:
- `app/services/weakness_service.py` — deterministic (non-AI) aggregation across three
  existing signals: JD `SkillGap`s (only when a `jd_id` scope is given), an assessment's
  `AssessmentResult.priority_weaknesses`, and any `InterviewEvaluation` dimension scoring
  below 0.6 (surfaced as a synthetic `interview:<dimension>` skill tag, e.g.
  `interview:clarity`). Same "our own gap engine, not delegated to AI" philosophy as
  `skill_matching.skill_present` — only the roadmap's task *text* is AI-generated; *which*
  skills are weak is computed here from real, owned data. The same skill appearing from
  multiple sources merges into one entry with escalated severity and a combined source
  list, so e.g. a skill that's both a JD gap and a test-result weakness surfaces once, at
  its highest severity.
- `app/services/roadmap_service.py` — `generate_roadmap` calls `compute_weaknesses` and,
  if none exist, raises a 422 rather than generating a roadmap from nothing (there must be
  at least one completed test, interview, or JD match first). Only one `Roadmap` is
  "active" per profile+jd scope at a time; generating a new one (including via reassess)
  marks the previous one `superseded` rather than deleting it.
- **`POST /roadmap/{id}/reassess` regenerates from scratch** rather than patching the
  existing task list in place — same `jd_id`/`duration_days`, fresh weaknesses, fresh AI
  call. Simpler and safer than merging partial task-list diffs; an explicit scope decision,
  same reasoning as the JD-delete 409-over-cascade choice in the prior audit.
- **`GET /roadmap/current`** — not in the documented endpoint table (section 8.5), added so
  a client can load "my active roadmap" directly rather than only ever holding onto an id
  returned from `/generate`. Returns `null` (200, not 404) when none exists yet, since "no
  roadmap generated yet" is a normal state, not an error.
- `RoadmapTask.due_date` is computed server-side from `date.today() + (day - 1)` at
  generation time — the AI only assigns relative day numbers (1..duration_days), never
  calendar dates, so a roadmap's dates are always anchored to when it was actually
  generated (or regenerated).
- 20 new tests (11 unit tests directly exercising `compute_weaknesses` against a real
  in-memory schema, 9 API tests over the full generate/current/patch/reassess lifecycle) —
  131 passing total.

**Whole-codebase audit (2026-08-23)** — a full review of `app/`, `app/services`, `app/api`,
`app/ai` against the running test suite surfaced and fixed several real issues, most
severe first:
- `skill_matching.skill_present` used plain substring containment (`"r" in "docker"` was
  `True`), silently fabricating skill matches for short skill names and for an empty JD
  skill value. Replaced with token-boundary matching (see the function's docstring). This
  affects the core resume↔JD gap analysis and the assessment blueprint's gap detection.
- `jd_service.detect_role` matched the *first* seeded role whenever the AI returned a
  blank `role_title`, because `"" in name_lower"` is trivially `True`. Now returns `None`
  for a blank/whitespace-only title instead of a wrong role.
- `resumes.py`'s `upload_resume` had the exact same event-loop-freezing bug already found
  and fixed in Stage 10's interview audio endpoint (an `async def` route calling a
  blocking service function directly) -- flagged as a known unfixed instance in the Stage
  10 notes below, now fixed the same way with `run_in_threadpool`.
- N+1 queries in `assessment_run_service` (`get_next_question`/`get_result`, one
  `db.get(Question, ...)` per attempt) replaced with the same bulk `IN (...)` pattern
  `interview_service` already used correctly for the equivalent problem.
- A check-then-set race on the lazy-loaded Whisper model (two concurrent first voice
  answers could each start loading their own model instance) fixed with a
  `threading.Lock`.
- Upload size limits (resumes, audio answers) were enforced only *after* the full body was
  already buffered in memory; `app/api/upload_utils.py`'s `read_upload_limited` now reads
  in chunks and aborts as soon as the limit is exceeded.
- Added `max_length` caps on previously-unbounded free-text fields (JD `raw_text`,
  assessment/interview answer text) that were otherwise embedded directly into AI prompts
  with no size limit.
- **New: deletion endpoints.** PROJECT_HANDOFF.md section 6 states "provide deletion
  controls for user data" and "protect resumes and uploaded documents" as non-negotiable,
  but no DELETE route existed anywhere before this audit. Added `DELETE /resumes/{id}`
  (also removes the file from disk, not just the DB row), `DELETE /jobs/{id}` (cascades
  through its Requirements/JobMatch/SkillGap, but refuses with 409 if an
  AssessmentBlueprint or Interview already depends on it, to avoid dangling a `jd_id`),
  and `DELETE /users/me/{education,skills,projects,experience,certifications}/{id}`.
- 18 new tests added for all of the above (111 passing total). Full findings list
  (including lower-priority items not fixed, e.g. pagination on list endpoints) available
  on request -- not all of it is reproduced here.

Stage 10 complete — Voice Interview (turn-based STT/LLM/TTS), built on Stage 9's lifecycle
(same endpoints, `mode="voice"` now accepted):
- `OllamaProvider.transcribe()` (STT) uses local `faster-whisper` (`tiny.en` by default,
  CPU, int8) -- dev-only, real inference, not a stub. `OllamaProvider.synthesize_speech()`
  (TTS) uses local Windows SAPI5 via `pyttsx3` -- also dev-only, real (if robotic) audio,
  never a stub. Both must be swapped for cloud providers before real user testing; both
  are documented in their own docstrings.
- `GET /interviews/{id}/questions/{question_id}/audio` -- **not in the documented endpoint
  table** (section 8.5), added because turn-based voice needs the system to *speak* each
  question, and the doc's own `AIProvider.synthesize_speech` only makes sense with a route
  that calls it. Synthesizes on demand every call; nothing is ever written to disk, per
  "minimize stored voice data" (section 6).
- `POST /interviews/{id}/answer/audio` (previously a 501 stub) now genuinely transcribes an
  uploaded recording and persists it as the answer. `InterviewAnswer.audio_url` stays
  `null` by design -- the raw recording is transcribed in memory and discarded, never
  persisted, again per "minimize stored voice data". This is a documented deviation from
  literally storing `audio_url`.
- `GET .../evaluation`: for `mode="voice"`, `grammar` is `null` (doc labels it text-mode)
  and `filler_word_rate` is computed deterministically from the transcript (word-count
  heuristic over common filler phrases -- not AI-judged, matching the codebase's "our own
  gap engine" philosophy). `speaking_pace` stays `null` for V1 -- it would need audio
  duration, which isn't available since raw audio is never persisted; a real fix would
  measure duration transiently at transcribe time without persisting the audio, deferred
  for now.
- **Real bug found and fixed via live testing, not present in earlier stages' scope**: the
  new `POST /answer/audio` route is `async def` (needed for `await file.read()`), and an
  `async def` route runs directly on the event loop -- FastAPI only auto-threadpools plain
  `def` routes. Calling `transcribe()` (a slow, blocking, synchronous call) directly inside
  it froze the *entire server*, not just that request, confirmed by a concurrent `/health`
  call timing out during a live transcription. Fixed with `starlette.concurrency.
  run_in_threadpool`. **Update:** the identical pre-existing bug in `resumes.py`'s
  `upload_resume` flagged here as out-of-scope for Stage 10 was fixed in the whole-codebase
  audit above -- see that section instead of treating this as still-open.
- Verified live end-to-end against real models (not mocks): synthesized a real question to
  WAV, fed that same audio back through the transcribe endpoint (a genuine TTS→STT round
  trip), and got back a near-verbatim transcript of the original question text. Completed a
  full voice interview lifecycle through evaluation; the AI evaluator correctly caught and
  penalized repeated/irrelevant answers with accurate, specific feedback rather than
  rubber-stamping.
- **First-run note**: `faster-whisper` downloads its model from Hugging Face on first use
  (~75MB for `tiny.en`). Hugging Face's xet-accelerated download path hung indefinitely in
  dev testing; `_get_whisper_model()` now sets `HF_HUB_DISABLE_XET=1` automatically before
  first import to force the reliable plain-HTTPS fallback.

Stage 9 — Text Interview Engine: the interview lifecycle (`POST /interviews`,
`GET .../next-question`, `POST .../answer`, `GET .../evaluation`) in
`app/services/interview_service.py`:
- `POST /interviews` starts an `Interview` of `type` (`technical`/`hr`/`project`) for the
  caller's profile. `mode` defaults to `"text"`. `jd_id` is optional; when given, its
  `detected_role_id` supplies role context, otherwise the profile's `target_role_id` is
  used (see `_build_context`).
- `GET .../next-question` generates one `InterviewQuestion` at a time (fixed at 5 per
  interview) via `interview_question_v1.txt` (schema `GeneratedInterviewQuestion`),
  prompted with the candidate's resume skills, listed `Project` rows, JD text, and every
  question already asked (so it never repeats itself). `type="project"` questions probe the
  candidate's own listed projects; `"technical"` probes role/JD-relevant concepts;
  `"hr"` is behavioral. A question with no `InterviewAnswer` row yet is "the pending
  question" -- calling next-question again before answering returns it unchanged, the same
  pattern as the assessment lifecycle but keyed off row absence instead of a nullable
  column, since there's no need to pre-reserve an interview answer slot.
- `POST .../answer` persists the answer text (a unique constraint on
  `interview_question_id` blocks answering twice -- 409). No per-question score exists in
  the doc's schema; scoring happens once, for the whole transcript, at evaluation time.
- `POST .../answer/audio` exists (matching the documented API surface) but always returns
  501 -- Stage 10 work.
- `GET .../evaluation` requires the interview to be `completed` (409 otherwise). Builds the
  full Q&A transcript and asks the AI to score it holistically in one call
  (`interview_evaluation_v1.txt` / `GeneratedInterviewEvaluation`): technical_correctness,
  relevance, completeness, reasoning, structure, clarity, grammar, role_jd_coverage,
  overall_score, plus written feedback -- all 0.0-1.0. `speaking_pace`/`filler_word_rate`
  are voice-only fields, set to `None` server-side rather than asked of the model in text
  mode. Idempotent -- a second call returns the persisted row.
- Verified live against `qwen3:8b`: a `type="project"` interview against a real listed
  project produced five genuinely project-specific questions (referencing the project's
  name and tech stack, no two alike) and a coherent final evaluation with sensible scores
  and actionable feedback.

Stage 8 — Role/JD Tests + adaptive difficulty: the test-taking lifecycle
(`POST /assessments`, `GET .../next-question`, `POST .../answer`, `GET .../result`) in
`app/services/assessment_run_service.py`:
- `POST /assessments` starts an `Assessment` from a previously generated blueprint (owned by
  the caller's profile), status `in_progress`.
- `GET .../next-question` walks the blueprint's sections in priority order (already sorted by
  gap/weight). For each skill it AI-generates one `Question` at a time via
  `question_generator_v1.txt` (schema `GeneratedQuestion` — skill_tag/difficulty/source are
  set server-side, never AI-generated, same pattern as `AIParsedJD` excluding
  `detected_role_id`). A `QuestionAttempt` row is created immediately with
  `student_answer=None`, which doubles as "the pending question" — calling next-question again
  before answering returns the same question instead of generating a new one.
- **Adaptive difficulty**: a skill's first question starts at `medium`; each subsequent
  question in that skill escalates one level (easy→medium→hard) after a correct answer and
  de-escalates after an incorrect one, derived purely from prior `QuestionAttempt` rows (no
  extra state column needed). Verified live: a correct `medium` DSA answer produced a `hard`
  follow-up question.
- `POST .../answer` grades against the pending attempt. MCQ is graded deterministically
  (exact match against `expected_answer_or_rubric` — no AI call). Every other type is graded
  by the AI via `answer_grader_v1.txt` (schema `AnswerGrade`), rubric-based, 0.0–1.0 score.
  `QuestionRead` deliberately omits `expected_answer_or_rubric`/`explanation` so the answer key
  never reaches the client before they respond.
- `GET .../result` requires every section to be answered (`Assessment.status == "completed"`,
  set automatically once the last section's question count is met) — else 409. Computes and
  persists `AssessmentResult`: per-skill average score, overall average, and
  `priority_weaknesses` (skills scoring below 0.6, worst first). Idempotent — a second call
  returns the persisted row rather than recomputing.

Stage 7 — Prepare Me Engine: `POST /assessments/blueprint`, the signature "Prepare
Me" trigger. Deterministic (non-AI) skill-matrix + blueprint generation in
`app/services/assessment_service.py`:
- Base skill matrix comes from the seeded `RoleSkillWeight` taxonomy for the given/detected role.
- If a JD is provided, its extracted skills override/merge into the matrix (`source` becomes
  `"jd_override"`; a JD-only skill not in the base taxonomy is added with a default
  `assessment_type="technical"`, since we don't know its true category).
- Each section's `question_count` and priority ordering account for whether the skill is
  already covered by the profile's resume (a "gap" skill gets more weight and more questions).
- `RoleSkillWeight` is seeded for all 11 starter roles (`alembic/versions/*_seed_default_role_skill_weights.py`)
  — only SDE/Data Analyst/Data Scientist/Data Engineer skill lists come directly from
  `PROJECT_HANDOFF.md` section 5; the other 7 roles are a reasonable first pass, easily
  adjusted later (the doc calls these "starting points, not fixed").
- "project" and "HR" from the doc's informal role skill lists are deliberately excluded from
  the seeded weights — those map to interview stages (Stage 9/10), not testable skills.

Fixed a real bug while adding tests: the global validation-error handler (Stage 3) crashed
with an unrelated 500 whenever a Pydantic `model_validator` raised a plain `ValueError` --
the resulting error dict's `ctx.error` held a raw exception object, which the default JSON
encoder can't serialize. Now wrapped in `jsonable_encoder`.

Also complete: FastAPI app, SQLAlchemy + Alembic-managed SQLite DB, JWT auth, student profile +
role selection + sub-resources, resume pipeline (PDF → AI-structured analysis), JD intelligence
(parse/role-detect/match), Prepare Me blueprint generation, role/JD test-taking with adaptive
difficulty, text + voice interviews, global error handling. Not yet built: weakness engine,
roadmap (Stage 11).

**Known schema deviations from `PROJECT_HANDOFF.md`** (documented where they occur):
- `SkillGap.skill_name` (string) instead of `skill_id` (FK) — there's no global skill catalog
  table, only per-profile `Skill` entries (see Stage 4 notes), so gaps are keyed by name,
  matching how `JobMatch.matching_skills`/`missing_skills` are also name-based.
- `RoleSkillWeight.skill` (string) instead of `skill_id` (FK) — same reasoning, and it matches
  the doc's own Pydantic schema (section 8.7) more closely than its prose entity list does.

**Security note:** `JWT_SECRET_KEY` in `.env.example` is a dev-only placeholder. It must be overridden with a real secret via `.env` (never committed) before any deployment beyond local development.
