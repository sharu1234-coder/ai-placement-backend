"""A request schema must never accept a string its own column cannot store.

The failure this prevents is invisible on SQLite and a 500 on PostgreSQL. SQLite does not
enforce VARCHAR length -- it stores whatever you give it -- so a schema that allows 300
characters into a String(255) column passes every test on the development database. On
PostgreSQL the same request reaches the driver, which raises StringDataRightTruncation, and
because nothing catches a DataError the student gets a 500 rather than a 422 naming the field
to shorten.

That is not hypothetical: `ProjectCreate.tech_stack` allowed 300 (a cap copied from
`ProjectBuild.stack_summary`, whose column really is String(300)) while `Project.tech_stack`
is String(255), and four more schemas had no cap at all. It survived because the only
database the suite had ever run against could not tell the difference.

Checking a boundary value per field through the API would need a request per field and would
still only cover the fields someone remembered to write a test for. This walks the mapping
instead, so a new field, or a column narrowed in a migration, is caught without anyone
choosing to look.
"""

import pytest
from pydantic import BaseModel

from app.models.certification import Certification
from app.models.education import Education
from app.models.experience import Experience
from app.models.profile import StudentProfile
from app.models.project import Project
from app.models.skill import Skill
from app.schemas.profile import StudentProfileUpsert
from app.schemas.profile_items import (
    CertificationCreate,
    EducationCreate,
    ExperienceCreate,
    ProjectCreate,
    SkillCreate,
)

# Request schema -> the model it is written into. Explicit rather than inferred: a guessed
# pairing that silently stopped matching would turn this file into a test that checks nothing.
SCHEMA_TO_MODEL = [
    (EducationCreate, Education),
    (SkillCreate, Skill),
    (ProjectCreate, Project),
    (ExperienceCreate, Experience),
    (CertificationCreate, Certification),
    (StudentProfileUpsert, StudentProfile),
]


def _column_caps(model) -> dict:
    """{column name: VARCHAR length} for the bounded string columns only.

    Text columns are deliberately absent: they have no storage limit, so a schema cap on one is
    an AI-input or abuse limit and is none of this file's business.
    """
    caps = {}
    for column in model.__table__.columns:
        length = getattr(column.type, "length", None)
        if length and column.type.__class__.__name__ in ("String", "VARCHAR"):
            caps[column.name] = length
    return caps


def _schema_max_length(field) -> int | None:
    for entry in field.metadata:
        if getattr(entry, "max_length", None) is not None:
            return entry.max_length
    return None


def _string_fields(schema: type[BaseModel]):
    for name, field in schema.model_fields.items():
        if "str" in str(field.annotation):
            yield name, field


@pytest.mark.parametrize(
    "schema,model", SCHEMA_TO_MODEL, ids=[s.__name__ for s, _ in SCHEMA_TO_MODEL]
)
def test_no_string_field_can_exceed_its_column(schema, model) -> None:
    caps = _column_caps(model)
    problems = []

    for name, field in _string_fields(schema):
        if name not in caps:
            continue  # Text column, or not persisted on this model at all.
        column_cap = caps[name]
        schema_cap = _schema_max_length(field)

        if schema_cap is None:
            problems.append(
                f"{schema.__name__}.{name} has no max_length but "
                f"{model.__name__}.{name} is String({column_cap}) -- any longer value is a "
                f"500 on PostgreSQL"
            )
        elif schema_cap > column_cap:
            problems.append(
                f"{schema.__name__}.{name} allows {schema_cap} but "
                f"{model.__name__}.{name} holds {column_cap} -- values in "
                f"{column_cap + 1}..{schema_cap} are a 500 on PostgreSQL"
            )

    assert not problems, "\n".join(problems)


def test_the_mapping_actually_covers_something() -> None:
    """Guards the guard. If a rename made every field fall through the `not in caps` skip
    above, each test would pass while checking nothing at all."""
    checked = sum(
        1
        for schema, model in SCHEMA_TO_MODEL
        for name, _ in _string_fields(schema)
        if name in _column_caps(model)
    )
    assert checked >= 12, f"only {checked} schema fields matched a bounded column"
