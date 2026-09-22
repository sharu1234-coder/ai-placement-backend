"""Full Recruiter Simulation -- a deterministic orchestration layer over the existing
Interview Engine, not a second interview engine.

interview_service.py remains the canonical owner of the interview lifecycle: it still creates
the Interview row, generates and persists every InterviewQuestion, accepts every answer,
decides completion, produces the one InterviewEvaluation, and feeds Interview Memory. A
simulation is an ordinary `interviews` row whose `type` happens to be "full_simulation"; it
uses the same tables, the same endpoints, the same evaluation dimensions, the same weakness
namespace and the same output guards as every other interview.

What this package adds is the two things the Interview Engine genuinely does not have:

    stages.py    a fixed 12-question plan, and the pure mapping from a question's order_index
                 to the stage it belongs to
    context.py   the bounded, read-only bundle of real student facts a recruiter-style
                 question is allowed to be grounded in

The division of authority is the whole design, and it is deliberately one-directional: the
AI chooses the *wording* of a question, and code chooses everything else -- which stage we are
in, how many questions that stage gets, how many remain, and when the interview is over. None
of that is asked of the model, none of it is sent by the client, and none of it is stored as
mutable state that could drift. Stage is a pure function of (interview type, order_index), so
two calls can never disagree about it and there is nothing to migrate.
"""
