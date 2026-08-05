"""Prove, from inside a container, that there is no route off-host.

Run it as::

    docker compose exec backend python -m app.egress_check

Exit status 0 means every attempt to leave the network failed, which is the
desired outcome. Exit status 1 means something got out — treat that as a
release blocker, not a warning.

This is the operator-facing command. Phase 10's ``test_egress_verification.py``
is the automated gate and asserts the same properties.
"""

from __future__ import annotations

import socket
import sys

# Deliberately boring, highly available destinations. If any of these is
# reachable, the seal is broken — the specific host does not matter.
TCP_TARGETS = [
    ("1.1.1.1", 443),
    ("8.8.8.8", 53),
    ("93.184.216.34", 80),
]
DNS_NAMES = ["pypi.org", "github.com", "ollama.com"]

TIMEOUT = 4.0


def _tcp_blocked(host: str, port: int) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return False, "CONNECTED — the seal is broken"
    except OSError as exc:
        return True, f"blocked ({exc.__class__.__name__}: {exc})"


def _dns_blocked(name: str) -> tuple[bool, str]:
    try:
        resolved = socket.getaddrinfo(name, None)
        addrs = sorted({item[4][0] for item in resolved})
        return False, f"RESOLVED to {', '.join(addrs)} — the seal is broken"
    except OSError as exc:
        return True, f"blocked ({exc.__class__.__name__})"


def main() -> int:
    print(f"Egress check from {socket.gethostname()}\n")
    sealed = True

    print("TCP:")
    for host, port in TCP_TARGETS:
        ok, detail = _tcp_blocked(host, port)
        sealed &= ok
        print(f"  {'PASS' if ok else 'FAIL'}  {host}:{port} — {detail}")

    print("\nDNS:")
    for name in DNS_NAMES:
        ok, detail = _dns_blocked(name)
        sealed &= ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name} — {detail}")

    if sealed:
        print("\nSEALED: every attempt to leave the network failed.")
        return 0
    print("\nNOT SEALED: at least one destination was reachable. Do not run "
          "a simulation until this is fixed.")
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
