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

Stage 7 complete — Prepare Me Engine: `POST /assessments/blueprint`, the signature "Prepare
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
(parse/role-detect/match), global error handling. Not yet built: the actual test-taking
lifecycle (Question generation, adaptive difficulty -- Stage 8), interviews, weakness engine,
roadmap.

**Known schema deviations from `PROJECT_HANDOFF.md`** (documented where they occur):
- `SkillGap.skill_name` (string) instead of `skill_id` (FK) — there's no global skill catalog
  table, only per-profile `Skill` entries (see Stage 4 notes), so gaps are keyed by name,
  matching how `JobMatch.matching_skills`/`missing_skills` are also name-based.
- `RoleSkillWeight.skill` (string) instead of `skill_id` (FK) — same reasoning, and it matches
  the doc's own Pydantic schema (section 8.7) more closely than its prose entity list does.

**Security note:** `JWT_SECRET_KEY` in `.env.example` is a dev-only placeholder. It must be overridden with a real secret via `.env` (never committed) before any deployment beyond local development.
