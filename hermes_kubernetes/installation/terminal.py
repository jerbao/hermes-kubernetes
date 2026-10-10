"""Install the private instance on its SSH terminal without transferring local secrets."""

import json
from pathlib import Path
import shlex
import subprocess
import tempfile

from hermes_kubernetes.installation.files import credential, run


def ssh_options(key, known_hosts, port=22):
    credential(key)
    if not Path(known_hosts).is_file():
        raise ValueError("Pinned SSH known_hosts file is required")
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "UserKnownHostsFile=" + str(known_hosts),
        "-i",
        str(key),
        "-p",
        str(port),
    ]


def runtime_identity(s):
    if s["TERMINAL_KIND"] == "existing":
        return s.file("TERMINAL_SSH_KEY_FILE"), s.file("TERMINAL_KNOWN_HOSTS_FILE")
    private = s.root / ".local/credentials"
    return private / "runtime_ed25519", private / "known_hosts"


def command(s):
    key, hosts = runtime_identity(s)
    return ssh_options(key, hosts, s["TERMINAL_PORT"]) + [
        s["TERMINAL_USER"] + "@" + s["TERMINAL_HOST"]
    ]


RECEIVE = r"""
import pathlib, shutil, subprocess, sys, tempfile
root = pathlib.Path(sys.argv[1])
if any(p.is_symlink() for p in (root, *root.parents)):
    raise ValueError('Workspace must not contain symlinks')
root.mkdir(parents=True, exist_ok=True, mode=0o700)
def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args]).decode().strip()
existing = (root / '.git').exists()
if existing and git(root, 'status', '--porcelain'):
    raise ValueError('Commit terminal edits before installing an instance update')
with tempfile.TemporaryDirectory(prefix='hermes-install-') as temporary:
    bundle = pathlib.Path(temporary) / 'repository.bundle'
    with bundle.open('wb') as output:
        shutil.copyfileobj(sys.stdin.buffer, output)
    staging = pathlib.Path(temporary) / 'project'
    subprocess.run(['git', 'clone', '-q', '--branch', 'main', str(bundle), str(staging)], check=True)
    names = git(staging, 'ls-files', '-z').split('\0')
    for name in filter(None, names):
        source, target = staging / name, root / name
        if source.is_symlink() or any(p.is_symlink() for p in (target, *target.parents)):
            raise ValueError('Aliased installation path')
        if not existing and target.exists() and target.read_bytes() != source.read_bytes():
            raise ValueError('Preserve the existing terminal file before installation')
    if existing:
        if git(root, 'branch', '--show-current') != 'main':
            raise ValueError('Terminal must be on main')
        git(root, 'fetch', str(bundle), 'main')
        old = set(filter(None, git(root, 'ls-files', '-z').split('\0')))
        for name in set(filter(None, names)) - old:
            if (root / name).exists():
                raise ValueError('Incoming project file would overwrite an untracked terminal file')
        git(root, 'merge', '--ff-only', 'FETCH_HEAD')
    else:
        for name in filter(None, names):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staging / name, target)
        shutil.move(str(staging / '.git'), root / '.git')
        git(root, 'remote', 'remove', 'origin')
"""


def install(s):
    from hermes_kubernetes.project.files import assert_paths, layout, tracked_files

    assert_paths(s.root, tracked_files(s.root), layout(s.root))
    if run(["git", "-C", str(s.root), "status", "--porcelain"]).strip():
        raise ValueError("Commit private instance changes before installation")
    if run(["git", "-C", str(s.root), "branch", "--show-current"]).strip() != "main":
        raise ValueError("Install from the private main branch")
    # Transfer the private Git history so the first sync can fast-forward, not diverge.
    with tempfile.TemporaryDirectory(prefix="hermes-instance-") as temporary:
        bundle = Path(temporary) / "repository.bundle"
        run(["git", "-C", str(s.root), "bundle", "create", str(bundle), "main"])
        with bundle.open("rb") as source:
            result = subprocess.run(
                command(s)
                + [
                    "python3 -c "
                    + shlex.quote(RECEIVE)
                    + " "
                    + shlex.quote(s["TERMINAL_WORKSPACE"])
                ],
                stdin=source,
                capture_output=True,
            )
    if result.returncode:
        raise RuntimeError(
            "Terminal workspace transfer failed; preserve terminal edits and verify SSH/storage"
        )
    if s["INSTANCE_REPOSITORY"]:
        repo = s["INSTANCE_REPOSITORY"]
        script = "git -C " + shlex.quote(s["TERMINAL_WORKSPACE"]) + " remote"
        remotes = run(command(s) + [script]).splitlines()
        operation = "set-url" if "origin" in remotes else "add"
        run(command(s) + [script + " " + operation + " origin " + shlex.quote(repo)])
        # Keys are sent over stdin, never in argv or the versioned payload.
        private_key = credential(s.file("GIT_SSH_KEY_FILE"))
        hosts = s.file("GIT_KNOWN_HOSTS_FILE").read_text()
        script = (
            "import json,os,pathlib,sys; v=json.load(sys.stdin); p=pathlib.Path.home()/'.ssh'; p.mkdir(mode=0o700,exist_ok=True); "
            "[(p/n).write_text(t) or None for n,t in v.items()]; [(p/n).chmod(0o600) for n in v]"
        )
        run(
            command(s) + ["python3 -c " + shlex.quote(script)],
            input=json.dumps({"github_ed25519": private_key, "github_known_hosts": hosts}),
        )
        git_ssh = "ssh -i ~/.ssh/github_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=~/.ssh/github_known_hosts"
        run(
            command(s)
            + [
                "git -C "
                + shlex.quote(s["TERMINAL_WORKSPACE"])
                + " config core.sshCommand "
                + shlex.quote(git_ssh)
            ]
        )
        # Preserve the actual private Git history and establish tracking before enabling sync.
        run(command(s) + ["git -C " + shlex.quote(s["TERMINAL_WORKSPACE"]) + " fetch origin main"])
        run(
            command(s)
            + ["git -C " + shlex.quote(s["TERMINAL_WORKSPACE"]) + " merge --ff-only origin/main"]
        )
        run(
            command(s)
            + [
                "git -C "
                + shlex.quote(s["TERMINAL_WORKSPACE"])
                + " branch --set-upstream-to=origin/main main"
            ]
        )
    # Bootstrap the exact dependency now; no model call is needed.
    run(command(s) + ["python3 " + shlex.quote(s["TERMINAL_WORKSPACE"] + "/hermes.py") + " check"])
    if s["INSTANCE_REPOSITORY"]:
        unit = f"""[Unit]
Description=Synchronize the private Hermes instance
[Service]
Type=oneshot
WorkingDirectory={s["TERMINAL_WORKSPACE"]}
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 {s["TERMINAL_WORKSPACE"]}/hermes.py sync --publish
"""
        timer = "[Timer]\nOnBootSec=2min\nOnUnitActiveSec=2min\n[Install]\nWantedBy=timers.target\n"
        script = "import json,pathlib,sys; p=pathlib.Path.home()/'.config/systemd/user'; p.mkdir(parents=True,exist_ok=True); [(p/n).write_text(t) for n,t in json.load(sys.stdin).items()]"
        run(
            command(s) + ["python3 -c " + shlex.quote(script)],
            input=json.dumps(
                {"hermes-workspace-sync.service": unit, "hermes-workspace-sync.timer": timer}
            ),
        )
        run(
            command(s)
            + [
                "systemctl --user daemon-reload && systemctl --user enable --now hermes-workspace-sync.timer"
            ]
        )
