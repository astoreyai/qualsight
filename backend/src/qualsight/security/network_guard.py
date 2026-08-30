"""Runtime network guard: refuse every non-loopback socket connection from this process (PRD §48; QS-H70).

QualSight's scientific runtime never needs the network: engines, matchers, and
analyses read registered local bytes. Installation or an explicit operator
action may acquire pinned architecture sources. Environment-driven application
startup installs the guard by default; only the thread performing that explicit
acquisition enters the bounded exception. Every other non-loopback connection
raises ``NetworkGuardViolation`` and is counted for operator inspection.
"""

from __future__ import annotations

from contextlib import contextmanager
import ipaddress
import socket
import threading
from typing import Any, Iterator

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "installed": False,
    "attempts": [],
    "authorized_setup_connections": 0,
    "original_connect": None,
    "original_connect_ex": None,
    "original_sendto": None,
    "original_sendmsg": None,
}
_SETUP_SCOPE = threading.local()


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
        original_sendto = socket.socket.sendto
        original_sendmsg = getattr(socket.socket, "sendmsg", None)

        def admit_or_refuse(address: Any) -> bool:
            if _is_loopback(address):
                return True
            if getattr(_SETUP_SCOPE, "depth", 0) > 0:
                with _LOCK:
                    _STATE["authorized_setup_connections"] += 1
                return True
            with _LOCK:
                _STATE["attempts"].append(repr(address))
            raise NetworkGuardViolation(f"network guard: refused connection to {address!r}")

        def guarded_connect(self: socket.socket, address: Any) -> None:
            admit_or_refuse(address)
            return original_connect(self, address)

        def guarded_connect_ex(self: socket.socket, address: Any) -> int:
            admit_or_refuse(address)
            return original_connect_ex(self, address)

        def guarded_sendto(self: socket.socket, data: Any, *args: Any) -> int:
            if not args:
                raise TypeError("sendto expected a destination address")
            admit_or_refuse(args[-1])
            return original_sendto(self, data, *args)

        def guarded_sendmsg(
            self: socket.socket,
            buffers: Any,
            ancdata: Any = (),
            flags: int = 0,
            address: Any = None,
        ) -> int:
            if address is not None:
                admit_or_refuse(address)
                return original_sendmsg(self, buffers, ancdata, flags, address)
            return original_sendmsg(self, buffers, ancdata, flags)

        socket.socket.connect = guarded_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
        socket.socket.sendto = guarded_sendto  # type: ignore[method-assign]
        if original_sendmsg is not None:
            socket.socket.sendmsg = guarded_sendmsg  # type: ignore[attr-defined,method-assign]
        _STATE.update(
            {
                "installed": True,
                "original_connect": original_connect,
                "original_connect_ex": original_connect_ex,
                "original_sendto": original_sendto,
                "original_sendmsg": original_sendmsg,
            }
        )


def guard_status() -> dict[str, Any]:
    with _LOCK:
        return {
            "installed": bool(_STATE["installed"]),
            "refused_attempts": list(_STATE["attempts"]),
            "authorized_setup_connections": int(_STATE["authorized_setup_connections"]),
        }


@contextmanager
def permit_explicit_setup_network() -> Iterator[None]:
    """Permit network only on the thread executing an explicit pinned-source pull."""

    depth = getattr(_SETUP_SCOPE, "depth", 0)
    _SETUP_SCOPE.depth = depth + 1
    try:
        yield
    finally:
        _SETUP_SCOPE.depth = depth
