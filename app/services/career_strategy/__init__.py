"""Career Strategy -- the strategic aggregation layer over AI Placement OS.

This package exists so that exactly one place in the product answers "what is currently
holding this student back, and what should they do about it". It is deliberately *thin and
compositional*: it owns no scores, no evidence, no readiness model and no match engine of its
own. Every fact it reasons about is read from the service that already owns it --
evidence_engine, career_twin_service, weakness_service, weakness_record_service,
ats_check_service, job_hunt/matching, job_hunt/readiness, role_context, skill_graph -- and is
carried through with its provenance attached.

If you are adding a new signal, add a *collector* in signals.py that reads the owning service.
Do not add a second computation of something another service already produces. The whole point
of this layer is that the product stops growing parallel readiness/recommendation systems; a
second one defeats it entirely.

Phase 1 (this package's current extent) is inputs only:

    signals.py             collect every fact, uniformly shaped, provenance preserved
    precedence.py          deterministic ordering of evidence kinds when sources disagree
    gaps.py                classify facts into the eight actionable gap categories
    contradictions.py      detect the seven cross-source mismatches, preserving BOTH facts
    diminishing_returns.py analyse performance *series*, refusing to conclude from too few

Nothing here ranks, decides, persists, or is exposed over HTTP. Bottleneck selection and
impact x effort prioritisation are Phase 2; persistence and the API are Phase 3. Nothing in
the existing product imports this package yet, by design.

Two invariants hold throughout, and both are tested:

  * Unknown is never zero. A missing measurement is `value=None` with
    `evidence_kind="missing"`, never a 0.0 that later averages into someone's readiness.
  * Nothing is upgraded by agreement. Two independent claims are still two claims; only a
    genuinely independent, observed source can raise what we believe about a capability.
"""
