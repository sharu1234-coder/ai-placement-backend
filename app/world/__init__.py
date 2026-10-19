"""World Career Intelligence (VA-5): approved public career information, with its provenance.

This layer answers questions about the world outside the student's own account -- what roles
are being advertised, what those adverts ask for -- and it answers them only from sources that
actually exist in this repository. There is exactly one today: the `JobProvider` abstraction in
`app/services/job_hunt/providers/`, and in this environment it is configured to `none`.

Three boundaries define the layer:

* **Observation, not belief.** Everything returned carries where it came from, when it was
  retrieved, how fresh it is and how strong the evidence is. A fact that cannot be confirmed
  comes back as an explicit unknown rather than as a confident-looking value.
* **No personal data leaves.** A world query carries a role, a geography, an experience level
  and approved filters. It never carries a resume, a profile or an identifier. The join between
  public listings and private career evidence already exists in
  `app/services/job_hunt/matching.py` for the authenticated job-search feature, and keeping it
  there is deliberate -- see `docs/world-intelligence.md`.
* **External text is data.** A job description is written by someone outside this system. It is
  carried as `UntrustedText` so that any future path into a model prompt has to say so out
  loud and go through `app/ai/input_guard.py`. VA-5 creates no such path.

What this layer deliberately does **not** do: persist anything (so no migration and no new
retention surface), call an LLM, reason, plan, remember, or enable a provider that is not
configured. A source without credentials degrades to a typed "not configured", which is a
different fact from "no jobs found" and is never collapsed into it.
"""
