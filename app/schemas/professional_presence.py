"""Response models for the connection endpoints.

There is no token field anywhere in this file, and that absence is the security property rather
than an oversight. A schema with nowhere to put a credential cannot leak one through a later
`model_config = ConfigDict(from_attributes=True)` picking up a new column, or through somebody
adding a debug field. The test suite asserts it too, but the schema is what makes it structural.
"""

import datetime
from typing import Optional

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


class AuthorizationStartRead(BaseModel):
    """What the app needs to open the system browser, and nothing else.

    `authorization_url` carries the state as a query parameter, so it is treated as a credential
    in transit: returned to the authenticated student who requested it, never logged, and never
    written anywhere.
    """

    authorization_url: str
    expires_in_seconds: int
