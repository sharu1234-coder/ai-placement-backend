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

Visit `http://127.0.0.1:8000/health` to confirm the app is running.

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

## Status

Stage 6 complete — JD Intelligence: `POST /jobs` (paste JD text → AI-structured parsing →
role detection → persisted `JobDescription`/`Requirement` rows), `GET /jobs/{id}/requirements`,
and `POST /jobs/{id}/match` (deterministic, non-AI skill matching against the profile's latest
completed resume analysis — see `app/services/matching_service.py` — producing `JobMatch` +
`SkillGap` rows with an explainable weighted score). Role detection is a case-insensitive
substring match against the seeded `Role` catalog (the AI has no knowledge of our internal
role ids); a smarter match can replace this heuristic later without changing the API contract.

Also complete: FastAPI app, SQLAlchemy + Alembic-managed SQLite DB, JWT auth, student profile +
role selection + sub-resources, resume pipeline (PDF → AI-structured analysis), global error
handling. Not yet built: Prepare Me engine, tests/interviews, weakness engine, roadmap.

**Known schema deviations from `PROJECT_HANDOFF.md`** (documented where they occur):
`SkillGap.skill_name` (string) instead of `skill_id` (FK) — there's no global skill catalog
table, only per-profile `Skill` entries (see Stage 4 notes), so gaps are keyed by name, matching
how `JobMatch.matching_skills`/`missing_skills` are also name-based.

**Security note:** `JWT_SECRET_KEY` in `.env.example` is a dev-only placeholder. It must be overridden with a real secret via `.env` (never committed) before any deployment beyond local development.
