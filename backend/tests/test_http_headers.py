"""The security headers, and the CORS policy that used to undo them.

These are set in two places on purpose. The gateway is the only ingress a
deployed stack has, and it covers responses the backend never produced — a 502
while the backend is restarting is an nginx error page. But the gateway is
invisible to this suite, applies one blanket policy to every /api response, and
would silently stop covering anything that ever reached Flask another way. So
Flask sets them too, tightened per content type, and the gateway hides the
upstream copy so exactly one of each survives.

`nosniff` is the one that earns its place. `/api/report/<id>/export?format=html`
renders agent-written post content. It is escaped by the renderer (Phase 8
Step 3), and a browser that MIME-sniffs its way to a different content type is
a second chance at the same mistake.
"""

from __future__ import annotations

import pytest

from app.main import HTML_CSP, JSON_CSP, create_app

SECURITY_HEADERS = (
    "X-Content-Type-Options",
    "Referrer-Policy",
    "X-Frame-Options",
    "Content-Security-Policy",
)


@pytest.fixture
def client():
    return create_app().test_client()


# ==========================================================================
# Present on everything
# ==========================================================================


@pytest.mark.parametrize("header", SECURITY_HEADERS)
def test_every_security_header_is_on_a_normal_response(client, header):
    assert header in client.get("/api/health/live").headers


@pytest.mark.parametrize("header", SECURITY_HEADERS)
def test_EVERY_SECURITY_HEADER_IS_ON_AN_ERROR_TOO(client, header):
    """An error response is still a response the browser renders."""
    assert header in client.get("/api/report/does-not-exist").headers


@pytest.mark.parametrize("path", [
    "/api/health/live",
    "/api/graph/",
    "/api/simulation/list",
    "/api/report/",
    "/api/nope",
])
def test_nosniff_is_set_across_the_api(client, path):
    assert client.get(path).headers.get("X-Content-Type-Options") == "nosniff"


def test_the_404_handler_does_not_lose_the_headers(client):
    response = client.get("/definitely/not/a/route")
    assert response.status_code == 404
    assert response.headers.get("X-Content-Type-Options") == "nosniff"


# ==========================================================================
# Tightened per content type
# ==========================================================================


def test_json_gets_the_strictest_policy(client):
    assert client.get("/api/health/live").headers["Content-Security-Policy"] == JSON_CSP


def test_json_needs_no_style_allowance(client):
    assert "unsafe-inline" not in client.get("/api/health/live") \
        .headers["Content-Security-Policy"]


def test_NO_POLICY_ANYWHERE_ALLOWS_A_SCRIPT():
    """The report export carries agent-written text; nothing should execute."""
    for policy in (HTML_CSP, JSON_CSP):
        assert "default-src 'none'" in policy
        assert "script-src" not in policy, "no script source is allowlisted at all"


def test_the_html_policy_allows_only_what_the_export_actually_uses():
    # One <style> block, no scripts, no external references.
    assert "style-src 'unsafe-inline'" in HTML_CSP
    assert "http" not in HTML_CSP, "no host is allowlisted"


@pytest.mark.parametrize("policy", [HTML_CSP, JSON_CSP])
def test_no_policy_can_be_reframed_or_rebased(policy):
    assert "frame-ancestors 'none'" in policy
    assert "base-uri 'none'" in policy
    assert "form-action 'none'" in policy


def test_a_handler_that_sets_its_own_policy_keeps_it():
    """setdefault, not assignment — the after_request must not overwrite."""
    app = create_app()

    @app.get("/api/_test/custom")
    def custom():
        from flask import make_response

        response = make_response("ok")
        response.headers["Content-Security-Policy"] = "default-src 'self'"
        return response

    headers = app.test_client().get("/api/_test/custom").headers
    assert headers["Content-Security-Policy"] == "default-src 'self'"
    assert headers["X-Content-Type-Options"] == "nosniff", "the rest still apply"


# ==========================================================================
# CORS
# ==========================================================================


def test_THE_API_IS_NOT_OPEN_TO_EVERY_ORIGIN(client):
    """The UI is same-origin. A wildcard let any page the user visited read
    their simulation data off a stack running on their own machine."""
    assert "Access-Control-Allow-Origin" not in client.get("/api/health/live").headers


def test_an_explicit_origin_can_be_allowed(monkeypatch):
    monkeypatch.setenv("CROWDSIGHT_CORS_ORIGINS", "http://127.0.0.1:5173")
    client = create_app().test_client()
    response = client.get("/api/health/live",
                          headers={"Origin": "http://127.0.0.1:5173"})
    assert response.headers.get("Access-Control-Allow-Origin") == "http://127.0.0.1:5173"


def test_a_wildcard_is_refused_even_when_asked_for(monkeypatch):
    monkeypatch.setenv("CROWDSIGHT_CORS_ORIGINS", "*")
    client = create_app().test_client()
    assert "Access-Control-Allow-Origin" not in client.get("/api/health/live").headers


def test_an_origin_that_was_not_allowed_gets_nothing(monkeypatch):
    monkeypatch.setenv("CROWDSIGHT_CORS_ORIGINS", "http://127.0.0.1:5173")
    client = create_app().test_client()
    response = client.get("/api/health/live", headers={"Origin": "http://evil.example"})
    assert response.headers.get("Access-Control-Allow-Origin") != "http://evil.example"
