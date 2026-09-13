"""GitHub OAuth: authorization URL, code exchange, identity, revocation.

Scope is deliberately absent from the authorization URL. GitHub's documentation states that a
token with no scope at all "grants read-only access to public information (including user
profile info, repository info, and gists)", which is the entire V1 requirement. Sending
`scope=` or omitting it are equivalent to GitHub; omitting it is clearer about the intent.

What is NOT requested, and why it is worth naming: `public_repo` reads as a public-repositories
scope and is read/write -- it grants write access to code, commit statuses, projects,
collaborators and deployments on every public repository the student owns. A tool that only
needs to read never has cause to hold it, and a student reviewing the consent screen should not
be asked to grant it.

PKCE is used even though this is a confidential client with a secret. GitHub accepts S256 and
rejects `plain`. The secret already protects the exchange, so PKCE here is defence in depth
against a leaked authorization code -- a code lifted from a redirect chain or a proxy log is
worthless without a verifier that never left this server.

Nothing in this module logs a code, a token, a verifier, a state, or an authorization URL. The
URL is excluded specifically because it carries the state as a query parameter, which makes an
"authorization URL" line in a log a credential in a log.
"""

import logging
from typing import Optional
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.models.provider_connection import PROVIDER_GITHUB
from app.services.professional_presence.errors import (
    ProviderError,
    ProviderForbidden,
    ProviderMalformedResponse,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnauthorized,
    ProviderUnavailable,
)
from app.services.professional_presence.providers.base import ProviderCredential, ProviderIdentity

logger = logging.getLogger(__name__)

_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"
_USER_URL = "https://api.github.com/user"
_API_BASE = "https://api.github.com"

# Matches the timeout the existing public-repo checker already uses, so the two GitHub paths in
# this codebase behave the same way under a slow provider.
_TIMEOUT = 10.0


def _raise_for_status(response: httpx.Response) -> None:
    """Map an HTTP status onto a category. Never reads the body.

    403 is split by headers rather than by body text. GitHub uses 403 for both "you are rate
    limited" and "you may not do this", and the body is the only place it says which in prose --
    prose this module has promised not to interpret or store. `x-ratelimit-remaining: 0` is a
    structured, documented signal that says the same thing without the liability.
    """
    status = response.status_code
    if status < 400:
        return
    if status == 401:
        raise ProviderUnauthorized()
    if status == 429:
        raise ProviderRateLimited()
    if status == 403:
        if response.headers.get("x-ratelimit-remaining") == "0":
            raise ProviderRateLimited()
        raise ProviderForbidden()
    if status == 404:
        raise ProviderNotFound()
    if status >= 500:
        raise ProviderUnavailable()
    raise ProviderError()


def _json_or_malformed(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        raise ProviderMalformedResponse()
    if not isinstance(payload, dict):
        # A list or a bare string where an object is documented is the same class of failure as
        # unparseable bytes: the contract is not what we were told it is.
        raise ProviderMalformedResponse()
    return payload


class GitHubOAuthProvider:
    """Stateless. Reads configuration at call time rather than at construction so that a test
    (or a settings reload) never finds an instance pinned to stale credentials."""

    key = PROVIDER_GITHUB

    def is_configured(self) -> bool:
        return bool(settings.github_client_id and settings.github_client_secret)

    def build_authorization_url(self, state: str, code_challenge: str) -> str:
        params = {
            "client_id": settings.github_client_id,
            "redirect_uri": settings.github_oauth_redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            # S256 only. GitHub rejects `plain`, and hardcoding it here means no caller can
            # downgrade the exchange by passing a weaker method through.
            "code_challenge_method": "S256",
        }
        # No `scope`: see the module docstring. An empty scope is the minimum permission that
        # still satisfies V1, and it is expressed by omission rather than by an empty string so
        # that nobody later "fixes" a blank-looking parameter by filling it in.
        return f"{_AUTHORIZE_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, code_verifier: str) -> ProviderCredential:
        try:
            response = httpx.post(
                _TOKEN_URL,
                data={
                    "client_id": settings.github_client_id,
                    "client_secret": settings.github_client_secret,
                    "code": code,
                    "redirect_uri": settings.github_oauth_redirect_uri,
                    "code_verifier": code_verifier,
                },
                # Without this GitHub answers in form-encoding, and the error path below would
                # never parse -- so a failed exchange would surface as "malformed" regardless of
                # what actually went wrong.
                headers={"Accept": "application/json"},
                timeout=_TIMEOUT,
            )
        except httpx.TimeoutException:
            raise ProviderTimeout()
        except httpx.HTTPError:
            raise ProviderUnavailable()

        _raise_for_status(response)
        payload = _json_or_malformed(response)

        if payload.get("error"):
            # GitHub answers a failed exchange with HTTP 200 and an `error` key -- a bad or
            # already-used code, an expired code, a verifier that does not match the challenge.
            # Every one of those means "this authorisation did not complete", so they collapse
            # to unauthorized. The error string itself is not logged or stored: it is provider
            # text, and the category is all the product needs.
            raise ProviderUnauthorized()

        access_token = payload.get("access_token")
        if not access_token or not isinstance(access_token, str):
            raise ProviderMalformedResponse()

        return ProviderCredential(
            access_token=access_token,
            # GitHub OAuth App user tokens do not expire and carry no refresh token unless the
            # app opts into expiring tokens, which this one does not. Read defensively anyway so
            # that turning that on later is a configuration change rather than a code change.
            refresh_token=payload.get("refresh_token") or None,
            granted_scopes=payload.get("scope") or "",
        )

    def fetch_identity(self, access_token: str) -> ProviderIdentity:
        try:
            response = httpx.get(
                _USER_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=_TIMEOUT,
            )
        except httpx.TimeoutException:
            raise ProviderTimeout()
        except httpx.HTTPError:
            raise ProviderUnavailable()

        _raise_for_status(response)
        payload = _json_or_malformed(response)

        raw_id = payload.get("id")
        # GitHub's account id is a number. Anything else -- absent, null, a string, an object --
        # means this is not the response documented, and guessing would mean binding a
        # connection to an identity we cannot actually vouch for.
        if not isinstance(raw_id, int):
            raise ProviderMalformedResponse()

        login = payload.get("login")
        return ProviderIdentity(
            provider_user_id=str(raw_id),
            username=login if isinstance(login, str) else None,
        )

    def revoke(self, access_token: str) -> None:
        """DELETE /applications/{client_id}/grant -- removes the grant and, with it, every token
        issued to this app for that user.

        Best effort on purpose. A student pressing Disconnect must end up disconnected even if
        GitHub is unreachable; our copy of the token is destroyed either way by the caller. The
        failure is swallowed here rather than raised so that a provider outage cannot leave
        somebody permanently unable to disconnect.
        """
        if not self.is_configured():
            return
        try:
            httpx.request(
                "DELETE",
                f"{_API_BASE}/applications/{settings.github_client_id}/grant",
                auth=(settings.github_client_id, settings.github_client_secret),
                json={"access_token": access_token},
                headers={"Accept": "application/vnd.github+json"},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError:
            logger.info("provider=%s event=revoke_failed reason=unavailable", self.key)


_PROVIDERS = {PROVIDER_GITHUB: GitHubOAuthProvider()}


def get_provider(key: str) -> Optional[object]:
    """Lookup by provider key. Returns None for anything not wired up, so a bad path parameter
    becomes a 404 at the router rather than an AttributeError deeper in."""
    return _PROVIDERS.get(key)
