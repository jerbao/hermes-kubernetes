"""Restore one baked MCP's exact lock in isolated scratch, handshake, then remove it."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import json
from pathlib import Path
import subprocess

PROBE = r"""
import json,os,pathlib,sys,tempfile
sys.path.insert(0,'/opt/mcp')
from hermes_kubernetes.executors.packages import installation_recipe,restore_package,smoke_mcp,has_active_override
argv=['npx','-y','@modelcontextprotocol/server-sequential-thinking']
with tempfile.TemporaryDirectory(prefix='.versioning-proof-',dir='/var/lib/mcp') as temporary:
 private=pathlib.Path(temporary)
 recipe=installation_recipe(argv,private)
 assert recipe is not None
 env={k:v for k,v in os.environ.items() if k in {'PATH','LANG','LC_ALL','TZ'}}
 env.update(HOME=str(private),NPM_CONFIG_CACHE=str(private/'npm-cache'),PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD='1',PUPPETEER_SKIP_DOWNLOAD='true')
 result=restore_package(argv,private,env,recipe,smoke=lambda command,environment:smoke_mcp(command,environment,cwd=private))
 assert result['status']=='restored' and has_active_override(argv,private)
 recorded=installation_recipe(argv,private)
 assert recorded['recipe']['lock']==recipe['recipe']['lock']
 print(json.dumps({'passed':True,'exact_npm_lock':True,'handshake':True,'isolated_generation':True,'cleanup':'on-exit'}))
"""


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kubeconfig", required=True)
    p.add_argument("--namespace", default="hermes")
    p.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[2] / ".local/versioning/mcp-restore-proof.json",
    )
    a = p.parse_args()
    base = ["kubectl", "--kubeconfig", a.kubeconfig, "-n", a.namespace]
    pods = json.loads(
        subprocess.check_output(
            base + ["get", "pods", "-l", "app.kubernetes.io/name=hermes-mcp", "-o", "json"],
            text=True,
        )
    )["items"]
    ready = [
        pod
        for pod in pods
        if pod["status"]["phase"] == "Running"
        and all(c["ready"] for c in pod["status"].get("containerStatuses", []))
        and not pod["metadata"].get("deletionTimestamp")
    ]
    if len(ready) != 1:
        raise SystemExit("Expected one ready API MCP executor")
    a.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    error = a.output.with_suffix(".err")
    with a.output.open("wb") as out, error.open("wb") as err:
        a.output.chmod(0o600)
        error.chmod(0o600)
        result = subprocess.run(
            base
            + [
                "exec",
                ready[0]["metadata"]["name"],
                "-c",
                "executor",
                "--",
                "/opt/mcp-venv/bin/python",
                "-c",
                PROBE,
            ],
            stdout=out,
            stderr=err,
            timeout=600,
        )
    if result.returncode:
        raise SystemExit("MCP restoration proof failed; private diagnostics: " + str(error))
    print(a.output.read_text())


if __name__ == "__main__":
    main()
