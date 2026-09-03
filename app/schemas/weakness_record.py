import datetime

from pydantic import BaseModel


class WeaknessRecordRead(BaseModel):
    skill_key: str
    status: str  # NEW | REPEATED | IMPROVING | NEARLY_CLOSED | CLOSED
    occurrences: int
    consecutive_good_signals: int
    last_score: float
    last_seen_at: datetime.datetime

    model_config = {"from_attributes": True}
