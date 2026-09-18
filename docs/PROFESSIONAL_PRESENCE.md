# Professional Presence

How a student's authorised external accounts become evidence, what this feature deliberately
cannot do, and what has to be configured before any of it works.

---

## 1. What it is

A student connects an external account. The server reads what that account makes legitimately
available, turns it into evidence rows the Evidence Engine already knows how to weigh, and
produces an audit of how their public presence reads to a recruiter.

The feature spans three concerns that are kept separate on purpose:

| Concern | Where it lives |
|---|---|
| Authorisation (OAuth 2.0 + PKCE, state, tokens) | `app/services/professional_presence/connection_service.py`, `oauth_state.py` |
| Reading a provider and normalising it | `providers/github.py` behind the `ProviderAdapter` protocol in `providers/base.py` |
| Turning stored data into evidence and findings | `sync_service.py`, `audit_service.py`, `app/services/evidence_engine.py` |

Nothing provider-specific reaches the intelligence layer. The adapter is the only module that
knows GitHub calls its account identifier `id` or signals rate limiting with a 403.

---

## 2. Providers

### GitHub — supported

**Scope requested: none at all.** The authorization URL carries no `scope` parameter. GitHub's
documentation states that a token with no scope *"Grants read-only access to public information
(including user profile info, repository info, and gists)"*, which is the entire requirement.

`public_repo` is **not** requested and must not be added casually: despite the name it is
read/**write**, granting write access to code, commit statuses, projects, collaborators and
deployments on every public repository the student owns. A test asserts the authorization URL
never contains it, alongside `repo`, `write:`, `delete_repo` and `admin:`.

What is read, and from where:

| Data | Endpoint | Nature |
|---|---|---|
| Profile identity, name, bio, company, location, blog, public repo count | `GET /user` | Typed by the student |
| Owned public repositories | `GET /user/repos?visibility=public&affiliation=owner` | Provider-listed |
| Per-repository language breakdown | `GET /repos/{owner}/{repo}/languages` | **Measured by GitHub from the bytes of code** |
| Repository topics | included in the listing | Typed by the repository owner |

`affiliation=owner` excludes repositories the student merely collaborates on or that an
organisation owns. This feature attributes work to one student, and "has push access to" is not
"wrote".

**What GitHub could tell us and we deliberately do not ask:**

- **Contribution / activity history.** `contributionsCollection` requires the `read:user` scope.
  Requesting a broader scope to obtain a nice-to-have violates the minimum-permission rule this
  integration was built on, so the feature says nothing about commit frequency or streaks rather
  than estimating them. This is a deliberate, documented absence, not an oversight.
- **Private repositories.** An empty scope cannot see them, and the feature never claims to.
- **README or code content.** Mining prose for skill names invents skills from arbitrary text.
  Only measured languages and typed topics are recorded.

### LinkedIn — not available, and not faked

`PROVIDER_LINKEDIN` exists as a constant and the schema supports it, but **there is no LinkedIn
integration and shipping one would be dishonest.**

LinkedIn's official self-serve product is *Sign In with LinkedIn using OpenID Connect*. Its
scopes are exactly `openid`, `profile`, `email`, and `GET /v2/userinfo` returns exactly:

```
sub, name, given_name, family_name, picture, locale, email, email_verified
```

**No skills. No positions or work experience. No education. No headline.** Those require
approval under LinkedIn's Partner Program, which this application does not hold. LinkedIn's own
documentation further states that Sign In with LinkedIn *"does not verify user identities and
should not be marketed as such"*.

A compliant integration would therefore yield a name, a picture and an email — all three of
which the app already holds from the student's own profile — and produce a "Connected" badge
carrying zero career evidence. The Android screen says so plainly instead, and offers no Connect
button. **Do not** add one by scraping LinkedIn HTML, by asking for a LinkedIn password, or by
inventing scopes; all three are prohibited and the first two also breach LinkedIn's terms.

If a student's LinkedIn information is ever accepted, it is a **claim**, not an observation, and
must not be stored in `external_evidence_items` — see that model's docstring.

---

## 3. Configuration

Four settings, none of which belong in the repository:

| Variable | Required | Notes |
|---|---|---|
| `GITHUB_CLIENT_ID` | to enable GitHub | From the GitHub OAuth App. Absent means the provider reports "not available" before the student acts. |
| `GITHUB_CLIENT_SECRET` | with the above | Never reaches the client. There is no field for it in any response schema. |
| `GITHUB_OAUTH_REDIRECT_URI` | with the above | Must be `https://`, must match the OAuth App exactly, and must carry no query string. Startup refuses a half-configured set. |
| `TOKEN_ENCRYPTION_KEY` | yes | Fernet key. Provider tokens are encrypted at rest; startup refuses a key that cannot decrypt what it encrypts. |

The Android return URI (`PROFESSIONAL_PRESENCE_APP_REDIRECT`, default
`aiplacementos://presence/callback`) is registered in `AndroidManifest.xml`. It is **not** a
secret and carries no credential — see §5.

Creating the OAuth App is a manual step performed by a human on GitHub. Nothing in this
repository creates, configures or tests against a real OAuth App, and live consent has never
been exercised — see §7.

---

## 4. Sync and freshness

Sync is **student-triggered only**. There is no scheduler in this deployment, and the feature
does not claim background refresh anywhere in the UI.

- Opening the screen costs **no** sync. It reads stored data.
- `POST /connections/{provider}/sync` is rate limited to **6/hour per authenticated student**.
  Note that in-memory counters are **per worker**, so with `--workers 4` the real ceiling is
  4×. That is a bound on abuse and cost, not an exact global quota — see
  `PRODUCTION_DEPLOYMENT.md`.
- One sync costs at most **1 + 3 + 25 GitHub calls** (profile, up to three listing pages, then
  language detail for the 25 most recently pushed repositories) against a 5,000/hour quota.
- A snapshot row is written **only when the account's material content changed**, decided by a
  sha256 fingerprint. Re-syncing an unchanged account writes no history row and makes **no AI
  call**.

`freshness` is computed server-side (`never` / `fresh` ≤7d / `aging` ≤90d / `stale`) so every
client says the same thing about the same age. `last_synced_at` means **last successful** sync
and never advances on failure — otherwise "synced 2 minutes ago" would sit beside weeks-old data.

**A failed sync destroys nothing.** Evidence is not cleared, the previous snapshot stays, and the
connection records `sync_status=failed` plus an error *category*. "GitHub could not be reached"
must never be rendered as "this student has no evidence" — the second is a claim about the
student made from a fact about our infrastructure.

**Reconciliation.** Evidence for a repository that has disappeared is removed **only when the
listing was complete**. A student with more repositories than the page cap would otherwise lose
genuine evidence every sync simply because we stopped reading.

---

## 5. Security assumptions

- **The provider token never leaves the server.** No response schema in this feature has a field
  for it, which is a stronger guarantee than remembering to exclude one. A test asserts it.
- **The deep link carries a result and nothing else** — no token, no authorization code, no
  state, no PKCE verifier. Any app on the device can register the scheme, so the redirect is
  assumed readable and *spoofable* by a hostile app. The Android client therefore reads nothing
  out of it and re-reads `GET /connections` on resume to learn what happened.
- **The OAuth state is the entire access control for the callback**, which is reachable with no
  bearer token and no App Check header (GitHub's redirect carries neither). It is 256 bits,
  stored only as a hash, single-use, expiring, and bound to the student who began the flow.
  `complete()` takes no profile argument, so it cannot bind the wrong student.
- **Every state failure returns one generic result.** Distinguishing "expired" from "never
  existed" would make the endpoint an oracle for probing other people's pending authorisations.
- **Identity is the provider's numeric account id, never the username.** A GitHub login can be
  renamed and the old name re-registered by someone else. A sync that sees a different id refuses
  and marks the connection `needs_reauth` rather than filing one person's repositories under
  another person's profile.
- **One external identity, one live connection**, enforced by a partial unique index — otherwise
  two students could connect the same GitHub account and one could borrow the other's work.
- **Provider error text never travels.** Errors are reduced to a fixed category vocabulary before
  leaving the adapter, because provider bodies can echo URLs, repository names or token
  fragments, and those categories are persisted and rendered.

---

## 6. Data lifecycle

| Table | Holds | Removed when |
|---|---|---|
| `provider_connections` | One grant. Encrypted token, status, sync state | Account deleted. Disconnect destroys the token and sets `revoked_at`, keeping the row as the audit record of a grant that existed |
| `external_evidence_items` | One normalised skill signal per (repo, skill, kind) | The repository disappears (complete listings only), the connection is deleted, or the account is deleted |
| `external_profile_snapshots` | Normalised account state + the generated audit narrative | Connection or account deleted |

Disconnecting stops evidence counting **immediately**: the Evidence Engine filters on a live
connection in SQL, so it does not depend on a cleanup pass having run.

Raw provider responses are never stored. Only normalised fields the product actually renders, so
the database does not accumulate unreviewed third-party text that ages badly.

---

## 7. What is NOT verified

- **Live GitHub OAuth has never been exercised.** No real OAuth App has been created or
  configured, and no real consent screen has been completed. Every test stubs the adapter's
  outbound HTTP. The flow is verified by construction and by tests; it is **not** verified
  against GitHub itself.
- **The Android OAuth round trip has not been run on a device** against a real backend with a
  real OAuth App, for the same reason.
- `read:user`-scoped data and contribution history are out of scope by choice (§2).

---

## 8. How evidence is weighed

External evidence is deliberately the weakest source in the Evidence Engine:

- It carries **no score**. `ExternalEvidenceItem` has no score column and the matcher emits none.
  Since `verified` requires a source scored ≥75, **external evidence can never on its own make a
  skill verified.** It can add breadth alongside an independently scored source. A repository is
  not a graded assessment and there is no honest number to put there.
- A **measured language** is independent evidence. A **repository topic** is a label the owner
  typed, so it is treated as self-asserted — anyone can put `kubernetes` on an empty repository
  in seconds, and treating that as independent would let a student promote a skill by labelling.
- Forks and archived repositories produce **no** evidence: a fork is somebody else's code until
  proven otherwise, and an archive is explicitly finished work rather than current capability.

The audit's AI layer may phrase an observation but can never introduce one. Every finding's
`evidence` sentence is built in Python from stored data; generated prose is looked up by
observation code and discarded if it does not match a real observation. The prompt version is
stored on the snapshot alongside the text it produced.
