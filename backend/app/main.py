"""Minimal backend entrypoint.

Exists so the sealed stack has something running to verify against. The real
API surface arrives in Phase 3 onward; today this serves health only.

Two endpoints, deliberately distinct:

* ``/api/health/live`` — liveness. Answers 200 as long as the process is up,
  and never touches a dependency. Docker's healthcheck uses this; a readiness
  probe that fails because Ollama is slow would restart a perfectly healthy
  container mid-simulation.
* ``/api/health`` — readiness. Reports configuration validity, any perimeter
  warnings, and whether Ollama and Neo4j are reachable.
"""

from __future__ import annotations

import os
import socket
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import Flask, jsonify
from flask_cors import CORS

from app.api.graph import bp as graph_bp
from app.api.report import bp as report_bp
from app.api.simulation import bp as simulation_bp
from app.api.simulation import control as simulation_control_bp
from app.config import ConfigError, PerimeterWarning, get_config

DEFAULT_PORTS = {"http": 80, "https": 443, "bolt": 7687, "neo4j": 7687}

#: Free space below which the health endpoint says so.
MIN_FREE_GB = 5.0

#: Everything a run writes lives under here.
DATA_ROOT = Path(os.environ.get("CROWDSIGHT_DATA_ROOT", "data"))


def _probe(url: str, timeout: float = 2.0) -> dict[str, Any]:
    """TCP-connect to a URL's host/port.

    A connect is enough to answer "is it there" without importing a driver or
    issuing a request that could block for a minute on a busy Ollama.
    """
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return {"reachable": False, "error": f"no hostname in {url!r}"}
    port = parsed.port or DEFAULT_PORTS.get(parsed.scheme.split("+")[0], 0)
    if not port:
        return {"reachable": False, "error": f"no port for scheme {parsed.scheme!r}"}
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"reachable": True, "endpoint": f"{host}:{port}"}
    except OSError as exc:
        return {"reachable": False, "endpoint": f"{host}:{port}", "error": str(exc)}


# The report export is a self-contained document: one <style> block, no
# scripts, no external references. So it takes the strictest policy that still
# renders. JSON needs nothing at all.
HTML_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
JSON_CSP = (
    "default-src 'none'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


def _models_present(config: Any) -> dict[str, Any]:
    """Are the models actually pulled, not just is Ollama answering?

    Reachability and availability are different failures with the same
    symptom. A sealed stack whose model was never pulled looks perfectly
    healthy until the first inference call fails — and it cannot fix itself,
    because pulling requires the internet the seal removes. That is worth
    knowing at startup rather than three minutes into a run.
    """
    import json as _json
    import urllib.request

    wanted = {config.LLM_MODEL_NAME, config.EMBEDDING_MODEL}
    base = config.LLM_BASE_URL.rstrip("/")
    # /v1 is the OpenAI-compatible surface; the tag list is Ollama's own.
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=5.0) as response:
            tags = _json.load(response)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return {"checked": False, "error": str(exc), "missing": sorted(wanted)}

    # Ollama reports "qwen2.5:14b"; a config naming "qwen2.5" should match it.
    present = {str(entry.get("name", "")) for entry in tags.get("models", [])}
    bare = {name.split(":")[0] for name in present}
    missing = sorted(
        name for name in wanted
        if name not in present and name.split(":")[0] not in bare
    )
    return {"checked": True, "present": sorted(present), "missing": missing}


def _disk(path: str | Path) -> dict[str, Any]:
    """Headroom where simulations are written.

    A run that fills the disk mid-round loses the round it was writing, and
    SQLite's error for that is not obviously about space.
    """
    import shutil as _shutil

    try:
        usage = _shutil.disk_usage(str(path))
    except OSError as exc:
        return {"checked": False, "error": str(exc)}
    free_gb = usage.free / 1_073_741_824
    return {
        "checked": True,
        "path": str(path),
        "free_gb": round(free_gb, 1),
        "total_gb": round(usage.total / 1_073_741_824, 1),
        "percent_used": round(100 * usage.used / usage.total, 1),
        # A 50-agent run writes tens of MB; the floor is for the operator, not
        # the run, and gives time to act before anything actually fails.
        "low": free_gb < MIN_FREE_GB,
    }


def create_app() -> Flask:
    from app.logging_setup import configure

    configure()
    app = Flask(__name__)

    # Same-origin in production: the gateway serves the UI and proxies /api
    # underneath it, so the browser never makes a cross-origin request and no
    # CORS header is required at all. The wildcard that used to be here was
    # left over from before there was a frontend, and it meant any page the
    # user happened to visit could read their simulation data from a stack
    # running on their own machine.
    #
    # `npm run dev` is the one real cross-origin case, and it proxies /api
    # itself rather than reaching across origins. CROWDSIGHT_CORS_ORIGINS
    # exists for anyone who needs something else; it is empty by default and
    # never accepts "*".
    origins = [
        origin.strip()
        for origin in os.environ.get("CROWDSIGHT_CORS_ORIGINS", "").split(",")
        if origin.strip() and origin.strip() != "*"
    ]
    if origins:
        CORS(app, resources={r"/api/*": {"origins": origins}})
    app.register_blueprint(graph_bp)
    app.register_blueprint(simulation_bp)
    app.register_blueprint(simulation_control_bp)
    app.register_blueprint(report_bp)

    @app.after_request
    def _security_headers(response):
        """Set them here as well as at the gateway.

        The gateway is the only ingress in a deployed stack, but it is invisible
        to this test suite, and it applies one blanket policy to every /api
        response. Setting them here means the guarantee is unit-tested, travels
        with the app however it is reached, and can be tightened per content
        type — JSON needs nothing at all, while the report export is a real HTML
        document.

        nosniff is the one that earns its place. /api/report/<id>/export renders
        agent-written post content; it is escaped by the renderer, but a browser
        that MIME-sniffs its way to a different content type is a second chance
        at the same mistake.

        setdefault, not assignment: a handler that has deliberately set its own
        policy keeps it.
        """
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        is_html = (response.mimetype or "").lower() == "text/html"
        response.headers.setdefault(
            "Content-Security-Policy", HTML_CSP if is_html else JSON_CSP)
        return response

    @app.errorhandler(404)
    def _not_found(_exc):
        return jsonify({"error": "Not found"}), 404

    @app.errorhandler(500)
    def _server_error(exc):  # pragma: no cover - defensive
        return jsonify({"error": f"{exc.__class__.__name__}: {exc}"}), 500

    # Capture perimeter warnings raised while the config is first built, so the
    # health endpoint can report them rather than leaving them in the log only.
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always", PerimeterWarning)
        try:
            config = get_config()
            config_error: str | None = None
        except ConfigError as exc:
            config = None
            config_error = str(exc)

    @app.get("/api/health/live")
    def live():
        return jsonify({"status": "alive"}), 200

    @app.get("/api/health")
    def health():
        if config is None:
            return jsonify({"status": "error", "config": "invalid", "detail": config_error}), 503

        checks = {
            "ollama": _probe(config.LLM_BASE_URL),
            "neo4j": _probe(config.NEO4J_URI),
        }
        models = _models_present(config) if checks["ollama"]["reachable"] else {
            "checked": False, "error": "ollama is not reachable", "missing": []}
        # Where simulations are written. Config has no setting for this — the
        # runtime places it — so the check names the directory rather than
        # inventing a config key that nothing else would read.
        disk = _disk(DATA_ROOT)

        reachable = all(check["reachable"] for check in checks.values())
        # A missing model is not "degraded" politeness — nothing will run.
        ready = reachable and not models.get("missing")
        payload = {
            "status": "ok" if ready else "degraded",
            "config": "valid",
            "perimeter_warnings": list(config.perimeter_notes),
            "checks": checks,
            "models": models,
            "disk": disk,
            "model": config.LLM_MODEL_NAME,
            "embedding_model": config.EMBEDDING_MODEL,
        }
        if disk.get("low"):
            payload["warnings"] = [
                f"only {disk['free_gb']} GB free at {disk['path']}; a run that "
                f"fills the disk loses the round it was writing"]
        return jsonify(payload), 200 if ready else 503

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    # Flask's development server. Phase 10 replaces this with a production
    # WSGI server; it is adequate while the API is health-only.
    app.run(host="0.0.0.0", port=5000, threaded=True)
