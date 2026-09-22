"""Deterministic questions for when AI question generation fails mid-simulation.

A reliability mechanism, and only that. It exists so a twelve-question interview does not die
at question seven because the local model timed out, and for no other reason: it is never used
after a weak answer, never used to adapt, and never used when generation succeeded. The stage
planner still decides which stage we are in and how many questions remain; this module only
answers "what do I ask for this stage if the model cannot tell me".

Three properties matter and all three come from being a static table:

  Deterministic  -- (stage, order_index) picks the question, so the same simulation state always
                    produces the same question. No randomness, no clock, no model.
  Free           -- no AI call, no network, no database read. The whole module is a dict.
  Safe           -- these are application-owned strings, so there is nothing for an attacker to
                    influence and nothing for output_guard to check. That is also why they are
                    written out in full here rather than assembled from student data.

The wording is deliberately conservative: every question asks the candidate to tell us about
their own experience, and not one of them asserts that the candidate has any. A fallback fires
precisely when we have lost the ability to ground a question in their real claims, so this is
the moment it would be easiest to invent one -- "tell me about the Kafka pipeline you built"
reads naturally and would be a fabrication. "Walk me through a project you've worked on" gets
the same interview signal and claims nothing.
"""

from typing import Dict, Tuple

from app.services.interview_simulation.stages import (
    STAGE_BACKGROUND,
    STAGE_BEHAVIORAL,
    STAGE_CANDIDATE_Q,
    STAGE_INTRO,
    STAGE_PROJECT,
    STAGE_RESUME,
    STAGE_TECHNICAL,
)

# (question_text, based_on) per stage. `based_on` is the same short tag an AI-generated question
# carries; these are written to name what the question is actually rooted in, so a transcript
# never implies a grounding the question did not have.
#
# A stage gets as many entries as it has questions in the plan, so a simulation that loses the
# AI for a whole stage does not ask the same thing twice. CLOSING is absent on purpose -- it is
# not a question stage.
_FALLBACK_QUESTIONS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    STAGE_INTRO: (
        (
            "To get us started -- could you briefly walk me through your background, and what "
            "you're looking for in your next role?",
            "Introduction",
        ),
    ),
    STAGE_BACKGROUND: (
        (
            "What led you towards this field in the first place?",
            "Motivation",
        ),
        (
            "Where would you like your career to go over the next couple of years, and how does "
            "this kind of role fit into that?",
            "Career direction",
        ),
    ),
    STAGE_RESUME: (
        (
            "Tell me about one thing on your CV you're genuinely proud of. What was your part "
            "in it?",
            "Experience",
        ),
        (
            "Which of the skills you've listed would you say you're strongest in, and what have "
            "you actually used it for?",
            "Skills",
        ),
    ),
    STAGE_PROJECT: (
        (
            "Walk me through something you've built. What problem was it meant to solve?",
            "Project overview",
        ),
        (
            "On that project -- what was the hardest part, and how did you work through it?",
            "Project challenges",
        ),
    ),
    STAGE_TECHNICAL: (
        (
            "Talk me through a technical decision you've made and why you chose that approach "
            "over the alternatives.",
            "Technical decision",
        ),
        (
            "How do you go about working out why something isn't behaving the way you expected?",
            "Problem solving",
        ),
        (
            "Pick something you've worked with recently and explain how it works, as if I hadn't "
            "come across it before.",
            "Technical depth",
        ),
    ),
    STAGE_BEHAVIORAL: (
        (
            "Tell me about a time something went wrong while you were working with other people. "
            "What did you do?",
            "Working with others",
        ),
    ),
    STAGE_CANDIDATE_Q: (
        (
            "That's everything from me. What would you like to ask about the role or the team?",
            "Candidate questions",
        ),
    ),
}


class NoFallbackForStageError(LookupError):
    """Asked for a fallback question for a stage that has none (i.e. CLOSING).

    Raised rather than silently returning something generic: reaching this means the caller
    believes it is generating a question in a stage the plan says has none, which is a bug in
    the caller and must surface as one.
    """


def question_for(stage: str, order_index: int) -> Tuple[str, str]:
    """The (question_text, based_on) to use for `stage` at question `order_index`.

    `order_index` rather than a per-stage counter because it is what the caller already has, is
    already the thing stage is derived from, and is stable for a given question forever -- so a
    resumed session that regenerates the same position produces the same question.
    """
    bank = _FALLBACK_QUESTIONS.get(stage)
    if not bank:
        raise NoFallbackForStageError(f"No fallback question is defined for stage {stage!r}")
    return bank[(order_index - 1) % len(bank)]
