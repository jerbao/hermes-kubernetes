"""Emit secret-safe machine-readable backup progress to container stderr."""

import json
import re
import sys
import threading
import time

_RCLONE_STATS = re.compile(
    r"^Transferred:\s*(?P<done>[0-9.]+\s+\w+)\s*/\s*"
    r"(?P<total>[0-9.]+\s+\w+),\s*(?P<percent>[0-9.]+)%,\s*"
    r"(?P<speed>[0-9.]+\s+\w+/s),\s*ETA\s+(?P<eta>[0-9hms]+)$"
)


class ProgressReporter:
    """Write aggregate-only JSON progress lines and periodic heartbeats."""

    def __init__(self, *, stream=None, interval=30):
        self.stream = stream if stream is not None else sys.stderr
        self.interval = interval
        self.started = time.monotonic()
        self.phase_name = "starting"
        self.entries_scanned = 0
        self.entries_processed = 0
        self.entries_total = None
        self.files_processed = 0
        self.files_total = None
        self.bytes_processed = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.emit()
        self._thread.start()

    def _payload(self, event="progress", **fields):
        return {
            "event": event,
            "phase": self.phase_name,
            "elapsed_seconds": round(time.monotonic() - self.started, 1),
            "entries_scanned": self.entries_scanned,
            "entries_processed": self.entries_processed,
            "entries_total": self.entries_total,
            "files_processed": self.files_processed,
            "files_total": self.files_total,
            "bytes_processed": self.bytes_processed,
            **fields,
        }

    def emit(self, event="progress", **fields):
        with self._lock:
            print(
                json.dumps(self._payload(event, **fields), separators=(",", ":")),
                file=self.stream,
                flush=True,
            )

    def phase(self, phase_name, *, reset_counters=False, **fields):
        with self._lock:
            if reset_counters:
                self.entries_processed = 0
                self.entries_total = None
                self.files_processed = 0
                self.files_total = None
                self.bytes_processed = 0
            self.phase_name = phase_name
            print(
                json.dumps(self._payload(**fields), separators=(",", ":")),
                file=self.stream,
                flush=True,
            )

    def scanned_entry(self):
        with self._lock:
            self.entries_scanned += 1

    def scan_complete(self, entries):
        with self._lock:
            self.entries_scanned = entries
            self.entries_total = entries
            print(json.dumps(self._payload(), separators=(",", ":")), file=self.stream, flush=True)

    def start_capture(self, entries, files):
        with self._lock:
            self.entries_processed = 0
            self.entries_total = entries
            self.files_processed = 0
            self.files_total = files
            self.bytes_processed = 0
            self.phase_name = "capture"
            print(json.dumps(self._payload(), separators=(",", ":")), file=self.stream, flush=True)

    def entry_processed(self):
        with self._lock:
            self.entries_processed += 1

    def file_processed(self):
        with self._lock:
            self.files_processed += 1

    def bytes_read(self, count):
        with self._lock:
            self.bytes_processed += count

    def transfer_stats(self, line):
        match = _RCLONE_STATS.fullmatch(line.strip())
        if match:
            self.emit("transfer_progress", **match.groupdict())

    def failure(self, reason):
        self.emit("failure", reason=reason)

    def finish(self):
        self.phase("done")

    def _heartbeat(self):
        while not self._stop.wait(self.interval):
            self.emit("heartbeat")

    def close(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1)
