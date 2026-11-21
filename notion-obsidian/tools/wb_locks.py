#!/usr/bin/env python3
"""Process locks on state/<name>.lock (IMPL_CONTRACT §5.5).

    with wb_locks.lock("writeback"):               # non-blocking; raises LockBusy
        with wb_locks.lock("refresh"):             # same file and flock as refresh.py's lock
            ...
    with wb_locks.lock_all(["writeback", "refresh"]):   # both, in a fixed order

Uses fcntl.flock(LOCK_EX), exactly like refresh.py (`open(state/refresh.lock, "w")` + flock), so a
lock held here excludes refresh.py and vice versa. flock locks belong to the open file, so a second
lock() on the same name in the same process is also refused.
"""
import contextlib
import fcntl
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import wb_common  # noqa: E402

LOCK_NAMES = ("refresh", "writeback")
_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_MESSAGES = {
    "refresh": "refresh 正在运行，稍后重试",
    "writeback": "另一个写回进程正在运行，稍后重试",
}


class LockBusy(RuntimeError):
    def __init__(self, name, path):
        super().__init__(f"{_MESSAGES.get(name, name + ' 锁被占用，稍后重试')}（{path}）")
        self.name = name
        self.path = path


class Held:
    def __init__(self, name, path, fd):
        self.name, self.path, self.fd = name, path, fd

    def __repr__(self):
        return f"<lock {self.name} {self.path}>"


def lock_path(name, state_dir=None) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError(f"invalid lock name: {name!r}")
    return os.path.join(state_dir or wb_common.STATE, f"{name}.lock")


@contextlib.contextmanager
def lock(name: str, blocking=False, *, state_dir=None):
    """Exclusive flock on <state_dir or state>/<name>.lock. Raises LockBusy if not blocking and held."""
    p = lock_path(name, state_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd = os.open(p, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o660)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise LockBusy(name, p) from None
        try:
            yield Held(name, p, fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def lock_all(names, blocking=False, *, state_dir=None):
    """Acquire several locks in LOCK_NAMES order (then alphabetical); all or none."""
    order = sorted(set(names), key=lambda n: (LOCK_NAMES.index(n) if n in LOCK_NAMES else len(LOCK_NAMES), n))
    with contextlib.ExitStack() as st:
        held = [st.enter_context(lock(n, blocking, state_dir=state_dir)) for n in order]
        yield held
