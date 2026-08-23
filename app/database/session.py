from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
# pool_pre_ping: a managed Postgres instance (RDS, Cloud SQL, etc.) can silently drop idle
# connections; without this, the first query on a stale connection fails outright instead
# of transparently reconnecting. No-op for SQLite (single file, no real "connection drop").
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
