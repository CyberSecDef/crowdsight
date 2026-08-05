"""Phase 1 Step 4 — proof that the sealed network has no route off-host.

This is the test that substantiates "nothing leaves my network". Everything
else in the project is a promise; this is the check.

**The egress proof never skips.** A test that quietly passes by skipping
itself when it cannot verify the seal is worse than no test at all — it
produces a green run that means nothing. When it cannot establish a verifiable
context it fails, with instructions.

Two contexts are supported and detected automatically:

* **Inside the backend container** — the direct case. Assert against this
  process's own network stack.
* **On the host, with the stack running** — shell into the backend container
  and assert there.

The topology assertions at the bottom (network flags, port bindings, the
gateway) are a separate matter: they inspect the Docker daemon, which a
container has no view of. Those skip in-container with a reason pointing at
the host, and run for real there. Only the seal proof is unconditional.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from tests.conftest import in_container

# `egress` only, deliberately not `integration`. Integration tests are
# deselected by default so the unit loop needs no services; the seal proof must
# not inherit that, because a check you have to remember to ask for is one that
# eventually nobody asks for.
pytestmark = [pytest.mark.egress]

# Deliberately boring, highly available destinations. If any is reachable the
# seal is broken; the specific host does not matter.
TCP_TARGETS = [("1.1.1.1", 443), ("8.8.8.8", 53), ("93.184.216.34", 80)]
DNS_NAMES = ["pypi.org", "github.com", "ollama.com"]
TIMEOUT = 4.0

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _tcp_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return True
    except OSError:
        return False


def _dns_resolves(name: str) -> bool:
    try:
        socket.getaddrinfo(name, None)
        return True
    except OSError:
        return False


def _compose(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(
        ["docker", "info"], capture_output=True, timeout=30
    ).returncode == 0


def _service_running(service: str) -> bool:
    result = _compose("ps", "--status", "running", "--format", "{{.Service}}")
    return result.returncode == 0 and service in result.stdout.split()


def _require_docker_host() -> None:
    """Gate for assertions that inspect the Docker daemon itself.

    These skip inside the container — not to dodge the check, but because the
    assertion is not expressible there: a container has no Docker CLI and no
    view of networks or port bindings. They still run, on the host, and the
    skip reason says so. The egress proof below never skips.
    """
    if in_container():
        pytest.skip(
            "inspects the Docker daemon; run from the host with "
            "`pytest tests/test_network_isolation.py`"
        )
    if not _docker_available():
        pytest.fail("Docker is unavailable, so the topology cannot be verified.")


def _require_context() -> str:
    """Return the verification context, or fail. Never skips."""
    if in_container():
        return "container"
    if not _docker_available():
        pytest.fail(
            "Cannot verify network isolation: not running inside a container and "
            "Docker is unavailable.\n"
            "Run the suite the documented way:\n"
            "    docker compose up -d\n"
            "    docker compose exec backend pytest\n"
            "This test does not skip — an unverified seal is not a passing seal."
        )
    if not _service_running("backend"):
        pytest.fail(
            "Cannot verify network isolation: the backend container is not running.\n"
            "Start the stack first:\n"
            "    docker compose up -d --no-deps backend gateway\n"
            "This test does not skip — an unverified seal is not a passing seal."
        )
    return "host"


# --------------------------------------------------------------------------
# The sealed container
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("host", "port"), TCP_TARGETS)
def test_no_outbound_tcp(host: str, port: int) -> None:
    if _require_context() == "container":
        assert not _tcp_reachable(host, port), (
            f"Opened a TCP connection to {host}:{port} from inside the sealed "
            f"network. The seal is broken."
        )
        return

    result = _compose(
        "exec", "-T", "backend", "python", "-c",
        f"import socket,sys\n"
        f"try:\n"
        f"    socket.create_connection(({host!r},{port}), {TIMEOUT})\n"
        f"    sys.exit(1)\n"
        f"except OSError:\n"
        f"    sys.exit(0)\n",
    )
    assert result.returncode == 0, (
        f"backend container reached {host}:{port} — the seal is broken.\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.parametrize("name", DNS_NAMES)
def test_no_external_dns(name: str) -> None:
    """DNS is the subtler channel: a resolver that answers is an exfiltration path."""
    if _require_context() == "container":
        assert not _dns_resolves(name), (
            f"Resolved {name} from inside the sealed network. Even without a "
            f"route, a working resolver is a data channel."
        )
        return

    result = _compose(
        "exec", "-T", "backend", "python", "-c",
        f"import socket,sys\n"
        f"try:\n"
        f"    socket.getaddrinfo({name!r}, None)\n"
        f"    sys.exit(1)\n"
        f"except OSError:\n"
        f"    sys.exit(0)\n",
    )
    assert result.returncode == 0, (
        f"backend container resolved {name} — the seal is broken.\n"
        f"{result.stdout}{result.stderr}"
    )


def test_backend_has_no_default_route() -> None:
    """The structural property beneath the behavioural ones.

    `internal: true` removes the default gateway. Without it there is nothing
    to route through, which is why the checks above fail the way they do.
    """
    if _require_context() == "container":
        routes = Path("/proc/net/route").read_text()
        defaults = [
            line for line in routes.splitlines()[1:] if line.split()[1] == "00000000"
        ]
        assert not defaults, f"A default route exists: {defaults}"
        return

    result = _compose(
        "exec", "-T", "backend", "python", "-c",
        "print(open('/proc/net/route').read())",
    )
    assert result.returncode == 0, result.stderr
    defaults = [
        line for line in result.stdout.splitlines()[1:]
        if line.split() and len(line.split()) > 1 and line.split()[1] == "00000000"
    ]
    assert not defaults, f"backend has a default route: {defaults}"


def test_egress_check_module_agrees() -> None:
    """The operator-facing command and this suite must not disagree."""
    if _require_context() == "container":
        from app import egress_check

        assert egress_check.main() == 0
        return

    result = _compose("exec", "-T", "backend", "python", "-m", "app.egress_check")
    assert result.returncode == 0, f"app.egress_check reported a leak:\n{result.stdout}"
    assert "SEALED" in result.stdout


# --------------------------------------------------------------------------
# Topology
# --------------------------------------------------------------------------


def test_sealed_network_is_internal() -> None:
    _require_docker_host()
    result = subprocess.run(
        ["docker", "network", "inspect", "crowdsight_sealed", "--format", "{{.Internal}}"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, "crowdsight_sealed does not exist — is the stack up?"
    assert result.stdout.strip() == "true", "crowdsight_sealed is not an internal network"


def test_backend_publishes_no_ports() -> None:
    """Publishing a port from the backend would mean giving it a route out."""
    _require_docker_host()
    # `docker port` on a container that does not exist also prints nothing, so
    # without this the test passes with the whole stack down — asserting
    # nothing while looking green.
    assert _service_running("backend"), (
        "backend is not running, so 'publishes no ports' cannot be verified. "
        "Start it with `docker compose up -d --no-deps backend gateway`."
    )
    result = subprocess.run(
        ["docker", "port", "crowdsight-backend"], capture_output=True, text=True, timeout=30
    )
    assert result.stdout.strip() == "", (
        f"backend publishes ports: {result.stdout!r}. Only the gateway may."
    )


# --------------------------------------------------------------------------
# The gateway — outside the seal, and hardened rather than guaranteed
# --------------------------------------------------------------------------


def test_gateway_has_no_outbound_tcp() -> None:
    """The gateway is the acknowledged boundary, but masquerade is disabled.

    Without NAT its outbound packets have no return path, so connections fail.
    This catches a regression where someone re-enables masquerade on `edge`.

    Deliberately NOT asserted: Docker's embedded resolver still answers
    external DNS queries on non-internal networks, so the gateway *can* resolve
    names. That residual channel is documented in docker-compose.yml and
    README.md rather than papered over here. The absolute guarantee lives on
    the sealed network, which the tests above cover.
    """
    _require_docker_host()
    if not _service_running("gateway"):
        pytest.fail("gateway is not running; start it with `docker compose up -d gateway`")

    result = _compose(
        "exec", "-T", "gateway", "sh", "-c",
        "timeout 6 wget -q -O- -T4 http://1.1.1.1/ >/dev/null 2>&1 && echo REACHED || echo blocked",
        timeout=60,
    )
    assert "REACHED" not in result.stdout, (
        "The gateway opened an outbound connection. IP masquerade has likely "
        "been re-enabled on the `edge` network."
    )
