"""Prove a fresh PVC starts and user preferences survive subsequent boots."""

import json
import os
from pathlib import Path
import sys
import subprocess
import hermes_yaml as yaml

sys.path.insert(0, "/opt/hermes-personal")
from hermes_kubernetes.runtime.bootstrap import prepare
from hermes_kubernetes.operations.server import Runtime
from hermes_kubernetes.operations.protocol import Client
from hermes_kubernetes.operations.tunnel import Tunnel
from hermes_kubernetes.backup.state import build_snapshot
from hermes_kubernetes.runtime.dependencies import prepare_store

home = Path("/opt/data")
policy = Path("/tmp/test-policy")
policy.mkdir()
instance = {
    "name": "fixture",
    "network": "dedicated",
    "terminal_host": "vm.fixture.ts.net",
    "ssh_user": "fixture",
    "model": "fixture-model",
    "timezone": "UTC",
    "proxy": {
        "mode": "external",
        "namespace": "cli-proxy-api",
        "base_url": "http://cli-proxy-api.cli-proxy-api.svc.cluster.local:8317/v1",
    },
}
(policy / "instance.json").write_text(json.dumps(instance))
prepare(home, policy=policy)
config = yaml.safe_load((home / "config.yaml").read_text())
assert config["model"]["provider"] == "custom"
assert config["model"]["base_url"] == instance["proxy"]["base_url"]
assert config["model"]["default"] == "fixture-model"
config["personal_preference"] = "preserved"
(home / "config.yaml").write_text(yaml.safe_dump(config))
prepare(home, policy=policy)
assert yaml.safe_load((home / "config.yaml").read_text())["personal_preference"] == "preserved"
assert (home / ".ssh/id_ed25519").stat().st_mode & 0o777 == 0o600
assert "127.0.0.1:1055" in (home / ".ssh/config").read_text()
instance.update(terminal_transport="direct", terminal_host="192.0.2.120", terminal_port=2222)
(policy / "instance.json").write_text(json.dumps(instance))
prepare(home, policy=policy)
assert "ProxyCommand" not in (home / ".ssh/config").read_text()
assert "Port 2222" in (home / ".ssh/config").read_text()
provenance = json.loads((home / "state/hermes-provenance.json").read_text())
assert Runtime(home).info()["uid"] == 10000
assert Runtime(home).info()["provenance"] == provenance
assert provenance["source_tree_sha256"]
assert provenance["project_revision"] == os.environ["HERMES_EXPECTED_REVISION"]
os.environ["HERMES_RUNTIME_DIR"] = str(prepare_store(home))
from pm.client import ensure

tool = ensure("uv", explicit=True)
from pm.install import uv_launcher

assert tool.run([str(uv_launcher("uv")), "--version"], capture_output=True, check=True).stdout
from hermes_cli._launchers import runtime_command

command = runtime_command(
    Path("/opt/hermes"),
    code='import hermes_yaml; print("fresh-launch-ok")',
    home=home,
    python=sys.executable,
)
assert "fresh-launch-ok" in subprocess.check_output(command, text=True, timeout=30)
print(
    "Fresh state, owner proxy endpoint, persistent preferences, SSH transports and non-root bootstrap: OK"
)
