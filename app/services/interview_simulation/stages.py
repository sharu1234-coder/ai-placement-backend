"""The deterministic stage plan. Pure functions over an integer -- no database, no AI, no state.

A recruiter interview moves through recognisable phases, and the product promise is that the
student experiences one continuous conversation rather than eight separate mini-interviews.
Both of those are served by the same decision: the sequence is fixed in code, so the model is
never asked where it is or what comes next. It only ever writes one question for a stage it is
told it is in.

Stage is derived from the question's existing `order_index` rather than stored in a new column.
That is not only a migration saved -- a derived stage cannot drift from the questions it
describes, cannot be edited by a client, and reads the same on a session resumed three days
later as it did when the question was written. The mapping is only valid because each stage's
question budget is FIXED: if a follow-up could extend a stage, order_index would stop implying
stage and the column would become necessary. Keeping the budget fixed is also the better
interview -- one that silently grows because you answered badly is not realistic.
"""

from typing import Optional, Tuple

# The `interviews.type` value. A plain string in a String(20) column with no CHECK constraint
# and no enum (see migration fb53dcc7e15a), which is why a new interview type costs a Pydantic
# Literal entry and nothing else.
SIMULATION_TYPE = "full_simulation"

STAGE_INTRO = "INTRO"
STAGE_BACKGROUND = "BACKGROUND"
STAGE_RESUME = "RESUME"
STAGE_PROJECT = "PROJECT"
STAGE_TECHNICAL = "TECHNICAL"
STAGE_BEHAVIORAL = "BEHAVIORAL"
STAGE_CANDIDATE_Q = "CANDIDATE_Q"
# Not a question stage: there is no 13th question. This is what the interview reports once
# every question has been answered, so the client has something truthful to show on the way to
# the evaluation instead of a blank.
STAGE_CLOSING = "CLOSING"

# Index 0 is question 1. The length of this tuple IS the simulation's question count -- there
# is no second place where "12" is written down, so the plan and the total cannot disagree.
_STAGE_PLAN: Tuple[str, ...] = (
    STAGE_INTRO,  # 1
    STAGE_BACKGROUND,  # 2
    STAGE_BACKGROUND,  # 3
    STAGE_RESUME,  # 4
    STAGE_RESUME,  # 5
    STAGE_PROJECT,  # 6
    STAGE_PROJECT,  # 7
    STAGE_TECHNICAL,  # 8
    STAGE_TECHNICAL,  # 9
    STAGE_TECHNICAL,  # 10
    STAGE_BEHAVIORAL,  # 11
    STAGE_CANDIDATE_Q,  # 12
)

SIMULATION_QUESTION_COUNT = len(_STAGE_PLAN)

# What the student is shown. Never the enum itself -- "CANDIDATE_Q" is an implementation
# detail, and the point of the simulation is that it reads as a conversation.
_STAGE_LABELS = {
    STAGE_INTRO: "Introduction",
    STAGE_BACKGROUND: "Background",
    STAGE_RESUME: "Your experience",
    STAGE_PROJECT: "Your projects",
    STAGE_TECHNICAL: "Technical",
    STAGE_BEHAVIORAL: "Working with others",
    STAGE_CANDIDATE_Q: "Your questions",
    STAGE_CLOSING: "Wrapping up",
}

# What the recruiter is trying to find out in each stage. This is the only stage-dependent
# thing that reaches the prompt, and it is authored here rather than generated: the model
# decides how to ask, never what the stage is for.
_STAGE_INTENTS = {
    STAGE_INTRO: (
        "Open the conversation. Ask the candidate to introduce themselves and say what draws "
        "them to this kind of work. Warm and straightforward -- this is the first thing said "
        "in the interview, not a test."
    ),
    STAGE_BACKGROUND: (
        "Explore their background: education, how they got into this field, what they are "
        "aiming for, and why this role fits. Judgement and motivation, not technical depth."
    ),
    STAGE_RESUME: (
        "Probe something the candidate has actually claimed about their experience or skills, "
        "using only the claims listed in the context. Ask them to expand on it -- what they "
        "actually did, how deep it goes. Never attribute experience they have not claimed."
    ),
    STAGE_PROJECT: (
        "Go deep on one of their real projects: what they personally owned, the design or "
        "technical decisions they made and why, what went wrong, and what the outcome was. "
        "Only projects listed in the context exist."
    ),
    STAGE_TECHNICAL: (
        "Test genuine technical understanding relevant to the target role -- concepts, "
        "reasoning, implementation, trade-offs. A realistic working problem, not memorised "
        "trivia, and answerable in a few sentences."
    ),
    STAGE_BEHAVIORAL: (
        "Ask about a specific past situation involving other people -- collaboration, "
        "disagreement, a failure, or taking ownership. Phrase it so a strong answer naturally "
        "tells a story with a result."
    ),
    STAGE_CANDIDATE_Q: (
        "Close your own questioning and invite the candidate to ask you something about the "
        "role, the team or the work. Make it a genuine invitation, not another test."
    ),
}


def is_simulation(interview_type: str) -> bool:
    return interview_type == SIMULATION_TYPE


def stage_for(order_index: int) -> str:
    """The stage of question `order_index` (1-based, matching InterviewQuestion.order_index).

    Anything past the plan is CLOSING rather than an error: the natural caller is "what stage
    are we in now", asked once the last question has been answered, and the honest answer then
    is that the interview is wrapping up. Out-of-range low values are clamped to the opening
    stage for the same reason -- this is a display-and-prompt helper, and no caller benefits
    from it raising.
    """
    if order_index < 1:
        return STAGE_INTRO
    if order_index > SIMULATION_QUESTION_COUNT:
        return STAGE_CLOSING
    return _STAGE_PLAN[order_index - 1]


def stage_label(stage: str) -> str:
    return _STAGE_LABELS.get(stage, stage.replace("_", " ").capitalize())


def stage_intent(stage: str) -> str:
    return _STAGE_INTENTS.get(stage, _STAGE_INTENTS[STAGE_TECHNICAL])


def questions_in_stage(stage: str) -> int:
    return sum(1 for s in _STAGE_PLAN if s == stage)


def position_in_stage(order_index: int) -> Optional[Tuple[int, int]]:
    """(which question of this stage, how many the stage has) -- e.g. (2, 3) for question 9.

    Lets the prompt say "this is your second of three technical questions", which is what stops
    the model from opening every technical question as though it were the first.
    """
    if order_index < 1 or order_index > SIMULATION_QUESTION_COUNT:
        return None
    stage = stage_for(order_index)
    seen = sum(1 for i in range(1, order_index + 1) if _STAGE_PLAN[i - 1] == stage)
    return seen, questions_in_stage(stage)
