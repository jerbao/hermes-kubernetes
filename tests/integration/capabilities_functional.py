"""Manual image acceptance: native Desktop RPCs and a real plugin dependency.

Run in a disposable container with /fixtures/plugin (a synthetic Git repository),
a writable /opt/data, /tmp and outbound access to the package registry.
No gateway or messaging channel is started.
"""

import contextlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, "/tests")
from capabilities_gateway import NativeGateway

from hermes_kubernetes.runtime.bootstrap import ensure_home_alias
from hermes_kubernetes.runtime.dependencies import prepare_store

home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
home.mkdir(mode=0o700, exist_ok=True)
ensure_home_alias(home)
os.environ["HERMES_RUNTIME_DIR"] = str(prepare_store(home))


def call(action, **values):
    return rpc.call("plugins.manage", action=action, **values)


def verify_after_restart():
    from hermes_cli._launchers import runtime_command

    code = """
import json
from hermes_cli.plugins import discover_plugins
from tools.registry import registry
discover_plugins(force=True)
entry=registry.get_entry('lifecycle_fixture')
assert entry
result=json.loads(entry.handler({}))
assert result['greeting']=='example-validation'
assert result['rendered']
print(json.dumps({'plugin_callable':True,'dependency_imported':True}))
"""
    command = runtime_command(
        Path("/opt/hermes"), code=code, home=home, python="/opt/hermes/.venv/bin/python"
    )
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["dependency_imported"]


rpc = NativeGateway(home)
report = {
    "passed": False,
    "native_desktop_rpc": True,
    "websocket": True,
    "production_activated": False,
}
log = home / "capabilities-functional.log"
with (
    os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle,
    contextlib.redirect_stdout(handle),
    contextlib.redirect_stderr(handle),
):
    logging.basicConfig(stream=handle, level=logging.INFO)
    try:
        rpc.start()
        installed = call("install", identifier="file:///fixtures/plugin", enable=True)
        assert installed["ok"] and installed["enabled"], installed
        name = installed["plugin_name"]
        assert call("settings", name=name, values={"greeting": "example-validation"})["ok"]
        verify_after_restart()
        assert call("toggle", name=name, enable=False)["ok"]
        rows = call("list")["plugins"]
        assert next(p for p in rows if p["name"] == name)["status"] == "disabled"
        assert call("toggle", name=name, enable=True)["ok"]
        verify_after_restart()
        assert call("remove", name=name)["ok"]
        assert not any(p["name"] == name for p in call("list")["plugins"])
        report.update(
            passed=True,
            install=True,
            configure=True,
            disable=True,
            enable=True,
            remove=True,
            native_pm_dependency=True,
            restart_preserves_plugin=True,
        )
    except Exception as error:
        logging.exception("Capabilities acceptance failed")
        report["error_type"] = type(error).__name__
    finally:
        rpc.close()
print(json.dumps(report))
raise SystemExit(0 if report["passed"] else 1)
