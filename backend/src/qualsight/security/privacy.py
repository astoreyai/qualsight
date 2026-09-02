"""Fail-closed projection of private local diagnostics at browser boundaries."""

from __future__ import annotations

import re
from urllib.parse import unquote


_PRIVATE_PATH_PATTERNS = (
    re.compile(r"(?i)\bfile:(?:/{2,3}|\\\\)[^\s\"'<>|,;)\]}]+"),
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|,;)\]}]+"),
    re.compile(r"\\\\[^\s\\/]+[\\/][^\s\"'<>|,;)\]}]+"),
    re.compile(r"(?<![:/])//[^\s/]+/[^\s\"'<>|,;)\]}]+"),
    re.compile(r"(?<![A-Za-z0-9])~[\\/][^\s\"'<>|,;)\]}]+"),
    re.compile(
        r"(?<![A-Za-z0-9:/\]])/(?!/|api(?:/|$))[^\s\"'<>|,;)\]}]+"
    ),
    re.compile(r"(?<![A-Za-z0-9])\.\.?[\\/][^\s\"'<>|,;)\]}]+"),
)

_NATIVE_ERROR_PATTERN = re.compile(
    r"(?:^|\b)(?:"
    r"OSError|IOError|FileNotFoundError|FileExistsError|PermissionError|"
    r"NotADirectoryError|IsADirectoryError|BlockingIOError|TimeoutError|"
    r"OperationalError|ProgrammingError|DatabaseError|IntegrityError"
    r"):|\[Errno\s+\d+\]",
    re.IGNORECASE,
)

_PUBLIC_LOCATOR_PATTERN = re.compile(
    r"(?i)\bhttps?://[^\s\"'<>?#]+|(?<![A-Za-z0-9])/(?:api(?:/[^\s\"'<>]*)?)"
)

_SAFE_API_LOCATOR_PATTERN = re.compile(
    r"/api(?:/[A-Za-z0-9._~:@+{}%-]+)*"
)

_ENCODED_SEPARATOR_PATTERN = re.compile(r"%(?:25)*(?:2f|5c)", re.IGNORECASE)

_ENCODED_PRIVATE_PATH_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9%])(?:"
    r"%(?:25)*2f(?!%(?:25)*2f|api(?:%(?:25)*2f|$))|"
    r"[A-Za-z]%(?:25)*3a%(?:25)*(?:2f|5c)|"
    r"%(?:25)*7e%(?:25)*(?:2f|5c)|"
    r"file%(?:25)*3a%(?:25)*(?:2f|5c)"
    r")(?:%[0-9A-Fa-f]{2}|[A-Za-z0-9._~!$'()*+,;=:@-])+"
)


def redact_private_paths(value: str, replacement: str = "recorded local resource") -> str:
    """Replace path-shaped tokens while retaining non-path diagnostic meaning."""

    protected: list[str] = []

    def protect(match: re.Match[str]) -> str:
        locator = match.group(0)
        if locator.startswith("/api"):
            decoded = unquote(locator)
            segments = decoded.split("/")[1:]
            if (
                _ENCODED_SEPARATOR_PATTERN.search(locator)
                or not _SAFE_API_LOCATOR_PATTERN.fullmatch(decoded)
                or any(segment in {".", ".."} for segment in segments)
            ):
                return locator
        protected.append(locator)
        return f"\x00QUALSIGHT_PUBLIC_LOCATOR_{len(protected) - 1}\x00"

    redacted = _PUBLIC_LOCATOR_PATTERN.sub(protect, value)
    redacted = _ENCODED_PRIVATE_PATH_PATTERN.sub(replacement, redacted)
    for pattern in _PRIVATE_PATH_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    for index, locator in enumerate(protected):
        redacted = redacted.replace(
            f"\x00QUALSIGHT_PUBLIC_LOCATOR_{index}\x00",
            locator,
        )
    return redacted


def public_diagnostic(value: str) -> str:
    """Return useful non-native text or one stable private-evidence marker."""

    if _NATIVE_ERROR_PATTERN.search(value):
        return "private local diagnostic recorded"
    return redact_private_paths(value)
