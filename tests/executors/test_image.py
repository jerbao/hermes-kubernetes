"""Boot the image's actual SSH entrypoint and complete one MCP handshake."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid


def run(argv, *, check=True, **kwargs):
    result = subprocess.run(argv, text=True, capture_output=True, **kwargs)
    if check and result.returncode:
        raise RuntimeError(f"command failed: {argv[0]} (exit {result.returncode})")
    return result


class ReadinessFailure(RuntimeError):
    def __init__(self, message, *, status, logs):
        super().__init__(message)
        self.status = status
        self.logs = logs


def server_diagnostics(name):
    status = run(
        ["docker", "inspect", "--format", "{{.State.Status}}", name], check=False
    ).stdout.strip()
    logs_result = run(["docker", "logs", "--tail", "40", name], check=False)
    logs = "\n".join(part for part in (logs_result.stdout, logs_result.stderr) if part).strip()
    return status or "unknown", logs[-1200:]


def main(image, *, codegraph=False):
    platform = run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Os}}/{{.Architecture}}{{if .Variant}}/{{.Variant}}{{end}}",
            image,
        ]
    ).stdout.strip()
    name = f"hermes-mcp-smoke-{uuid.uuid4().hex[:12]}"
    network = name + "-net"
    fixture_volume = name + "-fixture"
    with tempfile.TemporaryDirectory(prefix="hermes-mcp-smoke-") as temporary:
        directory = Path(temporary)
        os.chmod(directory, 0o755)
        client = directory / "client"
        host_key = directory / "host_key"
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(client)])
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)])
        (directory / "authorized_keys").write_text((directory / "client.pub").read_text())
        host_public = run(["ssh-keygen", "-y", "-f", str(host_key)]).stdout.strip()
        (directory / "known_hosts").write_text(f"hermes-mcp {host_public}\n")
        for path in (client, host_key):
            os.chmod(path, 0o600)

        # CI's Linux bind-mount owner is the runner UID, while the image runs as
        # 10000. Stage synthetic credentials into a Docker-managed filesystem,
        # then set the same numeric owner/modes the rootless image sees at runtime.
        try:
            run(["docker", "volume", "create", fixture_volume])
            stage = (
                "import os,shutil\n"
                "items=[('/source/host_key','host_key',0o400),"
                "('/source/authorized_keys','authorized_keys',0o600),"
                "('/source/client','client_key',0o400),"
                "('/source/known_hosts','known_hosts',0o400)]\n"
                "for source,name,mode in items:\n"
                " target='/fixture/'+name\n"
                " shutil.copyfile(source,target)\n"
                " os.chown(target,10000,10000)\n"
                " os.chmod(target,mode)\n"
                " stat=os.stat(target)\n"
                " assert stat.st_uid==10000 and stat.st_gid==10000\n"
                " assert stat.st_mode & 0o777==mode\n"
            )
            run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--platform",
                    platform,
                    "--user",
                    "0:0",
                    "--mount",
                    f"type=volume,src={fixture_volume},dst=/fixture",
                    "--mount",
                    f"type=bind,src={directory},dst=/source,readonly",
                    "--entrypoint",
                    "/opt/mcp-venv/bin/python",
                    image,
                    "-c",
                    stage,
                ]
            )
            run(["docker", "network", "create", network])
        except Exception:
            run(["docker", "volume", "rm", fixture_volume], check=False)
            raise
        try:
            run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--platform",
                    platform,
                    "--name",
                    name,
                    "--network",
                    network,
                    "--network-alias",
                    "hermes-mcp",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--mount",
                    f"type=volume,src={fixture_volume},dst=/etc/mcp-ssh,readonly",
                    "--tmpfs",
                    "/var/lib/mcp:rw,exec,nosuid,nodev,uid=10000,gid=10000,mode=0700,size=64m",
                    "--tmpfs",
                    "/tmp:rw,exec,nosuid,nodev,uid=10000,gid=10000,mode=0700,size=64m",
                    image,
                ]
            )
            deadline = time.monotonic() + 30
            while True:
                probe = run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--platform",
                        platform,
                        "--network",
                        network,
                        "--entrypoint",
                        "/usr/bin/ssh-keyscan",
                        image,
                        "-T",
                        "2",
                        "-p",
                        "2222",
                        "-t",
                        "ed25519",
                        "hermes-mcp",
                    ],
                    check=False,
                )
                if probe.stdout.strip():
                    break
                if time.monotonic() >= deadline:
                    status, logs = server_diagnostics(name)
                    raise ReadinessFailure(
                        "MCP SSH listener did not become ready",
                        status=status,
                        logs=logs,
                    )
                time.sleep(0.2)

            fixture_server = (
                "import json,sys\n"
                "for line in sys.stdin:\n"
                " request=json.loads(line)\n"
                " if request.get('method')=='initialize':\n"
                "  result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},"
                "'serverInfo':{'name':'fixture','version':'1'}}\n"
                " elif request.get('method')=='tools/list':\n"
                "  result={'tools':[{'name':'echo','description':'fixture',"
                "'inputSchema':{'type':'object','properties':{}}}]}\n"
                " else: continue\n"
                " print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)\n"
            )
            request = [
                {"command": "python3", "args": ["-c", fixture_server], "env": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "hermes-mcp-image-smoke",
                            "version": "1",
                        },
                    },
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            ]
            ssh = [
                "docker",
                "run",
                "--rm",
                "-i",
                "--platform",
                platform,
                "--network",
                network,
                "--mount",
                f"type=volume,src={fixture_volume},dst=/tmp/mcp-ssh,readonly",
                "--entrypoint",
                "/usr/bin/ssh",
                image,
                "-T",
                "-F",
                "/dev/null",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "HostKeyAlias=hermes-mcp",
                "-o",
                "UserKnownHostsFile=/tmp/mcp-ssh/known_hosts",
                "-o",
                "ConnectTimeout=10",
                "-i",
                "/tmp/mcp-ssh/client_key",
                "-p",
                "2222",
                "mcp@hermes-mcp",
                "hermes-mcp",
            ]
            result = subprocess.run(
                ssh,
                input="\n".join(map(json.dumps, request)) + "\n",
                text=True,
                capture_output=True,
                timeout=45,
            )
            if result.returncode:
                detail = result.stderr.strip()[-500:]
                server_log = run(["docker", "logs", name], check=False).stderr.strip()[-500:]
                status = run(
                    ["docker", "inspect", "--format", "{{.State.Status}}", name]
                ).stdout.strip()
                raise RuntimeError(
                    f"authenticated MCP SSH command failed ({status}): {detail}; {server_log}"
                )
            messages = [
                json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
            ]
            initialize = next((item for item in messages if item.get("id") == 1), None)
            tools = next((item for item in messages if item.get("id") == 2), None)
            assert initialize and isinstance(
                initialize.get("result", {}).get("protocolVersion"), str
            )
            assert tools and "echo" in {
                item.get("name") for item in tools.get("result", {}).get("tools", [])
            }
            if codegraph:
                native_probe = (
                    "import os,sys;from pathlib import Path;"
                    "sys.path.insert(0,'/opt/mcp');from hermes_kubernetes.executors.packages import smoke_mcp;"
                    "root=Path('/tmp/codegraph-smoke');root.mkdir();"
                    "(root/'sample.py').write_text('def hello(): return 1\\n');"
                    "smoke_mcp(['codegraph','serve','--mcp'],os.environ.copy(),cwd=root,timeout=45)"
                )
                probe = run(
                    [
                        "docker",
                        "exec",
                        name,
                        "/opt/mcp-venv/bin/python",
                        "-c",
                        native_probe,
                    ],
                    check=False,
                    timeout=60,
                )
                if probe.returncode:
                    raise RuntimeError("Synthetic Codegraph probe failed: " + probe.stderr[-1000:])
            print(
                json.dumps(
                    {
                        "passed": True,
                        "default_entrypoint": True,
                        "synthetic_ssh_auth": True,
                        "mcp_initialize": True,
                        "tools_list": True,
                        "codegraph_native": codegraph,
                    }
                )
            )
        finally:
            run(["docker", "stop", name], check=False, timeout=15)
            run(["docker", "rm", name], check=False, timeout=15)
            run(["docker", "network", "rm", network], check=False, timeout=15)
            run(["docker", "volume", "rm", fixture_volume], check=False, timeout=15)


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3 and sys.argv[2] != "--codegraph"):
        raise SystemExit("usage: python tests/executors/test_image.py IMAGE [--codegraph]")
    try:
        main(sys.argv[1], codegraph="--codegraph" in sys.argv[2:])
    except ReadinessFailure as error:
        print(
            json.dumps(
                {
                    "passed": False,
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "server_status": error.status,
                    "synthetic_server_logs": error.logs,
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)
    except Exception as error:
        print(
            json.dumps(
                {
                    "passed": False,
                    "error_type": type(error).__name__,
                    "error": str(error)[-1500:],
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)
