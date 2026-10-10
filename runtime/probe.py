"""Readiness is local gateway health, independent of VM/provider availability."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def healthy(record, now=None):
    if record.get("gateway_state") not in {"running", "ready"}:
        return False
    try:
        pid = int(record["pid"])
        if pid <= 0:
            return False
        os.kill(pid, 0)
        updated = datetime.fromisoformat(record["updated_at"].replace("Z", "+00:00"))
        age = ((now or datetime.now(timezone.utc)) - updated).total_seconds()
        return 0 <= age < 180
    except (KeyError, TypeError, ValueError, OSError):
        return False


if __name__ == "__main__":
    try:
        home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
        sys.exit(0 if healthy(json.loads((home / "gateway_state.json").read_text())) else 1)
    except (OSError, ValueError):
        sys.exit(1)
