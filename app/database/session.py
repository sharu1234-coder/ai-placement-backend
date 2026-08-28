from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

_is_sqlite = settings.database_url.startswith("sqlite")
connect_args = {"check_same_thread": False} if _is_sqlite else {}
# pool_pre_ping: a managed Postgres instance (RDS, Cloud SQL, etc.) can silently drop idle
# connections; without this, the first query on a stale connection fails outright instead
# of transparently reconnecting. No-op for SQLite (single file, no real "connection drop").
#
# pool_size/max_overflow: explicit rather than left at SQLAlchemy's QueuePool default
# (5 + 10 overflow = 15 max concurrent connections), which is undersized relative to
# Starlette's default sync-route threadpool (up to 40 workers) -- under real concurrent
# load, requests would queue for a DB connection well before they'd queue for a worker
# thread. SQLite doesn't use QueuePool (a single file has no real notion of a connection
# pool), so these kwargs are Postgres-only -- passing them to SQLite's pool class raises.
pool_kwargs = {} if _is_sqlite else {"pool_size": 10, "max_overflow": 20}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True, **pool_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
