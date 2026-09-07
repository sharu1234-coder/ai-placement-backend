def test_get_privacy_policy_is_public(client):
    resp = client.get("/legal/privacy-policy")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == "1"
    assert "AI Placement OS" in body["content"]
    assert "privacy@sgldquantlabs.com" in body["content"]


def test_get_privacy_policy_does_not_require_auth(client):
    # No Authorization header at all -- must not 401/403 like the account endpoints do.
    resp = client.get("/legal/privacy-policy")
    assert resp.status_code == 200


def test_get_terms_of_service_is_public(client):
    resp = client.get("/legal/terms-of-service")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == "1"
    assert "AI Placement OS" in body["content"]
    assert "legal@sgldquantlabs.com" in body["content"]


def test_get_terms_of_service_does_not_require_auth(client):
    resp = client.get("/legal/terms-of-service")
    assert resp.status_code == 200


def test_privacy_policy_html_is_browser_readable(client):
    """Google Play needs a privacy policy URL a reviewer can open in a browser. The JSON route
    is publicly accessible but renders as a raw object with the document collapsed into
    escaped newlines, which is not a policy page."""
    resp = client.get("/legal/privacy-policy.html")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "<title>AI Placement OS - Privacy Policy</title>" in body
    # Real content, not an empty shell.
    assert "Privacy Policy" in body
    assert len(body) > 1000


def test_terms_of_service_html_is_browser_readable(client):
    resp = client.get("/legal/terms-of-service.html")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<title>AI Placement OS - Terms of Service</title>" in resp.text
    assert len(resp.text) > 1000


def test_legal_html_requires_no_authentication(client):
    """Must be readable logged-out and before signup -- app stores fetch it anonymously."""
    for path in ("/legal/privacy-policy.html", "/legal/terms-of-service.html"):
        assert client.get(path).status_code == 200


def test_legal_html_escapes_its_source_rather_than_interpreting_it(client, tmp_path, monkeypatch):
    """The source is a trusted repo file today, so escaping is defence in depth: a future edit
    that happens to contain markup must render as text, not execute. Asserted by pointing the
    route at a document that actually contains a script tag."""
    from app.api import legal

    hostile = tmp_path / "privacy_policy_v1.txt"
    hostile.write_text(
        """Policy
<script>alert(1)</script>
5 > 3 & 2 < 4
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(legal, "_LEGAL_DIR", tmp_path)

    resp = client.get("/legal/privacy-policy.html")
    assert resp.status_code == 200
    assert "<script>" not in resp.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in resp.text
    assert "5 &gt; 3 &amp; 2 &lt; 4" in resp.text


def test_json_legal_routes_still_work(client):
    """The HTML routes are additive -- the Android app keeps using the JSON ones."""
    resp = client.get("/legal/privacy-policy")
    assert resp.status_code == 200
    assert resp.json()["version"] == "1"
