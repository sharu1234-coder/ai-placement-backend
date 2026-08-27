from typing import List

from pydantic import BaseModel, Field


class GeneratedTopicExplanation(BaseModel):
    """One topic's "why it matters" text. topic_key must echo one of the keys the prompt
    supplied verbatim -- the service layer validates this and never trusts an unrecognized
    or missing key (falls back to a deterministic template sentence instead)."""

    topic_key: str
    why_it_matters: str = Field(max_length=400)


class GeneratedPlacementPathExplanations(BaseModel):
    explanations: List[GeneratedTopicExplanation]
