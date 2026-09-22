"""allow ungraded interview evaluations

Lets an InterviewEvaluation represent "we tried to grade this and could not" without inventing
a number. The eight score columns below were NOT NULL, which left the Interview Engine two
options when the AI grader failed: write a fabricated score, or write nothing at all. The first
is a lie the student's readiness would then be computed from; the second loses the fact that
grading was attempted and leaves the client with nothing to explain.

Recruiter Attack already resolved the identical problem this way -- its score columns are
nullable and `used_fallback` is *derived* from their NULL-ness, so the two can never disagree.
This brings the Interview Engine to the same semantics rather than inventing a second one.

Only the score columns change. feedback_text stays NOT NULL (an ungraded evaluation still
carries an explanation), and grammar/speaking_pace/filler_word_rate were already nullable for
their own mode-specific reasons. No other table is touched.

Revision ID: 0d6496be2213
Revises: e8691c85f409
Create Date: 2026-09-21

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0d6496be2213"
down_revision: Union[str, Sequence[str], None] = "e8691c85f409"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The eight dimensions that carry a grade. grammar/speaking_pace/filler_word_rate are
# deliberately absent -- they are already nullable because they are mode-specific, not because
# of grading failure, and conflating the two reasons would make both harder to reason about.
_SCORE_COLUMNS = (
    "technical_correctness",
    "relevance",
    "completeness",
    "reasoning",
    "structure",
    "clarity",
    "role_jd_coverage",
    "overall_score",
)


def upgrade() -> None:
    # batch_alter_table for the same reason every other column change in this project uses it:
    # SQLite has no ALTER COLUMN, so batch mode rebuilds the table, while on Postgres it is a
    # thin wrapper over a plain ALTER. existing_type is required for the rebuild to reproduce
    # the column faithfully.
    with op.batch_alter_table("interview_evaluations") as batch_op:
        for column in _SCORE_COLUMNS:
            batch_op.alter_column(column, existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    """Restore NOT NULL.

    Ungraded rows cannot exist under the old schema -- that is precisely what this migration
    added -- so they are removed first, or the ALTER would fail against any database where one
    had been written. This deletes ONLY rows whose overall_score IS NULL, i.e. rows that carry
    no measurement of any kind: just a note saying grading did not complete. Removing one
    returns that interview to "not evaluated yet", which the old schema represents perfectly
    well, and the next request for its evaluation regenerates it. No graded row is touched, and
    no score is altered.
    """
    op.execute(sa.text("DELETE FROM interview_evaluations WHERE overall_score IS NULL"))
    with op.batch_alter_table("interview_evaluations") as batch_op:
        for column in _SCORE_COLUMNS:
            batch_op.alter_column(column, existing_type=sa.Float(), nullable=False)
