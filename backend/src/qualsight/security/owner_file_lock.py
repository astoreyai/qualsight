"""Process-wide serialization for owner-supplied file references and deletion.

Lock order is strict: API mutation coordination completes its short check
before this lock is acquired; this lock is then acquired before
``RunStore._lock``. No store method may acquire this lock while already holding
``RunStore._lock``. The lock is re-entrant so a collection import can retain it
while individual admissions and their store writes use the same boundary.

This protects one QualSight Python process. Multiple server worker processes
would require an inter-process lock and are not admitted by the local beta.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import threading


_OWNER_FILE_MUTATION_LOCK = threading.RLock()


@contextmanager
def serialized_owner_file_mutation() -> Iterator[None]:
    """Hold the process-wide owner-file mutation boundary."""

    with _OWNER_FILE_MUTATION_LOCK:
        yield
