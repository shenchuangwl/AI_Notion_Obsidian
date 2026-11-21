#!/usr/bin/env python3
"""Cross-process Notion request rate gate (WRITEBACK_SPEC v1.1 §11.2, H7; decision D-16).

One token bucket shared by every process on this machine that talks to the Notion API
(snapshot, assets_fetch, writeback reads and writes). Its state lives in a small JSON file,
by default ``<WORK>/state/rate_gate.json``; every read-modify-write of that file happens under an
exclusive ``fcntl.flock``. Each ``acquire()`` opens its own descriptor, so the lock also excludes
threads of the same process and children created with ``fork`` (which would otherwise share
an open file description and therefore a flock).

* ``total_rps`` (default 2.5 = config ``rate_limit_total_rps``) is the combined rate for all
  processes; ``burst`` (default 3) is the bucket capacity.
* ``cap_rps`` is an optional extra per-process cap (e.g. snapshot ``--rate``, or 1.0 for the
  write-back while refresh is running). It can only lower the rate, never raise it above
  ``total_rps``.
* ``pause(seconds)`` sets a global pause (429/529 ``Retry-After``,
  ``public_api_space_request_rate_limit``): every process's ``acquire()`` blocks until it ends.
  Pauses only ever extend (max of the old and new deadline) and are clamped to ``MAX_PAUSE``.
  During a pause the bucket is drained, so traffic restarts at the steady rate, not in a burst.

The gate is a drop-in replacement for ``nmig.client.RateLimiter``: assign it to
``NotionClient.limiter`` (``acquire()`` is the only method the client calls).

Robustness:

* only a state file this very call created (``O_CREAT|O_EXCL``) starts as a full bucket, like
  ``nmig`` ``RateLimiter``; an existing file that is empty or corrupt is an EMPTY bucket
  (conservative);
* a write is ``pwrite`` of the new JSON at offset 0 followed by ``ftruncate`` to its length, so the
  file is never empty after creation; a crash between the two leaves the new JSON followed by
  stale bytes, and the reader parses the leading JSON object only (``raw_decode``), so the state
  -- including a global pause -- survives;
* each ``RateGate`` also remembers the latest pause deadline it has set or seen and re-applies it
  if the file lost it, so a damaged file never shortens a pause this process knows about;
* wall-clock jumps backwards are absorbed; a pause deadline further than ``MAX_PAUSE`` in the
  future (clock jump) is clamped. The state file never contains tokens or request data.
"""
import fcntl
import json
import math
import os
import threading
import time

WORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(WORK, "state", "rate_gate.json")
DEFAULT_TOTAL_RPS = 2.5


class RateGate:
    MAX_PAUSE = 600.0     # seconds; longer Retry-After values are clamped
    MAX_SLEEP = 2.0       # re-check the shared state at least this often while waiting

    def __init__(self, path=None, total_rps=DEFAULT_TOTAL_RPS, burst=3, cap_rps=None, *,
                 clock=time.time, sleep=time.sleep):
        total_rps = float(total_rps)
        if not (total_rps > 0 and math.isfinite(total_rps)):
            raise ValueError("total_rps must be a positive number")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self.path = path or DEFAULT_PATH
        self.total_rps = total_rps
        self.burst = float(burst)
        if cap_rps is not None:
            cap_rps = float(cap_rps)
            if not (cap_rps > 0 and math.isfinite(cap_rps)):
                raise ValueError("cap_rps must be a positive number")
            cap_rps = min(cap_rps, total_rps)
        self.cap_rps = cap_rps
        self._clock, self._sleep = clock, sleep
        self._cap_lock = threading.Lock()
        self._cap_tokens, self._cap_last = self.burst, None
        self._pause_seen = 0.0        # latest pause deadline this instance set or saw
        self.stats = {"acquired": 0, "waited_s": 0.0, "pauses_set": 0}

    def __repr__(self):
        return f"<RateGate {self.path} total={self.total_rps}/s burst={self.burst:g} cap={self.cap_rps}>"

    # ------------------------------------------------------------------ file state
    def _open(self):
        """Open + exclusively lock the state file. Returns (fd, created_now)."""
        d = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(d, exist_ok=True)
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        for _ in range(3):
            try:
                fd, created = os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o600), True
                break
            except FileExistsError:
                pass
            try:
                fd, created = os.open(self.path, flags), False
                break
            except FileNotFoundError:     # removed between the two opens: try again
                continue
        else:   # e.g. a dangling symlink (EXCL says exists, plain open says missing): old behaviour,
            fd, created = os.open(self.path, flags | os.O_CREAT, 0o600), False   # conservative bucket
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd, created

    @staticmethod
    def _read(fd, now, created):
        try:
            raw = os.pread(fd, 65536, 0)
        except OSError:
            raw = b""
        conservative = {"tokens": 0.0, "last": now, "pause_until": 0.0}
        if not raw:
            # only a file created by this very open is "first use"; an existing empty file was
            # damaged (e.g. truncated by a crash) -> empty bucket
            return None if created else conservative
        try:
            st, _end = json.JSONDecoder().raw_decode(raw.decode("utf-8", "replace"))   # stale tail ignored
            vals = {k: float(st[k]) for k in ("tokens", "last", "pause_until")}
            if not all(math.isfinite(v) for v in vals.values()):
                raise ValueError
            return vals
        except (ValueError, KeyError, TypeError):
            return conservative   # corrupt -> conservative

    @staticmethod
    def _write(fd, st):
        data = json.dumps({"v": 1, "tokens": round(st["tokens"], 6), "last": st["last"],
                           "pause_until": st["pause_until"]}, sort_keys=True).encode("utf-8")
        os.pwrite(fd, data, 0)          # never leaves an empty file (see module docstring)
        os.ftruncate(fd, len(data))

    def _state_locked(self, fd, now, created=False):
        st = self._read(fd, now, created)
        if st is None:   # first use: a full bucket, like nmig RateLimiter
            st = {"tokens": self.burst, "last": now, "pause_until": 0.0}
        if st["last"] > now:          # wall clock went backwards
            st["last"] = now
        st["pause_until"] = max(st["pause_until"], self._pause_seen)   # a damaged file never shortens a pause
        if st["pause_until"] - now > self.MAX_PAUSE:   # clock jump / garbage
            st["pause_until"] = now + self.MAX_PAUSE
        self._pause_seen = st["pause_until"]
        st["tokens"] = min(self.burst, max(0.0, st["tokens"]) + (now - st["last"]) * self.total_rps)
        st["last"] = now
        return st

    def _try_take(self):
        """Take one token from the shared bucket. Returns 0.0 on success, else seconds to wait."""
        fd, created = self._open()
        try:
            now = self._clock()
            st = self._state_locked(fd, now, created)
            if st["pause_until"] > now:
                st["tokens"] = 0.0
                self._write(fd, st)
                return st["pause_until"] - now
            if st["tokens"] >= 1.0:
                st["tokens"] -= 1.0
                self._write(fd, st)
                return 0.0
            self._write(fd, st)
            return (1.0 - st["tokens"]) / self.total_rps
        finally:
            os.close(fd)   # closing the descriptor releases the flock

    # ------------------------------------------------------------------ per-process cap
    def _take_cap(self):
        while True:
            with self._cap_lock:
                now = self._clock()
                if self._cap_last is None or self._cap_last > now:
                    self._cap_last = now
                self._cap_tokens = min(self.burst, self._cap_tokens + (now - self._cap_last) * self.cap_rps)
                self._cap_last = now
                if self._cap_tokens >= 1.0:
                    self._cap_tokens -= 1.0
                    return
                wait = (1.0 - self._cap_tokens) / self.cap_rps
            self.stats["waited_s"] += wait
            self._sleep(wait)

    # ------------------------------------------------------------------ public API
    def acquire(self) -> None:
        """Block until this process may send one request."""
        if self.cap_rps is not None:
            self._take_cap()
        while True:
            wait = self._try_take()
            if wait <= 0:
                self.stats["acquired"] += 1
                return
            wait = min(wait, self.MAX_SLEEP)
            self.stats["waited_s"] += wait
            self._sleep(wait)

    def pause(self, seconds: float) -> None:
        """Global pause for every process using this state file (never shortens a pause)."""
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return
        if not math.isfinite(seconds) or seconds <= 0:
            return
        seconds = min(seconds, self.MAX_PAUSE)
        fd, created = self._open()
        try:
            now = self._clock()
            st = self._state_locked(fd, now, created)
            st["pause_until"] = max(st["pause_until"], now + seconds)
            self._pause_seen = st["pause_until"]
            st["tokens"] = 0.0
            self._write(fd, st)
            self.stats["pauses_set"] += 1
        finally:
            os.close(fd)

    def state(self) -> dict:
        """Current shared state (refilled to 'now'); for diagnostics and tests. Does not consume."""
        fd, created = self._open()
        try:
            now = self._clock()
            st = self._state_locked(fd, now, created)
            self._write(fd, st)
            return {"tokens": st["tokens"], "pause_until": st["pause_until"],
                    "paused_for": max(0.0, st["pause_until"] - now), "now": now}
        finally:
            os.close(fd)
