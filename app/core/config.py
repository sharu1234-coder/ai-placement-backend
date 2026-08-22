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

    upload_dir: str = "uploads"


settings = Settings()
