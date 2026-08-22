"""seed starter role taxonomy

Revision ID: cbb4b6e88773
Revises: 4d6c4f6fc68c
Create Date: 2026-08-22 20:05:11.733334

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cbb4b6e88773'
down_revision: Union[str, None] = '4d6c4f6fc68c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Starter taxonomy per PROJECT_HANDOFF.md section 5 (Role-Based Assessment Engine).
# Configurable/extensible — this is a seed, not a fixed list.
ROLES = [
    {"name": "SDE", "category": "Engineering", "description": "Aptitude, DSA, coding, OOP, DBMS/SQL, OS, networks."},
    {"name": "Data Analyst", "category": "Data", "description": "SQL, Excel, statistics, Power BI/Tableau, Python, analytics case studies."},
    {"name": "Data Scientist", "category": "Data", "description": "Statistics, probability, Python, SQL, ML, model evaluation, case studies."},
    {"name": "Data Engineer", "category": "Data", "description": "SQL, Python, ETL, data warehousing, Spark, cloud, pipelines."},
    {"name": "Business Analyst", "category": "Business", "description": "Requirements analysis, SQL, data interpretation, stakeholder communication."},
    {"name": "ML Engineer", "category": "Data", "description": "Python, ML systems, model deployment, data pipelines, statistics."},
    {"name": "Web Developer", "category": "Engineering", "description": "Frontend/backend web fundamentals, APIs, databases."},
    {"name": "DevOps Engineer", "category": "Engineering", "description": "CI/CD, cloud infrastructure, containers, automation."},
    {"name": "Cloud Engineer", "category": "Engineering", "description": "Cloud platforms, networking, infrastructure as code."},
    {"name": "QA/Test Engineer", "category": "Engineering", "description": "Testing fundamentals, test design, automation basics."},
    {"name": "Cybersecurity", "category": "Security", "description": "Security fundamentals, networks, common vulnerabilities."},
]

roles_table = sa.table(
    "roles",
    sa.column("name", sa.String),
    sa.column("category", sa.String),
    sa.column("description", sa.Text),
)


def upgrade() -> None:
    op.bulk_insert(roles_table, ROLES)


def downgrade() -> None:
    conn = op.get_bind()
    names = [role["name"] for role in ROLES]
    conn.execute(roles_table.delete().where(roles_table.c.name.in_(names)))
