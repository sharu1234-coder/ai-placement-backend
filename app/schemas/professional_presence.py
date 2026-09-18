"""Response models for the connection endpoints.

There is no token field anywhere in this file, and that absence is the security property rather
than an oversight. A schema with nowhere to put a credential cannot leak one through a later
`model_config = ConfigDict(from_attributes=True)` picking up a new column, or through somebody
adding a debug field. The test suite asserts it too, but the schema is what makes it structural.
"""

import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict


class ConnectionStatusRead(BaseModel):
    """One provider's connection state, as the app sees it.

    Returned for providers that are NOT connected as well, with `connected=False`. The browser
    flow can end with no callback at all -- the student closes the tab, switches app, loses
    signal -- so the app has to be able to ask the server what happened, and "no row" has to be
    a real answer rather than an omission the client must interpret.
    """

    model_config = ConfigDict(from_attributes=True)

    provider: str
    connected: bool
    username: Optional[str] = None
    status: Optional[str] = None
    sync_status: Optional[str] = None
    last_synced_at: Optional[datetime.datetime] = None
    # A category from ProviderConnection.ERROR_KINDS, never a provider message. See
    # services/professional_presence/errors.py for why the free text never travels.
    last_error_kind: Optional[str] = None
    connected_at: Optional[datetime.datetime] = None


class SyncResultRead(BaseModel):
    """What one sync did. Counts only -- never the repositories themselves.

    `complete` is False when the repository listing hit its page cap, and it is surfaced rather
    than hidden because it changes what the numbers mean: an incomplete listing is a floor, not
    a total, and reconciliation deliberately removes nothing when it is False.
    """

    provider: str
    evidence_added: int
    evidence_removed: int
    repositories_seen: int
    snapshot_written: bool
    complete: bool


class AuditFindingRead(BaseModel):
    """One observation, its proof, and what to do about it.

    `evidence` is never generated -- it is assembled in Python from the stored snapshot, so a
    student can always trace a recommendation back to the fact that produced it. `why_it_matters`
    is the only optional field: it is the generated half, and it is absent rather than invented
    when the model failed or said nothing usable about this finding.
    """

    code: str
    kind: str  # strength | gap | opportunity
    subject: Optional[str] = None
    evidence: str
    why_it_matters: Optional[str] = None
    recommended_action: str
    is_template: bool


class PresenceAuditRead(BaseModel):
    """The Professional Presence screen's whole payload for one provider.

    Carries the connection's state as well as the findings so the client never has to combine
    two responses to decide what to render -- and, more importantly, so "we could not reach
    GitHub" can never be displayed as "you have no evidence". `sync_status` and `last_error_kind`
    are what distinguish those two, and they travel with the data rather than beside it.

    `freshness` is derived from `last_synced_at` on the server so that every client renders the
    same words for the same age, rather than each inventing its own threshold.
    """

    provider: str
    connected: bool
    username: Optional[str] = None
    status: Optional[str] = None
    sync_status: Optional[str] = None
    last_synced_at: Optional[datetime.datetime] = None
    last_error_kind: Optional[str] = None
    # never | fresh | aging | stale -- see audit router for the thresholds.
    freshness: str
    # When the snapshot these findings describe was captured. Distinct from last_synced_at: a
    # sync that found nothing changed advances the sync time and not this one, and showing both
    # is what lets the screen say "checked today, unchanged since March" honestly.
    snapshot_captured_at: Optional[datetime.datetime] = None
    # False when the repository listing hit its page cap, so the findings describe a subset.
    listing_complete: bool = True
    findings: List[AuditFindingRead] = []
    # Which prompt produced the generated prose, or None when every finding is a template.
    narrative_prompt_version: Optional[str] = None


class PresenceHistoryItemRead(BaseModel):
    """One past state of the account, for answering "what changed?".

    Deliberately a summary rather than the whole stored payload: the snapshot holds repository
    descriptions and topics, and replaying all of it to the client on a history screen would
    ship far more third-party text than the question needs.
    """

    sequence: int
    captured_at: datetime.datetime
    public_repositories: int
    described_repositories: int
    languages: List[str]


class AuthorizationStartRead(BaseModel):
    """What the app needs to open the system browser, and nothing else.

    `authorization_url` carries the state as a query parameter, so it is treated as a credential
    in transit: returned to the authenticated student who requested it, never logged, and never
    written anywhere.
    """

    authorization_url: str
    expires_in_seconds: int
