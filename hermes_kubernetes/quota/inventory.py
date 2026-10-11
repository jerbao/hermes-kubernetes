"""Attach proxy subscription telemetry to native custom-provider picker rows."""

from datetime import datetime, timezone
import threading
import time
from urllib.parse import urlsplit

from hermes_kubernetes.quota.proxy import request_json

_lock = threading.Lock()
_cache = {}
_inflight = set()


def valid_url(value):
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ValueError("Invalid read-only quota service URL")
    if parts.query or parts.fragment or parts.path not in {"", "/"}:
        raise ValueError("Quota service URL must be an origin")
    return value.rstrip("/")


def _fetch(key):
    try:
        payload = request_json(key[1] + "/v1/usage", timeout=5)
        if payload.get("schema") != 1:
            raise ValueError("Unsupported quota response")
    except (OSError, ValueError, TypeError):
        payload = None
    finally:
        with _lock:
            _cache[key] = (time.monotonic(), locals().get("payload"))
            _inflight.discard(key)


def cached(home, url):
    key = (str(home), valid_url(url))
    with _lock:
        started, payload = _cache.get(key, (float("-inf"), None))
        if time.monotonic() - started >= 120 and key not in _inflight:
            _inflight.add(key)
            threading.Thread(target=_fetch, args=(key,), daemon=True).start()
        return payload


def usage_for(payload, family):
    if not isinstance(payload, dict):
        return {
            "accounts": [
                {
                    "id": "pending",
                    "label": "Quota query pending",
                    "state": "unknown",
                    "windows": [],
                    "resets_at": None,
                }
            ]
        }
    try:
        stamp = datetime.fromisoformat(payload["fetched_at"])
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        stale = not 0 <= age <= 300
    except (KeyError, TypeError, ValueError):
        stale = True
    accounts = []
    for entry in payload.get("providers", {}).get(family, {}).get("accounts", []):
        if not isinstance(entry, dict):
            continue
        row = {key: entry.get(key) for key in ("id", "label", "state", "resets_at", "windows")}
        if stale:
            row.update(state="unknown", windows=[], resets_at=None)
        accounts.append(row)
    if not accounts:
        accounts = [
            {
                "id": "unavailable",
                "label": "Quota unavailable",
                "state": "unavailable",
                "windows": [],
                "resets_at": None,
            }
        ]
    return {"accounts": accounts}


def provider_usage():
    """Read the proxy's already-cached subscription snapshot by custom-provider slug.

    Unlike ``apply_usage``, this does not add a second process-local cache. The sidecar refreshes
    upstream usage in its own bounded background loop; this read-only entry point lets short-lived
    UI surfaces refresh their quota rows without rebuilding or probing the model catalog.
    """
    from hermes_cli.config import load_config

    config = load_config().get("proxy_usage") or {}
    mapping = config.get("providers") or {}
    url = config.get("url")
    if not url or not isinstance(mapping, dict):
        return {"providers": {}}

    payload = request_json(valid_url(url) + "/v1/usage", timeout=2)
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise ValueError("Unsupported quota response")
    providers = {
        slug: usage_for(payload, family)
        for slug, family in mapping.items()
        if isinstance(slug, str)
        and isinstance(family, str)
        and family in {"codex", "gemini", "claude"}
    }
    return {"providers": providers, "fetched_at": payload.get("fetched_at")}


def apply_usage(rows):
    from hermes_cli.config import load_config
    from hermes_constants import get_hermes_home

    config = load_config().get("proxy_usage") or {}
    if not config.get("url"):
        return
    mapping = config.get("providers") or {}
    try:
        payload = cached(get_hermes_home(), config["url"])
        for row in rows:
            family = mapping.get(row.get("slug"))
            if row.get("is_user_defined") and family in {"codex", "gemini", "claude"}:
                row["usage"] = usage_for(payload, family)
    except (OSError, ValueError, TypeError, AttributeError):
        # Optional telemetry must never prevent the model picker from opening.
        return
