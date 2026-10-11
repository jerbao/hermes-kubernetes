"""Report whether the private workspace synchronization timer is operating."""

import subprocess


TIMER = "hermes-workspace-sync.timer"
SERVICE = "hermes-workspace-sync.service"
PROPERTIES = (
    "ActiveState",
    "SubState",
    "UnitFileState",
    "Result",
    "ExecMainStatus",
    "ExecMainStartTimestamp",
    "LastTriggerUSec",
    "NextElapseUSecRealtime",
)


def _show(unit):
    result = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "--no-pager",
            *("--property=" + property_name for property_name in PROPERTIES),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def report():
    try:
        timer = _show(TIMER)
        service = _show(SERVICE)
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "ok": False,
            "error": "Cannot inspect user synchronization units: " + type(error).__name__,
        }

    timer_active = timer.get("ActiveState") == "active"
    timer_enabled = timer.get("UnitFileState") in {"enabled", "enabled-runtime"}
    running = service.get("ActiveState") in {"activating", "active", "reloading"}
    errors = []
    if not timer_enabled:
        errors.append("timer is not enabled for this user")
    if not timer_active:
        errors.append("timer is not active for this user")
    if service.get("ActiveState") == "failed" or service.get("Result") == "failed":
        errors.append("last synchronization service run failed")

    return {
        "ok": not errors,
        "errors": errors,
        "timer": {
            "unit": TIMER,
            "active": timer_active,
            "enabled": timer_enabled,
            "last_trigger": timer.get("LastTriggerUSec", ""),
            "next_trigger": timer.get("NextElapseUSecRealtime", ""),
        },
        "service": {
            "unit": SERVICE,
            "running": running,
            "state": service.get("ActiveState", "unknown"),
            "result": service.get("Result", "unknown"),
            "exit_code": service.get("ExecMainStatus", ""),
            "started_at": service.get("ExecMainStartTimestamp", ""),
        },
    }
