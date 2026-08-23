from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "AI Placement OS Backend"
    environment: str = "development"
    debug: bool = True

    database_url: str = "sqlite:///./app.db"

    # Dev-only default. Must be overridden via JWT_SECRET_KEY in .env before any real deployment.
    jwt_secret_key: str = "dev-only-insecure-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    ai_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    # A hardcoded 120s was too tight for larger structured-output calls (e.g. a multi-day
    # roadmap's task list) on modest dev hardware -- found via a live smoke test, not a
    # guess. Override via OLLAMA_TIMEOUT_SECONDS in .env if a given deployment needs more.
    ollama_timeout_seconds: int = 240

    # Production cloud provider (AI_PROVIDER=anthropic). Never hardcode a real key here --
    # this default is None so a misconfigured deployment fails loudly at first use rather
    # than silently trying an empty string. claude-sonnet-5 is the documented pick for
    # high-volume production workloads (vs. claude-opus-5's higher per-token cost) --
    # override via ANTHROPIC_MODEL in .env per-deployment if a different quality/cost
    # tradeoff is wanted (e.g. Opus for interview evaluation specifically, Haiku for cheap
    # extraction) -- see README "Production AI provider" for the full tradeoff.
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-sonnet-5"

    # Dev-only local STT for OllamaProvider.transcribe -- swap for a cloud STT provider
    # before real user testing (see CloudProvider note in README).
    whisper_model_size: str = "tiny.en"

    upload_dir: str = "uploads"


settings = Settings()
