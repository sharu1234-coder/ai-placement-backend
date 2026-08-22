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

Stage 5 complete — Resume pipeline: `POST /resumes` (PDF upload → PyMuPDF text extraction →
AI-structured parsing → persisted `Resume`/`ResumeAnalysis`) and `GET /resumes/{id}/analysis`.
Structured AI output is validated against `ParsedResume` (Pydantic), with one retry on
validation failure before a graceful `parse_status="failed"` (upload still succeeds — the
student sees the file was received even if parsing didn't work). Also complete: FastAPI app,
SQLAlchemy + Alembic-managed SQLite DB, JWT auth, student profile + role selection + sub-resources,
global error handling. Not yet built: JD intelligence, Prepare Me engine, tests/interviews, roadmap.

**Security note:** `JWT_SECRET_KEY` in `.env.example` is a dev-only placeholder. It must be overridden with a real secret via `.env` (never committed) before any deployment beyond local development.
