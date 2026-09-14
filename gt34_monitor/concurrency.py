from __future__ import annotations

import hashlib
import os
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import BinaryIO

try:
    import msvcrt
except ImportError:  # pragma: no cover - Windows production only
    msvcrt = None


class ConcurrencyAcquireTimeout(TimeoutError):
    pass


def _safe_host_token(host: str) -> str:
    digest = hashlib.sha256(host.encode("utf-8")).hexdigest()[:12]
    readable = "".join(ch if ch.isalnum() else "_" for ch in host)[:48]
    return f"{readable}_{digest}"


class PerHostSlotGuard(AbstractContextManager["PerHostSlotGuard"]):
    """Windows process-safe bounded concurrency guard, scoped by physical host.

    Each slot is represented by a one-byte file lock. Windows releases the lock
    automatically when the owning process/file handle terminates.
    """

    def __init__(
        self,
        host: str,
        state_dir: Path,
        *,
        slots: int = 4,
        timeout_seconds: float = 20.0,
        retry_seconds: float = 0.05,
    ) -> None:
        if not host:
            raise ValueError("host is required")
        if slots < 1:
            raise ValueError("slots must be >= 1")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if retry_seconds <= 0:
            raise ValueError("retry_seconds must be > 0")
        self.host = host
        self.state_dir = Path(state_dir)
        self.slots = int(slots)
        self.timeout_seconds = float(timeout_seconds)
        self.retry_seconds = float(retry_seconds)
        self._fh: BinaryIO | None = None
        self.slot: int | None = None

    @property
    def lock_dir(self) -> Path:
        return self.state_dir / "prtg_concurrency" / _safe_host_token(self.host)

    def _try_slot(self, slot: int) -> BinaryIO | None:
        if msvcrt is None:
            raise RuntimeError("PerHostSlotGuard requires Windows msvcrt")
        path = self.lock_dir / f"slot_{slot}.lock"
        fh = path.open("a+b")
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell() < 1:
                fh.write(b"0")
                fh.flush()
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fh.close()
                return None
            return fh
        except Exception:
            fh.close()
            raise

    def acquire(self) -> "PerHostSlotGuard":
        if self._fh is not None:
            return self
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            for slot in range(self.slots):
                fh = self._try_slot(slot)
                if fh is not None:
                    self._fh = fh
                    self.slot = slot
                    return self
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConcurrencyAcquireTimeout(
                    f"Timed out after {self.timeout_seconds:.1f}s waiting for one of "
                    f"{self.slots} polling slots for IRD {self.host}"
                )
            time.sleep(min(self.retry_seconds, remaining))

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        self.slot = None
        if fh is None:
            return
        try:
            fh.seek(0)
            if msvcrt is not None:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            fh.close()

    def __enter__(self) -> "PerHostSlotGuard":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False
