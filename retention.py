"""Keeping a run's outputs long enough to fetch them (#13).

Streaming an output means not having read it into the response, which means the
file has to still exist when the client asks. Today a workspace is removed the
moment perform_run returns, so there is nothing left to stream.

The obvious fix -- stop deleting -- is how a service fills a disk. So retention
is bounded in both the ways that matter: a run's outputs are kept for a fixed
time, and no more than a fixed number of runs keep anything at all. Whichever
bound is hit first wins, and a workspace is removed the moment its run is
forgotten.

The alternative would have been copying outputs somewhere on completion, which
is the same bytes moved twice for no gain: the workspace is already private to
the run and already removed when the run is evicted.
"""

import os
import shutil
import threading
import time

KEEP_OUTPUTS_SECONDS = int(os.getenv("BIOCHEF_KEEP_OUTPUTS", "3600"))
"""How long a finished run's outputs remain fetchable.

An hour by default: long enough that a client which lost its connection can come
back for them, short enough that a busy service does not accumulate a day's work
on disk. Zero disables retention, which restores the old behaviour of removing
the workspace as soon as the run ends.
"""

MAX_RETAINED = int(os.getenv("BIOCHEF_MAX_RETAINED_RUNS", "32"))
"""How many finished runs may be keeping outputs at once.

The time bound alone is not enough. A service asked for a hundred runs in a
minute would honour every one of their hours simultaneously, and the disk does
not care that each was individually reasonable.
"""


class Retained:
    """Workspaces being kept for their outputs, with both bounds enforced.

    A workspace can also be held: its run is waiting for egress, so its clock has
    not started and neither bound applies. A review can take longer than any
    retention setting, and deleting outputs a person is about to release would be
    the worst thing this module could do. Held entries count against the limit,
    and when they fill it `full` says so and no new run is admitted.

    Every method takes the lock. Runs finish on worker threads and are fetched
    from the event loop, so entries are added and removed concurrently by
    construction.
    """

    def __init__(self, keep_seconds=None, max_retained=None):
        self._entries = {}
        self._lock = threading.Lock()
        self._keep = (KEEP_OUTPUTS_SECONDS if keep_seconds is None
                      else keep_seconds)
        self._max = MAX_RETAINED if max_retained is None else max_retained

    @property
    def enabled(self):
        return self._keep > 0 and self._max > 0

    @property
    def full(self):
        """Every slot is held for egress, so a new run has nowhere to keep outputs."""
        with self._lock:
            return (self.enabled and sum(kept_at is None for _, kept_at
                                         in self._entries.values()) >= self._max)

    def keep(self, run_id, ws, now=None, hold=False):
        """Hold a workspace for this run, evicting whatever is over the bounds.

        Returns True if it was kept. When retention is disabled the caller is
        told so and cleans up itself, rather than this quietly holding nothing
        and leaving the directory behind.

        `hold` keeps it with no clock, until `start_clock`.
        """
        if not self.enabled:
            return False
        now = time.time() if now is None else now
        with self._lock:
            self._entries[run_id] = (ws, None if hold else now)
            self._evict(now)
        return True

    def adopt(self, run_id, ws, kept_at):
        """Take back a workspace after a restart. `kept_at` None means held."""
        with self._lock:
            self._entries[run_id] = (ws, kept_at)

    def start_clock(self, run_id, kept_at):
        """Egress is over, so a held workspace starts to age. Idempotent."""
        with self._lock:
            entry = self._entries.get(run_id)
            if entry is not None and entry[1] is None:
                self._entries[run_id] = (entry[0], kept_at)

    def workspace(self, run_id, now=None):
        """The workspace for a run, or None if it is gone or expired.

        Expiry is checked on the way past rather than by a timer. A service with
        no traffic has nothing to clean up, and one with traffic cleans up as it
        goes.
        """
        now = time.time() if now is None else now
        with self._lock:
            self._evict(now)
            entry = self._entries.get(run_id)
            return entry[0] if entry else None

    def open_read(self, run_id, filename, now=None):
        """Open under the retention lock and stream from the handle after unlocking."""
        now = time.time() if now is None else now
        with self._lock:
            self._evict(now)
            entry = self._entries.get(run_id)
            if entry is None:
                return None
            return entry[0].open_read(filename, regular_only=True)

    def release(self, run_id):
        """Remove a run's workspace now, if it is being kept."""
        with self._lock:
            entry = self._entries.pop(run_id, None)
        if entry is not None:
            _remove(entry[0])

    def release_all(self):
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for ws, _ in entries:
            _remove(ws)

    def _evict(self, now):
        """Called with the lock held. Time first, then count. Held entries stay."""
        expired = [run_id for run_id, (_, kept_at) in self._entries.items()
                   if kept_at is not None and now - kept_at >= self._keep]
        for run_id in expired:
            _remove(self._entries.pop(run_id)[0])

        while len(self._entries) > self._max:
            aging = [run_id for run_id, (_, kept_at) in self._entries.items()
                     if kept_at is not None]
            if not aging:
                break
            oldest = min(aging, key=lambda run_id: self._entries[run_id][1])
            _remove(self._entries.pop(oldest)[0])


def _remove(ws):
    """Remove a workspace, tolerating one that has already gone.

    Through the workspace's own cleanup, which compares the inode before
    deleting -- so a directory that has been moved or replaced since is left
    alone rather than taking whatever now occupies the path.
    """
    try:
        ws.cleanup()
    except Exception:                                # noqa: BLE001
        pass
