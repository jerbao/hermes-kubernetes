"""Translate authoritative provider quotas to the native Desktop usage contract."""

from datetime import datetime, timezone
import math


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def reset_time(value):
    try:
        if number(value):
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        if isinstance(value, str) and value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo:
                return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        pass
    return None


def window(label, used, reset, scope="account"):
    if not number(used) or not 0 <= used <= 100:
        return None
    return {
        "label": label,
        "used_percent": float(used),
        "resets_at": reset_time(reset),
        "scope": scope,
    }


def normalize(provider, payload):
    """Missing, malformed or unsupported data never becomes a fabricated zero."""
    if not isinstance(payload, dict):
        return []
    windows = []
    if provider == "codex":
        groups = [("", payload.get("rate_limit"), "account")]
        for extra in payload.get("additional_rate_limits") or []:
            if isinstance(extra, dict):
                groups.append(
                    (
                        str(extra.get("limit_name") or "Model") + " · ",
                        extra.get("rate_limit"),
                        "model",
                    )
                )
        for prefix, group, scope in groups:
            if not isinstance(group, dict):
                continue
            for key in ("primary_window", "secondary_window"):
                item = group.get(key)
                if not isinstance(item, dict):
                    continue
                duration = item.get("limit_window_seconds")
                label = {18000: "5 hours", 604800: "7 days"}.get(duration, "Usage window")
                if number(duration) and duration > 0 and duration not in (18000, 604800):
                    label = f"{duration / 3600:g} hours"
                windows.append(
                    window(prefix + label, item.get("used_percent"), item.get("reset_at"), scope)
                )
    elif provider == "claude":
        for key, label in (
            ("five_hour", "5 hours"),
            ("seven_day", "7 days"),
            ("seven_day_opus", "Opus · 7 days"),
            ("seven_day_sonnet", "Sonnet · 7 days"),
        ):
            item = payload.get(key)
            if isinstance(item, dict):
                # Claude utilization is a percentage, including fractional percentages.
                windows.append(
                    window(
                        label,
                        item.get("utilization"),
                        item.get("resets_at"),
                        "model" if key in {"seven_day_opus", "seven_day_sonnet"} else "account",
                    )
                )
    elif provider == "gemini":
        for item in payload.get("buckets") or []:
            if not isinstance(item, dict):
                continue
            fraction = item.get("remainingFraction")
            if not number(fraction) or not 0 <= fraction <= 1:
                continue
            label = (
                str(item.get("modelId") or "Gemini") + " · " + str(item.get("tokenType") or "Quota")
            )
            windows.append(
                window(label, round((1 - fraction) * 100, 8), item.get("resetTime"), "model")
            )
    return [item for item in windows if item is not None]
