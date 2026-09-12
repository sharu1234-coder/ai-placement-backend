from pathlib import Path
from typing import Optional

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.config import settings

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
    """Whether the database's schema matches what this deployment's code expects.

    A missing alembic_version table (current_revision is None) means two completely different
    things depending on where we are, so it is answered differently in each.

    Outside production it is "can't tell, don't block": this project's own test databases are
    created directly from SQLAlchemy metadata (tests/conftest.py), bypassing Alembic entirely,
    so they never have an alembic_version table and that is expected rather than a fault.

    In production nothing creates tables from metadata -- Alembic is the only thing that ever
    builds the schema. A missing alembic_version table there has exactly one cause: migrations
    have never been run. Treating that as "can't tell" made readiness answer 200 on a freshly
    deployed service with no schema at all, so the platform would route traffic to it and every
    request would fail on a missing relation -- which is precisely the "deployed the code but
    forgot `alembic upgrade head`" case this check exists to catch, slipping through in its
    most severe form because the schema was not merely stale but absent."""
    if current_revision is None:
        return settings.environment == "development"
    return current_revision == expected_revision
