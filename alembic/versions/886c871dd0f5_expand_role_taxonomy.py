"""expand role taxonomy

Revision ID: 886c871dd0f5
Revises: ec4080de08f6
Create Date: 2026-08-25 23:07:26.408130

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '886c871dd0f5'
down_revision: Union[str, None] = 'ec4080de08f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Extends the 11-role starter taxonomy (cbb4b6e88773) per the redesign spec's much
# larger target-role list (section 7/39) -- purely additive, nothing renamed or
# removed so existing target_role_id FKs stay valid. Deduped by exact name against the
# starter 11. "Other" is deliberately NOT seeded here -- it's handled client-side via
# StudentProfile.custom_target_role (Phase 2 API), not a catalog row.
ROLES = [
    {"name": "Software Developer", "category": "Engineering", "description": "General-purpose software engineering across the stack."},
    {"name": "Frontend Developer", "category": "Engineering", "description": "UI engineering, browser fundamentals, JS/TS frameworks."},
    {"name": "Backend Developer", "category": "Engineering", "description": "APIs, databases, server-side systems design."},
    {"name": "Full Stack Developer", "category": "Engineering", "description": "Frontend and backend engineering combined."},
    {"name": "Mobile App Developer", "category": "Engineering", "description": "Mobile app fundamentals across platforms."},
    {"name": "Android Developer", "category": "Engineering", "description": "Kotlin/Java, Android SDK, mobile architecture."},
    {"name": "iOS Developer", "category": "Engineering", "description": "Swift, iOS SDK, mobile architecture."},
    {"name": "Flutter Developer", "category": "Engineering", "description": "Dart, Flutter, cross-platform mobile UI."},
    {"name": "React Native Developer", "category": "Engineering", "description": "JS/TS, React Native, cross-platform mobile UI."},
    {"name": "Java Developer", "category": "Engineering", "description": "Java fundamentals, OOP, JVM ecosystem."},
    {"name": "Python Developer", "category": "Engineering", "description": "Python fundamentals, common frameworks and tooling."},
    {"name": "C++ Developer", "category": "Engineering", "description": "C++ fundamentals, memory management, performance."},
    {"name": "Site Reliability Engineer", "category": "Engineering", "description": "Reliability, monitoring, incident response, infrastructure."},
    {"name": "Machine Learning Engineer", "category": "Data", "description": "ML systems, model training and deployment, Python."},
    {"name": "AI Engineer", "category": "Data", "description": "Applied AI systems, model integration, Python."},
    {"name": "NLP Engineer", "category": "Data", "description": "Natural language processing, ML, Python."},
    {"name": "Computer Vision Engineer", "category": "Data", "description": "Computer vision, ML, Python."},
    {"name": "Product Analyst", "category": "Product", "description": "Product metrics, SQL, data-informed decision-making."},
    {"name": "Product Manager", "category": "Product", "description": "Product strategy, requirements, stakeholder communication."},
    {"name": "Technical Product Manager", "category": "Product", "description": "Product management with strong technical depth."},
    {"name": "UI/UX Designer", "category": "Design", "description": "Interface design, usability, design systems."},
    {"name": "UX Researcher", "category": "Design", "description": "User research methods, usability testing, synthesis."},
    {"name": "Cyber Security Analyst", "category": "Security", "description": "Security monitoring, threat analysis, incident response."},
    {"name": "Cyber Security Engineer", "category": "Security", "description": "Security engineering, hardening, tooling."},
    {"name": "Security Engineer", "category": "Security", "description": "Application and infrastructure security engineering."},
    {"name": "Cloud Security Engineer", "category": "Security", "description": "Cloud platform security, IAM, compliance."},
    {"name": "Network Engineer", "category": "Engineering", "description": "Networking fundamentals, protocols, infrastructure."},
    {"name": "QA Engineer", "category": "Testing", "description": "Test design, manual and exploratory testing."},
    {"name": "Test Engineer", "category": "Testing", "description": "Test planning, execution, defect tracking."},
    {"name": "Automation Test Engineer", "category": "Testing", "description": "Test automation frameworks and scripting."},
    {"name": "SDET", "category": "Testing", "description": "Software development in test, automation, CI integration."},
    {"name": "Database Administrator", "category": "Engineering", "description": "Database operations, tuning, backup/recovery."},
    {"name": "Database Engineer", "category": "Engineering", "description": "Database design, SQL, data modeling."},
    {"name": "System Administrator", "category": "Engineering", "description": "Systems administration, Linux, infrastructure."},
    {"name": "Solutions Architect", "category": "Engineering", "description": "System design, architecture trade-offs, cloud platforms."},
    {"name": "Software Architect", "category": "Engineering", "description": "Software system design, architecture patterns."},
    {"name": "Embedded Systems Engineer", "category": "Core Engineering", "description": "Embedded C/C++, microcontrollers, real-time systems."},
    {"name": "Firmware Engineer", "category": "Core Engineering", "description": "Firmware development, embedded C, hardware interfaces."},
    {"name": "IoT Engineer", "category": "Core Engineering", "description": "IoT protocols, embedded systems, connected devices."},
    {"name": "VLSI Engineer", "category": "Core Engineering", "description": "Digital design, VLSI fundamentals, hardware description languages."},
    {"name": "Electronics Engineer", "category": "Core Engineering", "description": "Electronics fundamentals, circuit design."},
    {"name": "Electrical Engineer", "category": "Core Engineering", "description": "Electrical fundamentals, power systems, circuits."},
    {"name": "Mechanical Engineer", "category": "Core Engineering", "description": "Engineering mathematics, thermodynamics, machine design."},
    {"name": "Civil Engineer", "category": "Core Engineering", "description": "Structural fundamentals, construction, surveying."},
    {"name": "Chemical Engineer", "category": "Core Engineering", "description": "Process engineering, thermodynamics, chemical fundamentals."},
    {"name": "Biotechnology Engineer", "category": "Core Engineering", "description": "Biotechnology fundamentals, lab techniques."},
    {"name": "Biomedical Engineer", "category": "Core Engineering", "description": "Biomedical systems, medical devices."},
    {"name": "Robotics Engineer", "category": "Core Engineering", "description": "Robotics fundamentals, controls, embedded systems."},
    {"name": "Automotive Engineer", "category": "Core Engineering", "description": "Automotive systems, mechanical/electrical fundamentals."},
    {"name": "Aerospace Engineer", "category": "Core Engineering", "description": "Aerospace fundamentals, fluid mechanics, structures."},
    {"name": "Core Engineering", "category": "Core Engineering", "description": "General core-engineering placement preparation."},
    {"name": "Consulting", "category": "Business", "description": "Case studies, structured problem-solving, communication."},
    {"name": "Finance", "category": "Business", "description": "Financial fundamentals, valuation, analytics."},
    {"name": "Investment Banking", "category": "Business", "description": "Finance fundamentals, valuation, markets."},
    {"name": "Risk Analyst", "category": "Business", "description": "Risk analysis, statistics, financial fundamentals."},
    {"name": "Operations Analyst", "category": "Business", "description": "Operations analysis, process improvement, data."},
    {"name": "Supply Chain Analyst", "category": "Business", "description": "Supply chain fundamentals, logistics, data analysis."},
    {"name": "Marketing Analyst", "category": "Business", "description": "Marketing fundamentals, analytics, communication."},
    {"name": "HR Analyst", "category": "Business", "description": "HR fundamentals, people analytics, communication."},
    {"name": "Sales Engineer", "category": "Business", "description": "Technical sales, product knowledge, communication."},
    {"name": "Technical Support Engineer", "category": "Business", "description": "Troubleshooting, product knowledge, communication."},
    {"name": "Technical Writer", "category": "Business", "description": "Technical communication, documentation."},
    {"name": "Research Engineer", "category": "Research", "description": "Applied research, experimentation, technical depth."},
    {"name": "Research Scientist", "category": "Research", "description": "Research methodology, technical depth, publications."},
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
