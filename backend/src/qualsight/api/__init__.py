"""FastAPI control plane for the local QualSight console.

``create_app`` is imported lazily so that ``qualsight.api.store`` can be used
by the domain services (projects, datasets, reproduction) without pulling the
FastAPI application (which imports those services) into the import cycle.
"""

from __future__ import annotations

from typing import Any

from .settings import Settings

__all__ = ["Settings", "create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(name)
