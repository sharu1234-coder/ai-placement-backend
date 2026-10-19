# World Career Intelligence (VA-5)

**Status: one real source, six capabilities, one declared unsupported. No live provider is
configured in this environment, so nothing has been retrieved from a real source.**

## 1. Source inventory, as actually configured

Audited against the current checkout, not carried forward from a previous report.

| Provider | Configured? | Auth | Geography | Documented limit | Terms worth knowing |
|---|---|---|---|---|---|
`NullJobProvider` | **yes — the active default** | none | — | — | Raises `JobProviderNotConfiguredError`; never empty results, never samples |
Adzuna | no (`ADZUNA_APP_ID`/`APP_KEY` unset) | app id + key | `ADZUNA_COUNTRY`, default `in` | per-minute; no per-day ceiling in code | Commercial use beyond a 14-day evaluation needs written consent; "Jobs by Adzuna" attribution link required |
Careerjet | no (`CAREERJET_AFFID` unset) | affiliate id | `CAREERJET_LOCALE_CODE`, default `en_IN` | per-minute | Contractually requires the **end user's IP and user agent** on every call |
Jobvetta | key present but **2 characters — not usable** | bearer token | India only | **50 requests per key per UTC day**, enforced in `provider_budget` | — |

`JOB_PROVIDER` is absent from `.env`, so `settings.job_provider` is `"none"` and
`build_job_provider()` returns `NullJobProvider`. An unrecognised name also falls back to it
rather than raising at import, so a typo degrades Job Hunt instead of taking the whole API
down. **Both facts are re-verified by a test rather than quoted from the earlier audit.**

The default is unchanged. Enabling a provider is a configuration change plus a terms decision
the owner has to make, and VA-5 had no authorisation to make either.

**Provider-side logging and retention: unknown.** None of the three publishes what it logs
about an inbound query or how long it keeps it. That is stated as unknown rather than asserted
to be nothing. The mitigation is what *leaves*: role, geography, experience level, employment
type, a remote flag and an age filter. Nothing else, because `WorldQuery` has no other field.

## 2. Capabilities

| Capability | Supported | Basis |
|---|---|---|
`world.job_openings` | yes | Live `JobProvider` retrieval, deduplicated, normalised |
`world.fresher_openings` | yes | Same, with `experience_level='fresher'`; contradicting adverts dropped |
`world.role_requirements` | yes | Skills extracted from retrieved adverts by the existing `normalization.extract_job_skills` |
`world.skill_demand` | yes | Frequency across one deduplicated, scoped sample |
`world.company_hiring` | yes | Advert counts per company within one sample |
`world.geographic_comparison` | yes | One retrieval per location, each sample sized |
`world.learning_resources` | **no** | **No approved retrieval source exists** |

**Why learning resources is unsupported.** There is no web-search client, documentation fetcher
or curated-resource dataset anywhere in `app/` — the only outbound HTTP in the whole backend is
the job providers, GitHub (for a user's *own* authorised account), Ollama (local) and the
payment gateway. VA-5 may not add a dependency or make unauthorised calls. Returning a model's
recollection of a URL would be a fabricated citation, which is precisely the failure this layer
exists to prevent. So the capability is declared unsupported with that reason, and asking for it
raises `CapabilityUnsupported` before any work starts.

## 3. Result contracts

`app/world/contracts.py`. The shape enforces one rule: **a live observation, an internal
reference value and a derived estimate must never be mistaken for one another.**

- `SourceKind` is `LIVE_PROVIDER` | `INTERNAL_REFERENCE` | `DERIVED`. There is no
  `MODEL_KNOWLEDGE`, because nothing in VA-5 produces it and having the label would imply a
  path that does not exist.
- `Observation[T]` carries `source`, `retrieved_at`, `published_at`, `location`, `evidence`,
  `limitations`, `conflicts` and — for anything `DERIVED` — a required `SampleScope`. A derived
  observation without its scope **raises at construction**: a count without its denominator is
  a claim about the labour market, and with the scope attached it is a claim about one query's
  results, which is what it actually is.
- `Unknown` is a separate type, not an empty `Observation`. "We looked and the market is quiet"
  and "we could not look" must not be interchangeable.
- `EvidenceState`: `CONFIRMED` | `PARTIAL` | `CONFLICTING` | `INSUFFICIENT` | `UNSUPPORTED`.
- `FieldConflict` keeps **both** values with their attribution. Picking a winner would
  manufacture a single false value and erase the disagreement.

## 4. Normalization, dedup, provenance

Extended, not duplicated. `dedupe.deduplicate` and `normalization` are the existing
job-hunt modules, used as they are.

- **Normalised:** title, company, location, employment type, work mode, experience level,
  skills (via `extract_job_skills`), URL (via `safe_external_url`, which refuses loopback and
  cloud-metadata hosts), posting date, stable provider id.
- **Experience level** uses the provider's own field when present and falls back to
  title inference only when it is absent — the provider's field is better evidence than a
  guess, and overwriting it would discard the better one. Inference marks the result `PARTIAL`
  and says so in `limitations`.
- **Deduplication** is the existing three-pass exact match: provider id → canonical URL →
  (title, company, location). Conservative in one direction on purpose: a false merge hides a
  real job the student never learns existed, while a missed merge is a cosmetic annoyance.
  Two adverts that both lack a company are *not* thereby the same job.
- **Provenance** travels per row, not per response, because one response can mix providers and
  ages and a single response-level label would be wrong about most of it. Attribution text
  travels with the data because several providers make it a contractual condition, and an
  obligation living only in a UI component is one waiting to be dropped.

**Aggregates.** Below `MIN_SAMPLE_FOR_AGGREGATE = 8` unique adverts, every aggregate returns
`Unknown(INSUFFICIENT)` instead of a percentage — "1 of 3 mentioned Kubernetes" rendered as 33%
reads exactly like a figure computed over three hundred. A skill repeated four times in one
advert counts once, or one verbose employer sets the trend. An aggregate is dated by its
**oldest** input, since it is only as current as its stalest evidence.

## 5. Freshness policy

Per category, because one duration would be wrong in both directions at once.

| Category | Fresh | Aging until | Why |
|---|---|---|---|
Job opening | ≤ 2 days | 7 days | A vacancy can be filled the day after posting; a closed advert costs a student real time |
Company hiring | ≤ 7 days | 30 days | Derived from adverts but a slower-moving signal than one vacancy |
Role requirements | ≤ 30 days | 180 days | Moves with the industry, not with one advert |
Skill demand | ≤ 30 days | 90 days | A trend claim needs dated evidence; a quarter-old sample is a different market |
Geographic comparison | ≤ 30 days | 90 days | Held to the same bar as the figures it compares |
Learning resource | ≤ 90 days | 365 days | No source exists; the row is here so adding one has to confront version-awareness |

**Undated is `UNDATED`, not stale.** Measuring from `retrieved_at` would make a six-month-old
advert look fresh because we fetched it a second ago; calling it stale would invent the bad
case. A naive timestamp is treated as UTC, once, here — providers differ and the alternative is
a `TypeError` that reads like a provider outage.

## 6. Budgets, caching, failure semantics

**Two separate limits, deliberately not merged** (the existing modules' own distinction):
`entitlement` answers "has this student paid?" (402, fixed by paying); `provider_budget` answers
"can this deployment's key take another call?" (429, not fixed by paying).

- **Budget** is reserved *before* the call and only on a cache miss — a cached answer costs the
  provider nothing, so metering it would spend a slot that was never used. A provider with no
  published per-day ceiling is not metered at all; inventing a limit nobody imposed would be a
  made-up restriction.
- **Cache** is in-process, LRU, bounded at 256 entries, TTL from each provider's own
  `cache_ttl` (a terms question, not a performance one). **It is per worker**, so N workers can
  each spend one call for the same query — which is exactly why it is *not* the quota
  mechanism; `provider_budget` is, from the database, across workers. A cache key spells `None`
  out explicitly, because without that `("python", None)` and `(None, "python")` collide and one
  query answers the other — for a geography parameter, that means answering a Pune question
  with Bangalore data.
- **Timeouts and retries** are the existing transport's: 10 s per attempt, one retry, only on a
  transport failure or 5xx, never on 4xx. Retrying a 401 cannot fix revoked credentials and
  retrying a 429 is how a rate limit becomes a ban.
- **Failure translation** keeps each meaning separate: `source_not_configured`,
  `source_quota_exhausted`, `source_rejected_credentials`, `source_unavailable`. Collapsing
  them would let a revoked key read as a quiet day in the job market. The provider's own
  message never travels outward or into a log — several of these providers put the API key in
  the query string, so the message is exactly the thing that must not be forwarded.
- **Partial results are honest.** In a geographic comparison, a location whose retrieval failed
  is **absent** from the result with the failure recorded as a limitation — never substituted.
  If *every* location fails, the error is raised rather than returned as an empty comparison.

## 7. Tool Registry integration

Six read-only tools in `app/tools/world_tools.py`, registered into the default catalogue
(19 tools total: 13 personal + 6 world). `build_default_registry(include_world=False)` gives the
personal-only catalogue for a caller that must make no outbound calls.

**One VA-4 contract change, and why.** `provider_budget.reserve_call` writes a
`ProviderCallBudget` row, so a world tool is **not** write-free — and VA-4's `ToolSpec` enforced
`side_effects == ()` for every `READ` tool. Declaring `side_effects=()` and saying nothing
would have been false. So `ToolSpec` gained `metering_effects`, kept distinct from
`side_effects` because the two differ in what they can harm: a domain side effect changes
something a student owns and would need confirmation, while a metering write changes only the
deployment's view of its own quota, carries no user data, and *skipping* it is what would do
harm — an unmetered call overspends a shared key. The read-only test now asserts that a READ
tool's writes are confined to exactly what it declared, which is a stronger guarantee than the
tidier, false claim of "no writes at all".

Authorization is unchanged: identity from the token via `authenticate_access_token`, no
owner-identifier argument anywhere, `extra="forbid"` on every input. A world tool does not even
need a profile — it requires none, because a world query carries no personal data.

## 8. Security and data minimisation

- **No credentials anywhere near the model.** Tools resolve the provider through the existing
  factory; nothing passes a key, and nothing is logged. The failure log records the provider
  name and the exception class only.
- **External text is untrusted data.** A description is wrapped in `UntrustedText`, which
  raises `TypeError` on direct interpolation so a caller has to say which boundary it is
  crossing. `for_prompt()` routes through the existing `input_guard.neutralize_delimiters`,
  which defeats *delimiter forgery* — the specific reproducible break this project found by
  red-teaming its own grading path. It does not claim to eliminate prompt injection; escaping
  alone does not, and the rest of that defence is the prompt's SECURITY RULE framing plus the
  output guard.
- **VA-5 creates no prompt path.** `job_hunt` made no AI calls before and still makes none —
  re-verified by grep over the package and its route. `UntrustedText` exists so VA-6 cannot
  create one by accident.
- **No personal data leaves.** `WorldQuery` has seven fields and none of them can carry a
  resume, profile, skill list or identifier. Careerjet's contractual end-user IP and user agent
  are deliberately left unset: a world query is not made on behalf of a browsing session, and
  inventing an address to satisfy a contractual field would be worse than declining to use a
  provider that requires it.
- **No persistence.** No migration, no new retention surface. A test asserts a world retrieval
  leaves `JobListing` empty.
- **Advert prose is withheld** from tool outputs entirely — untrusted, largest field by far, no
  VA-5 consumer needs it.

## 9. Deferred, with phase

| Item | Phase |
|---|---|
Ranking listings against private career evidence | VA-6 / agent phase (the join already exists for authenticated job search in `matching.py` and stays there) |
Agent reasoning, planning, intent loops | VA-6 |
Persistent conversation memory | VA-6+ |
Write/action tools, confirmation, idempotency keys | VA-7 |
Learning resources / official documentation | needs an approved source and authorisation to add one |
RAG, embeddings, document parsing | separately authorised |
Enabling a live provider | owner decision: credentials + terms |
LLM and STT/TTS provider selection | VA-3's evidence gate |

## 10. Known issues, still open

- VA-2 session state is process-local; **no cross-worker session recovery**.
- Live Android↔FastAPI socket: **NEEDS TEST**.
- Physical-device audio: **NEEDS TEST**.
- End-to-end spoken conversation: **not exercised**.
- No live job-provider call has ever been made from this environment.
- `weakness_record_service._STATUS_RANK` `KeyError` risk: **rechecked, deliberately not
  changed.** `WeaknessRecord.status` has exactly one writer in the codebase
  (`weakness_record_service`), which writes only `NEW`, `REPEATED`, `IMPROVING`,
  `NEARLY_CLOSED` and `CLOSED` — all five present in `_STATUS_RANK`. So the `KeyError` is not
  reachable through application code; the VA-4 report found it because that phase's *test
  fixture* wrote an invalid status, which is not a path production has. The available fix
  (`_STATUS_RANK.get(status, default)`) would trade a loud failure on a corrupt row for a
  silent mis-sort, and a row with an unknown status is a data-integrity problem worth hearing
  about. Residual exposure: a future second writer, or a manually-inserted row. Recorded here
  rather than patched.
