"""Bounded admission for local scientific background work."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import threading
from typing import Any, Callable, TypeVar


Result = TypeVar("Result")


class ExecutorOverloaded(RuntimeError):
    """Raised when every worker and pending slot is already reserved."""


class ExecutorReservation:
    """One capacity slot that can be submitted once or released without work."""

    def __init__(self, executor: "BoundedExecutor") -> None:
        self._executor = executor
        self._lock = threading.Lock()
        self._active = True
        self._submitted = False

    def submit(self, function: Callable[..., Result], /, *args: Any, **kwargs: Any) -> Future[Result]:
        with self._lock:
            if not self._active or self._submitted:
                raise RuntimeError("executor reservation is not available for submission")
            self._submitted = True
        return self._executor._submit_reserved(self, function, *args, **kwargs)

    def release(self) -> None:
        with self._lock:
            if not self._active:
                return
            self._active = False
        self._executor._release_slot()


class BoundedExecutor:
    """A ``ThreadPoolExecutor`` with a nonblocking bound over running and pending work."""

    def __init__(self, *, max_workers: int, max_pending: int, thread_name_prefix: str) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least one")
        if max_pending < 0:
            raise ValueError("max_pending cannot be negative")
        self._capacity = max_workers + max_pending
        self._slots = threading.BoundedSemaphore(self._capacity)
        self._state_lock = threading.Lock()
        self._idle = threading.Condition(self._state_lock)
        self._in_flight = 0
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )

    def reserve(self) -> ExecutorReservation:
        if not self._slots.acquire(blocking=False):
            raise ExecutorOverloaded(
                f"scientific work capacity is exhausted ({self._capacity} running-or-pending slots)"
            )
        with self._state_lock:
            self._in_flight += 1
        return ExecutorReservation(self)

    def _release_slot(self) -> None:
        with self._idle:
            self._in_flight -= 1
            if self._in_flight == 0:
                self._idle.notify_all()
        self._slots.release()

    def _submit_reserved(
        self,
        reservation: ExecutorReservation,
        function: Callable[..., Result],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[Result]:

        def invoke() -> Result:
            try:
                return function(*args, **kwargs)
            finally:
                reservation.release()

        try:
            future = self._executor.submit(invoke)
        except BaseException:
            reservation.release()
            raise
        future.add_done_callback(lambda completed: reservation.release() if completed.cancelled() else None)
        return future

    def submit(self, function: Callable[..., Result], /, *args: Any, **kwargs: Any) -> Future[Result]:
        return self.reserve().submit(function, *args, **kwargs)

    def status(self) -> dict[str, int]:
        with self._state_lock:
            in_flight = self._in_flight
        return {
            "capacity": self._capacity,
            "in_flight": in_flight,
            "available": self._capacity - in_flight,
        }

    def wait_for_idle(self, *, timeout_seconds: float) -> bool:
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds < 0
        ):
            raise ValueError("idle wait timeout cannot be negative")
        with self._idle:
            return self._idle.wait_for(
                lambda: self._in_flight == 0,
                timeout=float(timeout_seconds),
            )

    def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
