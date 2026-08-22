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

## Status

Stage 3 complete — FastAPI app, SQLAlchemy + Alembic-managed SQLite DB, JWT auth (`/auth/signup`, `/auth/login`, `/auth/refresh`), and global error handling (clean validation-error responses, no leaked stack traces on unhandled exceptions). No feature business logic yet (profile, resume, JD, etc.).

**Security note:** `JWT_SECRET_KEY` in `.env.example` is a dev-only placeholder. It must be overridden with a real secret via `.env` (never committed) before any deployment beyond local development.
