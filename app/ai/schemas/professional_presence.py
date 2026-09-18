from typing import List, Optional

from pydantic import BaseModel, Field


class GeneratedObservationText(BaseModel):
    """Prose for ONE observation the deterministic layer already established.

    `code` and `subject` are echoes, not inputs: the service looks each entry up against the
    observations it computed itself and discards anything that does not match a real one. The
    model therefore cannot introduce an observation, only phrase one -- which is what keeps a
    fluent sentence from becoming a fact about the student.

    There is no field for a count, a score, a percentage or a probability. The numbers a student
    sees all come from the snapshot via the observation's own evidence sentence, so there is
    nowhere in this schema for a generated figure to enter.
    """

    code: str
    subject: Optional[str] = None
    why_it_matters: str = Field(max_length=300)
    recommended_action: str = Field(max_length=300)


class GeneratedPresenceNarrative(BaseModel):
    """The model's contribution to a presence audit, and the whole of it.

    Defaulted rather than required so a provider that returns the wrong shape, or omits a
    section it had nothing to say about, still yields a usable audit built from the
    deterministic half instead of failing validation and losing everything.
    """

    items: List[GeneratedObservationText] = Field(default_factory=list)
