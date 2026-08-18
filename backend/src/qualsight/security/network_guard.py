"""Runtime network guard: refuse every non-loopback socket connection from this process (PRD §48; QS-H70).

QualSight's runtime never needs the network: engines, matchers, and analyses
read registered local bytes. Installation-time acquisition of pinned upstream
sources (``qualsight.matchers.upstream``) is the one sanctioned outbound path,
and it runs before the guard is installed. With ``QUALSIGHT_NETWORK_GUARD=1``
the guard is installed at application start-up, and any attempt to connect a
socket to a non-loopback address raises ``NetworkGuardViolation``; the
attempts are counted so the offline gate can report them.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from typing import Any

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"installed": False, "attempts": [], "original_connect": None, "original_connect_ex": None}


class NetworkGuardViolation(PermissionError):
    """Raised when the runtime attempts a non-loopback connection under the network guard."""


def _is_loopback(address: Any) -> bool:
    if isinstance(address, str):
        return True  # AF_UNIX path
    if isinstance(address, (tuple, list)) and address:
        host = address[0]
        if host in ("localhost", ""):
            return True
        try:
            return ipaddress.ip_address(host.split("%")[0]).is_loopback
        except ValueError:
            return False
    return False


def install_network_guard() -> None:
    with _LOCK:
        if _STATE["installed"]:
            return
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex

        def guarded_connect(self: socket.socket, address: Any) -> None:
            if not _is_loopback(address):
                _STATE["attempts"].append(repr(address))
                raise NetworkGuardViolation(f"network guard: refused connection to {address!r}")
            return original_connect(self, address)

        def guarded_connect_ex(self: socket.socket, address: Any) -> int:
            if not _is_loopback(address):
                _STATE["attempts"].append(repr(address))
                raise NetworkGuardViolation(f"network guard: refused connection to {address!r}")
            return original_connect_ex(self, address)

        socket.socket.connect = guarded_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
        _STATE.update({"installed": True, "original_connect": original_connect, "original_connect_ex": original_connect_ex})


def guard_status() -> dict[str, Any]:
    with _LOCK:
        return {"installed": bool(_STATE["installed"]), "refused_attempts": list(_STATE["attempts"])}
