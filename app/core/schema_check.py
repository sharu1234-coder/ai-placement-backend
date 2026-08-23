from pathlib import Path
from typing import Optional

from alembic.config import Config
from alembic.script import ScriptDirectory

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"


def get_expected_head_revision() -> str:
    """The migration revision GET /health/ready expects the database to be at -- read from
    the alembic/versions/ scripts bundled in *this* deployment (i.e. this codebase's own
    migration history), not from the database itself. Used to catch the common "deployed new
    code but forgot to run `alembic upgrade head`" failure mode -- the app starts fine and
    /health passes, but it's running against a schema older than the code expects."""
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def is_schema_current(current_revision: Optional[str], expected_revision: str) -> bool:
    """current_revision is None when the alembic_version table doesn't exist at all --
    treated as "can't tell, don't block" rather than "out of date". This matters because
    this project's own test databases are created directly from SQLAlchemy metadata
    (tests/conftest.py), bypassing Alembic entirely -- they never have an alembic_version
    table, and that's expected, not a real readiness failure."""
    return current_revision is None or current_revision == expected_revision
