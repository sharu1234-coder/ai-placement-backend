"""seed default role skill weights

Revision ID: 6043737942f3
Revises: d36dd7ceee4e
Create Date: 2026-08-22 22:23:15.851015

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6043737942f3'
down_revision: Union[str, None] = 'd36dd7ceee4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (skill, weight, assessment_type) per role. assessment_type must be one of
# aptitude/technical/sql/coding/case_study/statistics/tool (RoleSkillWeight schema,
# PROJECT_HANDOFF.md section 8.7). "project" and "HR" from the doc's informal role lists
# are deliberately excluded here -- those are interview stages (Stage 9/10), not
# testable skills for the assessment blueprint.
#
# SDE / Data Analyst / Data Scientist / Data Engineer skill lists are taken directly from
# PROJECT_HANDOFF.md section 5. The remaining 7 roles have no skill breakdown in the doc
# ("starting points, not fixed" / "keep role weights configurable") -- these are a
# reasonable first pass, easily adjusted later via this same table.
ROLE_SKILLS = {
    "SDE": [
        ("Aptitude", 0.5, "aptitude"),
        ("DSA", 1.0, "coding"),
        ("OOP", 0.7, "technical"),
        ("DBMS", 0.7, "sql"),
        ("OS", 0.5, "technical"),
        ("Networks", 0.5, "technical"),
    ],
    "Data Analyst": [
        ("SQL", 1.0, "sql"),
        ("Excel", 0.7, "tool"),
        ("Statistics", 0.8, "statistics"),
        ("Power BI", 0.6, "tool"),
        ("Python", 0.7, "coding"),
        ("Analytics case studies", 0.8, "case_study"),
    ],
    "Data Scientist": [
        ("Statistics", 0.9, "statistics"),
        ("Probability", 0.7, "statistics"),
        ("Python", 0.9, "coding"),
        ("SQL", 0.7, "sql"),
        ("Machine Learning", 1.0, "technical"),
        ("Model evaluation", 0.7, "technical"),
        ("Case studies", 0.8, "case_study"),
    ],
    "Data Engineer": [
        ("SQL", 0.9, "sql"),
        ("Python", 0.8, "coding"),
        ("ETL", 0.8, "technical"),
        ("Data warehousing", 0.7, "technical"),
        ("Spark", 0.6, "technical"),
        ("Cloud", 0.6, "technical"),
        ("Pipelines", 0.6, "technical"),
    ],
    "Business Analyst": [
        ("Requirements analysis", 0.8, "technical"),
        ("SQL", 0.7, "sql"),
        ("Data interpretation", 0.8, "case_study"),
        ("Aptitude", 0.5, "aptitude"),
    ],
    "ML Engineer": [
        ("Python", 0.9, "coding"),
        ("ML systems", 1.0, "technical"),
        ("Statistics", 0.7, "statistics"),
        ("SQL", 0.6, "sql"),
        ("Model deployment", 0.7, "technical"),
        ("Case studies", 0.6, "case_study"),
    ],
    "Web Developer": [
        ("JavaScript", 0.9, "coding"),
        ("HTML/CSS", 0.7, "technical"),
        ("APIs", 0.7, "technical"),
        ("Databases", 0.6, "sql"),
        ("Aptitude", 0.4, "aptitude"),
        ("DSA", 0.6, "coding"),
    ],
    "DevOps Engineer": [
        ("CI/CD", 0.8, "technical"),
        ("Linux", 0.7, "technical"),
        ("Scripting", 0.7, "coding"),
        ("Cloud", 0.8, "technical"),
        ("Containers", 0.7, "technical"),
        ("Networking", 0.5, "technical"),
    ],
    "Cloud Engineer": [
        ("Cloud platforms", 0.9, "technical"),
        ("Networking", 0.7, "technical"),
        ("Infrastructure as code", 0.8, "technical"),
        ("Security basics", 0.6, "technical"),
        ("Linux", 0.5, "technical"),
    ],
    "QA/Test Engineer": [
        ("Testing fundamentals", 0.9, "technical"),
        ("Test design", 0.7, "case_study"),
        ("Automation basics", 0.6, "coding"),
        ("SQL", 0.5, "sql"),
        ("Aptitude", 0.4, "aptitude"),
    ],
    "Cybersecurity": [
        ("Security fundamentals", 0.9, "technical"),
        ("Networks", 0.7, "technical"),
        ("Common vulnerabilities", 0.8, "technical"),
        ("Aptitude", 0.4, "aptitude"),
        ("Scripting", 0.5, "coding"),
    ],
}

roles_table = sa.table("roles", sa.column("id", sa.Integer), sa.column("name", sa.String))
weights_table = sa.table(
    "role_skill_weights",
    sa.column("role_id", sa.Integer),
    sa.column("skill", sa.String),
    sa.column("weight", sa.Float),
    sa.column("assessment_type", sa.String),
)


def upgrade() -> None:
    conn = op.get_bind()
    role_ids = {
        name: role_id
        for role_id, name in conn.execute(sa.select(roles_table.c.id, roles_table.c.name))
    }

    rows = []
    for role_name, skills in ROLE_SKILLS.items():
        role_id = role_ids.get(role_name)
        if role_id is None:
            continue
        for skill, weight, assessment_type in skills:
            rows.append(
                {"role_id": role_id, "skill": skill, "weight": weight, "assessment_type": assessment_type}
            )

    if rows:
        op.bulk_insert(weights_table, rows)


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(weights_table.delete())
