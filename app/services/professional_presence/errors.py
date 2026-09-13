"""Provider failures, reduced to a fixed vocabulary before they leave the adapter layer.

The rule this file enforces is that a provider's own words never travel. GitHub's error bodies
can echo back a request URL, a repository name, an organisation, or a fragment of the token
that failed -- and whatever reaches here gets persisted to `provider_connections.last_error_kind`
and rendered to the student. A fixed set of categories cannot leak any of that, and it is also
the only thing the UI ever branches on, so carrying the free text would be storing a liability
in exchange for nothing.

`kind` values are exactly the ERROR_KINDS constants on the ProviderConnection model, so a
category can be written to the column without a translation table in between that could drift.

The distinction between `unavailable` and "no evidence" is the one that matters most to the
product. A provider being down must never be rendered as "this student has no GitHub evidence"
-- that is a claim about the student, made from a fact about our infrastructure. Keeping the
failure categorised all the way to the surface is what lets the UI say "GitHub could not be
reached" instead.
"""

from app.models.provider_connection import (
    ERROR_FORBIDDEN,
    ERROR_MALFORMED,
    ERROR_NOT_FOUND,
    ERROR_RATE_LIMIT,
    ERROR_TIMEOUT,
    ERROR_UNAUTHORIZED,
    ERROR_UNAVAILABLE,
)


class ProviderError(Exception):
    """A provider call failed, reduced to a category.

    The message is written by us and is safe to show a student. It is never the provider's own
    error text, and nothing constructed here interpolates a response body, a URL, a token or a
    code into the message.
    """

    kind: str = ERROR_UNAVAILABLE

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.default_message)

    @property
    def default_message(self) -> str:
        return "GitHub could not be reached right now. Please try again."


class ProviderUnauthorized(ProviderError):
    """The credential was rejected (401), or the authorization code could not be exchanged.

    Covers a token the student revoked on GitHub's own settings page, which is a normal thing
    for them to do and must be reported as "reconnect", not as an error.
    """

    kind = ERROR_UNAUTHORIZED

    @property
    def default_message(self) -> str:
        return "GitHub did not accept the authorisation. Please try connecting again."


class ProviderForbidden(ProviderError):
    kind = ERROR_FORBIDDEN

    @property
    def default_message(self) -> str:
        return "GitHub refused this request."


class ProviderNotFound(ProviderError):
    kind = ERROR_NOT_FOUND

    @property
    def default_message(self) -> str:
        return "GitHub could not find that account."


class ProviderRateLimited(ProviderError):
    """Distinct from `forbidden` even though GitHub signals both with 403.

    They are told apart by the rate-limit headers, not by the body, because the body is text we
    have promised not to read into our own state. The difference is worth keeping: a rate limit
    resolves on its own and the student should be told to wait, while a genuine refusal will not
    resolve no matter how long they wait.
    """

    kind = ERROR_RATE_LIMIT

    @property
    def default_message(self) -> str:
        return "GitHub's rate limit was reached. Please try again in a few minutes."


class ProviderTimeout(ProviderError):
    kind = ERROR_TIMEOUT

    @property
    def default_message(self) -> str:
        return "GitHub did not respond in time. Please try again."


class ProviderUnavailable(ProviderError):
    kind = ERROR_UNAVAILABLE


class ProviderMalformedResponse(ProviderError):
    """A 200 whose body was not what the API contract says it is.

    Kept separate from `unavailable` because it means something changed on the provider's side
    rather than that the provider is down, and the two need different responses from us: one is
    waited out, the other is a bug report.
    """

    kind = ERROR_MALFORMED

    @property
    def default_message(self) -> str:
        return "GitHub returned a response we could not read. Please try again."
