"""Exclusive per-user account lock, compatible with earlier executors."""
import hashlib
import os
import sys
from contextlib import contextmanager
from pathlib import Path


def lock_directory():
    if os.name == "nt":
        root = Path(os.environ["APPDATA"])
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "nba-executor" / "locks"


@contextmanager
def account_lock(account, directory=None, *, environment="live"):
    root = directory or lock_directory()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = hashlib.sha256((environment + "\0" + account.lower()).encode()).hexdigest()
    with (root / (name + ".lock")).open("a+b") as lock:
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("another executor owns this trading account") from exc
        yield  # Closing the handle releases either platform's OS lock.
