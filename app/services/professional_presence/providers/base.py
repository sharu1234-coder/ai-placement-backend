"""The contract every external provider implements, and the only shape the rest of the app sees.

This is the seam that keeps `if github:` out of the intelligence layer. Career Strategy, the
Evidence Engine and the API all deal in the three structures below; only the adapter under this
package knows that GitHub calls its account identifier `id`, returns scopes in a comma-separated
header-ish string, or signals rate limiting with a 403 rather than a 429.

Adding LinkedIn, Kaggle or a portfolio site later means writing one class here. It does not mean
touching the connection service, the router, or anything downstream -- which is the property the
specification asks for and the reason this file exists before there are two providers to justify
it in hindsight.
"""

import datetime
from dataclasses import dataclass
from typing import Optional, Protocol, Tuple


@dataclass(frozen=True)
class ProviderCredential:
    """What a successful code exchange yields.

    `granted_scopes` is what the provider actually gave, which is not necessarily what was
    asked for. For GitHub under the minimum-permission choice it is the empty string, and that
    is a real value rather than a missing one -- "we deliberately requested nothing" has to stay
    distinguishable from "we never recorded what we requested".
    """

    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[datetime.datetime] = None
    granted_scopes: str = ""


@dataclass(frozen=True)
class ProviderIdentity:
    """Who the credential belongs to, on the provider's side.

    `provider_user_id` is the provider's immutable account identifier and is the only thing
    ownership is ever keyed on. `username` is for display: a GitHub login can be renamed by its
    owner, after which the old name becomes available for someone else to register, so keying on
    it would let a connection silently follow whoever holds the name today.
    """

    provider_user_id: str
    username: Optional[str] = None


@dataclass(frozen=True)
class ProviderRepository:
    """One repository, reduced to the facts this product can honestly use.

    Everything here is either measured by the provider or typed by the repository's owner, and
    which of the two it is decides how much weight it can carry downstream -- so the two are
    kept in separate fields rather than merged into one "skills" list:

    `primary_language` and `languages` come from GitHub's own analysis of the bytes of code in
    the repository. The student did not type them anywhere.

    `topics` are labels the owner typed onto their own repository. Real context, and still an
    assertion: anyone can add the topic "kubernetes" to an empty repository in seconds.

    Deliberately absent: README text, commit counts and any quality score. Mining prose for
    skill names is the "invent skills from arbitrary filenames" failure the specification rules
    out, and there is no honest number to put in a score -- a repository is not a graded
    assessment.
    """

    full_name: str
    name: str
    description: Optional[str]
    primary_language: Optional[str]
    languages: Tuple[str, ...]
    topics: Tuple[str, ...]
    is_fork: bool
    is_archived: bool
    stars: int
    # When the code was last actually touched, NOT when we looked. This is what the Evidence
    # Engine's staleness window reads, so it has to mean "this work is two years old".
    pushed_at: Optional[datetime.datetime]


@dataclass(frozen=True)
class ProviderProfile:
    """Account-level facts, and the completeness signals an audit can be built from.

    `provider_user_id` is repeated here so the sync path can confirm the account it is reading
    is still the account that was connected. A token that starts returning a different id is not
    a refresh, it is a different grant, and continuing would attribute one person's repositories
    to another person's profile.
    """

    provider_user_id: str
    username: Optional[str]
    name: Optional[str]
    bio: Optional[str]
    company: Optional[str]
    location: Optional[str]
    blog: Optional[str]
    public_repos: int
    followers: int


@dataclass(frozen=True)
class ProviderFetch:
    """One complete read of a provider account.

    `complete` records whether the repository listing was exhausted or cut off at the page cap.
    It exists because reconciliation depends on it: evidence for a repository that has vanished
    may only be removed when we are certain we saw the whole list, otherwise a student with more
    repositories than the cap would have real evidence deleted on every sync.
    """

    profile: ProviderProfile
    repositories: Tuple[ProviderRepository, ...]
    complete: bool


class ProviderAdapter(Protocol):
    """A Protocol rather than an ABC: the adapters are stateless and are swapped wholesale in
    tests, so structural typing is all that is needed and there is no shared implementation
    worth inheriting."""

    key: str

    def is_configured(self) -> bool:
        """Whether this provider can be used at all in this deployment.

        Checked before an authorization URL is offered, so an unconfigured deployment says
        "not available" on a screen the student has not acted on yet -- rather than after they
        have already authorised on the provider's own site and been redirected back.
        """
        ...

    def build_authorization_url(self, state: str, code_challenge: str) -> str:
        ...

    def exchange_code(self, code: str, code_verifier: str) -> ProviderCredential:
        ...

    def fetch_identity(self, access_token: str) -> ProviderIdentity:
        ...

    def fetch_account(self, access_token: str) -> "ProviderFetch":
        """Read the account's public professional surface in one call sequence.

        Returns normalised structures, never the provider's raw payload: raw responses retain
        far more about the student than the product needs, age badly as provider schemas change,
        and put unreviewed third-party text into our database.
        """
        ...

    def revoke(self, access_token: str) -> None:
        """Best effort, by design. Disconnection must still destroy our copy of the credential
        even when the provider cannot be reached -- otherwise a provider outage would leave a
        student unable to disconnect, which is the wrong way for that failure to fall."""
        ...
