"""A 422 must never hand back what the client submitted.

The defect this pins was found by the post-Phase-28 audit and reproduced directly: Pydantic
attaches the offending value to every validation error under `input`, and the handler in
`app/main.py` returned those errors verbatim. So `POST /auth/signup` with a password shorter
than the minimum answered 422 with that password in the response body.

A password is the sharpest example, not the only one. The same shape would return a pasted job
description, a resume excerpt, an API key typed into the wrong field -- anything a student put
in a field that then failed validation. The client already knows what it sent, so this is not a
cross-user leak; it matters because response bodies travel further than requests do, into
proxy logs, crash reports and support tickets.

The invariant is therefore about the *handler*, not about passwords: no validation error this
app returns carries an `input` key, whatever the route or the field. These tests assert both
halves -- the value is gone, and the information a client needs to fix the request is not.
"""
import json

import pytest

from app.main import _validation_errors_without_input

#: Values chosen so that a leak is unambiguous in the response body. None is a real credential.
SENSITIVE = {
    "password": "hunter2-correct-horse",
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.PHASE29TOKEN.sig",
    "api_key": "sk-phase29-0123456789abcdefghij",
    "resume": "PHASE29 RESUME Priya Sharma priya@example.com B.Tech CSE 8.9 CGPA",
    "job_description": "PHASE29 JD We are hiring a backend engineer with Python and Postgres",
}


def _assert_no_submitted_values(body: str, *values: str) -> None:
    for value in values:
        assert value not in body, (
            f"the 422 response echoed a submitted value back to the client: {value[:24]!r}..."
        )


def test_signup_with_a_short_password_does_not_echo_the_password(client):
    """The exact reproduction from the audit."""
    response = client.post(
        "/auth/signup",
        json={"email": "phase29@example.com", "password": SENSITIVE["password"][:3]},
    )
    assert response.status_code == 422
    _assert_no_submitted_values(response.text, SENSITIVE["password"][:3])
    assert "input" not in response.text


def test_signup_with_a_full_length_password_that_fails_other_validation(client):
    response = client.post(
        "/auth/signup",
        json={"email": "not-an-email", "password": SENSITIVE["password"]},
    )
    assert response.status_code == 422
    _assert_no_submitted_values(response.text, SENSITIVE["password"], "not-an-email")


def test_an_invalid_email_still_reports_what_is_wrong_and_where(client):
    """Removing `input` must not turn 422 into an unactionable error."""
    response = client.post("/auth/signup", json={"email": "nope", "password": "x"})
    assert response.status_code == 422
    payload = response.json()
    assert payload["detail"] == "Validation failed"
    assert payload["request_id"]
    errors = payload["errors"]
    assert errors, "a 422 with no errors tells the client nothing"
    for error in errors:
        assert "type" in error and "loc" in error and "msg" in error
        assert "input" not in error
    # The field that failed is still identifiable, which is the whole point of keeping `loc`.
    located = {part for error in errors for part in error["loc"]}
    assert "email" in located or "password" in located


@pytest.mark.parametrize("field", sorted(SENSITIVE))
def test_no_sensitive_value_is_echoed_from_any_field(client, field):
    """Submit each sensitive shape into a request that cannot validate, and look for it back."""
    value = SENSITIVE[field]
    response = client.post("/auth/signup", json={"email": value, "password": value})
    assert response.status_code == 422
    _assert_no_submitted_values(response.text, value)


def test_a_wrong_type_does_not_echo_the_structure_that_was_sent(client):
    """Type errors are the case where Pydantic attaches whole objects, not just scalars."""
    response = client.post(
        "/auth/signup",
        json={"email": {"nested": SENSITIVE["resume"]}, "password": [SENSITIVE["token"]]},
    )
    assert response.status_code == 422
    _assert_no_submitted_values(response.text, SENSITIVE["resume"], SENSITIVE["token"])


def test_a_query_parameter_is_not_echoed(client):
    """The handler is global, so the guarantee must not be specific to JSON bodies."""
    response = client.get("/jobs", params={"limit": SENSITIVE["api_key"]})
    if response.status_code == 422:
        _assert_no_submitted_values(response.text, SENSITIVE["api_key"])
        assert "input" not in response.text


def test_the_sanitiser_keeps_schema_constraints_and_drops_the_raised_exception():
    """`ctx` carries two different kinds of thing, and only one of them is safe to return."""
    cleaned = _validation_errors_without_input(
        [
            {
                "type": "string_too_short",
                "loc": ("body", "password"),
                "msg": "String should have at least 8 characters",
                "input": SENSITIVE["password"],
                "ctx": {"min_length": 8},
            },
            {
                "type": "value_error",
                "loc": ("body", "email"),
                "msg": "Value error, that address is not usable",
                "input": SENSITIVE["resume"],
                "ctx": {"error": ValueError(SENSITIVE["resume"])},
            },
        ]
    )
    assert len(cleaned) == 2
    assert all("input" not in error for error in cleaned)
    # Schema-side metadata survives: it tells the client the rule, not the value.
    assert cleaned[0]["ctx"] == {"min_length": 8}
    # The raised exception does not: it renders to a string the validator chose, which for a
    # custom validator can quote the input straight back.
    assert cleaned[1]["ctx"] == {}
    assert cleaned[0]["msg"] and cleaned[1]["msg"]
    assert SENSITIVE["password"] not in json.dumps(cleaned, default=str)
    assert SENSITIVE["resume"] not in json.dumps(cleaned, default=str)


def test_the_sanitiser_drops_anything_it_cannot_reason_about():
    """An error that is not a dict could carry the input in a field this code never inspects."""
    assert _validation_errors_without_input(["not a dict", None, 42]) == []


def test_no_code_outside_the_central_handler_reads_a_validation_error_input():
    """The central guarantee is only worth as much as its coverage.

    A route that called `.errors()` itself and returned the result would reintroduce the leak
    for that endpoint alone while every test above kept passing. Two places outside the handler
    legitimately call `.errors()` -- both AI providers, summarising a schema failure of the
    *model's own output* rather than of a client request -- so the rule here is not "never call
    it" but "never touch the submitted value": each call site must project the error onto safe
    keys such as `loc` and `type`, and none may reach for `input`.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    offenders = []
    for path in (root / "app").rglob("*.py"):
        if path.name == "main.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"\.errors\(\)", text):
            line = text[: match.start()].count("\n") + 1
            window = text[max(0, match.start() - 300) : match.end() + 300]
            if re.search(r"""\[\s*["']input["']\s*\]|\.get\(\s*["']input["']""", window):
                offenders.append(f"{path.relative_to(root)}:{line}")
    assert offenders == [], (
        "these read the submitted value out of a validation error outside the central "
        f"handler, where nothing strips it: {offenders}"
    )
