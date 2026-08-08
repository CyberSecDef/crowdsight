"""Phase 10 Step 2 — the compliance gate, in the parts a packet capture cannot see.

`test_network_isolation.py` proves the running network has no route off-host.
This proves the things that survive a restart and would still be true if the
network were opened tomorrow: that no source file names a host we did not
choose, that every dependency resolves from the registry we said it does, that
the configuration refuses an endpoint outside the perimeter, and that the
frontend container is confined exactly as the backend is.

**A failure here is a release blocker, not a warning.** Marked `egress` rather
than `integration` for the same reason the seal proof is: integration tests are
deselected by default, and a check you have to remember to ask for is one that
eventually nobody asks for.

**Traffic capture is deliberately not here.** The spec offers it as an option:
capture during a short run and assert every destination is allowlisted. Doing
that means installing tcpdump inside the container and granting it NET_RAW —
weakening the very confinement being tested in order to test it. The evidence
already available is stronger and costs nothing: the sealed network is declared
`internal: true`, so there is no route for traffic to be captured *on*, and the
tests below attempt real connections to real external hosts and require them to
fail.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import in_container

pytestmark = pytest.mark.egress

REPO = Path(__file__).resolve().parents[2]

#: The only hosts a *runtime* source file may name.
#:
#: Service names on the sealed network, plus loopback. Anything else in
#: backend/app or frontend/src means code that intends to talk to somewhere we
#: did not choose — which is the failure this whole project exists to prevent.
ALLOWED_RUNTIME_HOSTS = frozenset({
    "ollama",      # local inference
    "neo4j",       # the graph
    "backend",     # the API, from the gateway
    "frontend",    # the UI, from the gateway
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
})

#: Where runtime code lives. Tests and docs are excluded on purpose: a test
#: that proves `api.openai.com` is unreachable has to name it, and a README
#: that links to a licence is not calling it.
RUNTIME_TREES = (
    REPO / "backend" / "app",
    REPO / "frontend" / "src",
)


def _require_repo() -> None:
    """The source audits read the repository, which a container does not hold.

    The backend image carries `backend/` only, and `frontend/src` is not in it
    at all — so an in-container run would audit half the tree while looking
    green. Same split as the topology checks in test_network_isolation.py:
    what cannot be verified here says so and points at where it can be.
    """
    if not all(tree.is_dir() for tree in RUNTIME_TREES):
        pytest.skip(
            "reads the source tree, which this container does not hold; run "
            "from the host with `pytest backend/tests/test_egress_verification.py`")

URL = re.compile(r"https?://([A-Za-z0-9._-]+)")


def _hosts_in(path: Path) -> set[str]:
    try:
        return set(URL.findall(path.read_text(encoding="utf-8", errors="ignore")))
    except OSError:
        return set()


def _source_files(tree: Path):
    for path in tree.rglob("*"):
        if not path.is_file():
            continue
        if any(part in {"node_modules", "__pycache__", "dist"} for part in path.parts):
            continue
        if path.suffix.lower() in {".py", ".js", ".vue", ".css", ".html", ".json"}:
            yield path


# --------------------------------------------------------------------------
# No source file names a host we did not choose
# --------------------------------------------------------------------------


def test_the_runtime_trees_exist():
    """A path typo here would make every audit below pass vacuously."""
    _require_repo()
    for tree in RUNTIME_TREES:
        assert tree.is_dir(), f"{tree} is not a directory; the audit would check nothing"
    assert any(True for tree in RUNTIME_TREES for _ in _source_files(tree))


def test_NO_RUNTIME_SOURCE_FILE_NAMES_AN_EXTERNAL_HOST():
    """The check that survives the network being opened tomorrow."""
    _require_repo()
    offenders: dict[str, set[str]] = {}
    for tree in RUNTIME_TREES:
        for path in _source_files(tree):
            stray = _hosts_in(path) - ALLOWED_RUNTIME_HOSTS
            if stray:
                offenders[str(path.relative_to(REPO))] = stray

    assert not offenders, (
        "runtime source names hosts outside the allowlist: "
        + "; ".join(f"{name} -> {sorted(hosts)}" for name, hosts in offenders.items())
    )


def test_the_audit_would_notice_a_new_host(tmp_path):
    """A grep that matches nothing passes; this proves it matches something."""
    planted = tmp_path / "leak.py"
    planted.write_text('TELEMETRY = "https://telemetry.example.com/v1"\n')

    assert _hosts_in(planted) - ALLOWED_RUNTIME_HOSTS == {"telemetry.example.com"}


@pytest.mark.parametrize("host", [
    "api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com",
    "huggingface.co", "registry.npmjs.org",
])
def test_no_cloud_provider_is_named_in_runtime_source(host):
    """Named individually so a failure says which one appeared."""
    _require_repo()
    for tree in RUNTIME_TREES:
        for path in _source_files(tree):
            assert host not in _hosts_in(path), \
                f"{path.relative_to(REPO)} names {host}"


# --------------------------------------------------------------------------
# The supply chain: every dependency comes from where we said
# --------------------------------------------------------------------------


def test_EVERY_NPM_DEPENDENCY_RESOLVES_FROM_THE_PUBLIC_REGISTRY():
    """Build-time internet is accepted; an unexpected *host* is not.

    A dependency resolved from somewhere other than the registry would be a
    supply-chain change nobody reviewed, and it would be invisible in the
    package.json everybody reads.
    """
    _require_repo()
    lockfile = REPO / "frontend" / "package-lock.json"
    assert lockfile.is_file(), "no lockfile; the frontend build is not reproducible"

    packages = json.loads(lockfile.read_text())["packages"]
    hosts = {
        URL.findall(entry["resolved"])[0]
        for entry in packages.values()
        if entry.get("resolved")
    }
    assert hosts <= {"registry.npmjs.org"}, f"dependencies resolve from {sorted(hosts)}"


def test_the_lockfile_actually_pins_something():
    """An empty lockfile would satisfy the check above while pinning nothing."""
    _require_repo()
    packages = json.loads(
        (REPO / "frontend" / "package-lock.json").read_text())["packages"]
    assert sum(1 for e in packages.values() if e.get("resolved")) > 10


def test_python_requirements_name_no_index_server():
    """A `--index-url` in requirements would move the whole install elsewhere."""
    _require_repo()
    for name in ("requirements.txt", "requirements-dev.txt"):
        path = REPO / "backend" / name
        if not path.is_file():
            continue
        text = path.read_text()
        for directive in ("--index-url", "--extra-index-url", "--find-links"):
            assert directive not in text, f"{name} redirects installation: {directive}"


# --------------------------------------------------------------------------
# Configuration refuses an endpoint outside the perimeter
# --------------------------------------------------------------------------


@pytest.fixture
def base_env(monkeypatch):
    """Everything Config needs *except* the endpoint under test.

    Without this the refusal tests passed on a host with no .env — but for the
    wrong reason: Config raised over a missing NEO4J_PASSWORD, so they would
    have gone on passing with the perimeter check deleted entirely. A test that
    cannot fail is not a test.
    """
    monkeypatch.setenv("NEO4J_PASSWORD", "not-a-real-password")
    return monkeypatch


# Note 203.0.113.0/24 is deliberately absent. It is RFC 5737 documentation
# space, and Python's `ipaddress.is_private` reports it as private — so it
# would test the opposite of what it looks like it tests.
@pytest.mark.parametrize("url", [
    "https://api.openai.com/v1",
    "https://ollama.example.com",
    "http://8.8.8.8:11434",
    "http://1.1.1.1:11434",
])
def test_CONFIG_REFUSES_A_PUBLIC_ENDPOINT(url, base_env):
    """The perimeter is enforced in configuration, not only in the network."""
    from app.config import Config, ConfigError

    base_env.setenv("LLM_BASE_URL", url)
    with pytest.raises((ConfigError, ValueError)) as raised:
        Config()

    # The refusal must be about the *perimeter*. Accepting any error here is
    # how these tests passed while proving nothing.
    assert "outside the sealed perimeter" in str(raised.value), str(raised.value)


@pytest.mark.parametrize("url", [
    "http://ollama:11434/v1",
    "http://localhost:11434/v1",
    "http://127.0.0.1:11434/v1",
])
def test_config_accepts_a_local_endpoint(url, base_env):
    """The refusal above must be about being external, not about being strict."""
    from app.config import Config

    base_env.setenv("LLM_BASE_URL", url)
    assert Config().LLM_BASE_URL == url


def test_a_private_lan_endpoint_is_permitted_but_warned(base_env):
    """Second-best and said out loud: traffic to another machine still leaves
    this host, and the container seal cannot cover it."""
    from app.config import Config

    base_env.setenv("LLM_BASE_URL", "http://192.168.1.50:11434/v1")
    config = Config()
    assert config.perimeter_notes, "a LAN endpoint must record a warning"


# --------------------------------------------------------------------------
# Container topology — the frontend, which the seal proof does not cover
# --------------------------------------------------------------------------


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True,
                          timeout=30).returncode == 0


def _require_docker_host(container: str = "crowdsight-frontend") -> None:
    if in_container():
        pytest.skip(
            "inspects the Docker daemon; run from the host with "
            "`pytest backend/tests/test_egress_verification.py`")
    if not _docker_available():
        pytest.fail("docker is not usable here, so the topology cannot be verified")
    running = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container],
        capture_output=True, text=True, timeout=30)
    assert running.stdout.strip() == "true", (
        f"{container} is not running, so its confinement cannot be verified. "
        "Start the stack with `docker compose up -d`.")


def _networks(container: str) -> set[str]:
    result = subprocess.run(
        ["docker", "inspect", "-f",
         "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}", container],
        capture_output=True, text=True, timeout=30)
    return set(result.stdout.split())


def test_THE_FRONTEND_IS_ON_THE_SEALED_NETWORK_AND_NOT_THE_EDGE():
    """It serves a compiled bundle; it has no reason to reach anything."""
    _require_docker_host()
    networks = _networks("crowdsight-frontend")

    assert any("sealed" in name for name in networks), \
        f"the frontend is not on the sealed network: {sorted(networks)}"
    assert not any("edge" in name for name in networks), \
        f"the frontend is on the edge network: {sorted(networks)}. Only the gateway may be."


def test_the_frontend_publishes_no_ports():
    _require_docker_host()
    result = subprocess.run(["docker", "port", "crowdsight-frontend"],
                            capture_output=True, text=True, timeout=30)
    assert result.stdout.strip() == "", (
        f"the frontend publishes ports: {result.stdout!r}. Only the gateway may.")


def test_the_frontend_has_no_default_route():
    """No default route means no way off the host, whatever the code asks for."""
    _require_docker_host()
    result = subprocess.run(
        ["docker", "exec", "crowdsight-frontend", "ip", "route"],
        capture_output=True, text=True, timeout=30)
    assert "default" not in result.stdout, (
        f"the frontend has a default route: {result.stdout!r}")


def test_THE_FRONTEND_CANNOT_REACH_THE_REGISTRY_IT_WAS_BUILT_FROM():
    """npm is a build-time dependency. At runtime it must be unreachable."""
    _require_docker_host()
    result = subprocess.run(
        ["docker", "exec", "crowdsight-frontend", "timeout", "5",
         "wget", "-q", "-O", "/dev/null", "https://registry.npmjs.org"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode != 0, \
        "the frontend reached registry.npmjs.org at runtime"


def test_the_shipped_bundle_names_no_external_host():
    """The audit above reads the source; this reads what was actually built."""
    _require_docker_host()
    result = subprocess.run(
        ["docker", "exec", "crowdsight-frontend", "sh", "-c",
         "grep -rhoE 'https?://[a-zA-Z0-9.-]+' /usr/share/nginx/html "
         "--include='*.js' --include='*.html' --include='*.css' 2>/dev/null "
         "| sort -u"],
        capture_output=True, text=True, timeout=60)
    hosts = {URL.findall(line)[0] for line in result.stdout.split() if URL.findall(line)}
    # www.w3.org appears in SVG namespaces, which are identifiers rather than
    # addresses — nothing fetches them.
    assert hosts <= ALLOWED_RUNTIME_HOSTS | {"www.w3.org"}, \
        f"the shipped bundle names {sorted(hosts - ALLOWED_RUNTIME_HOSTS)}"
