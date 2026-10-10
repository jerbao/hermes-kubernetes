"""Run MCP stdio processes in their own pod, without the Hermes filesystem."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from contextlib import suppress

import asyncssh
from hermes_kubernetes.executors.packages import (
    active_package_versions,
    has_active_override,
    package_version,
    prepare as prepare_package,
    smoke_mcp,
    update_package,
    installation_recipe,
    restore_package,
)

MAX_DEFINITION = 1024 * 1024
NPM_BINS = {
    "@colbymchenry/codegraph": "codegraph",
    "@modelcontextprotocol/server-sequential-thinking": "mcp-server-sequential-thinking",
    "@upstash/context7-mcp": "context7-mcp",
    "@zereight/mcp-gitlab": "mcp-gitlab",
    "chrome-devtools-mcp": "chrome-devtools-mcp",
    "@playwright/mcp": "playwright-mcp",
    "@modelcontextprotocol/server-github": "mcp-server-github",
}
BIN_PACKAGES = {binary: package for package, binary in NPM_BINS.items()}


def package_command(argv, prefix=Path("/opt/mcp/node_modules/.bin")):
    """Canonicalize only known image package commands for profile overrides."""
    if not argv:
        return argv
    command, args = argv[0], list(argv[1:])
    package = BIN_PACKAGES.get(command)
    if package is None:
        for binary, candidate in BIN_PACKAGES.items():
            if command == str(prefix / binary):
                package = candidate
                break
    if package is None:
        return argv
    return ["npx", "-y", package + "@latest", *args]


def packaged_command(argv, prefix=Path("/opt/mcp/node_modules/.bin"), *, dynamic=False):
    """Use the image's pinned package instead of fetching @latest on restart."""
    if argv[0] != "npx":
        return argv
    arguments = list(argv[1:])
    while arguments and arguments[0] in {"-y", "--yes", "--"}:
        arguments.pop(0)
    if not arguments:
        raise ValueError("Missing npm package")
    package = arguments.pop(0)
    name = package.rsplit("@", 1)[0] if "@" in package[1:] else package
    if name not in NPM_BINS:
        if dynamic:
            return argv
        raise ValueError("Add the MCP package to the image before activating it")
    if name != package:
        version = package[len(name) + 1 :]
        pinned = json.loads(
            (Path(__file__).resolve().parents[2] / "dependencies/mcp/package.json").read_text()
        )["dependencies"][name]
        if version not in {"latest", pinned}:
            if dynamic:
                return argv
            raise ValueError("The requested MCP version differs from the image")
    binary = prefix / NPM_BINS[name]
    return [str(binary), *arguments]


def child_configuration(definition, directory, workspace=None):
    profile = definition.get("profile", "default")
    if not isinstance(profile, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", profile):
        raise ValueError("Invalid MCP profile")
    command, args = definition.get("command"), definition.get("args", [])
    if not isinstance(command, str) or not command or not isinstance(args, list):
        raise ValueError("Invalid MCP command")
    if not all(isinstance(value, str) for value in args):
        raise ValueError("Invalid MCP arguments")
    private = directory / "profiles" / profile
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = private / "workspace"
    work.mkdir(mode=0o700, exist_ok=True)
    requested = definition.get("cwd")
    if requested:
        if not isinstance(requested, str) or workspace is None:
            raise ValueError("The MCP requires a mounted workspace")
        root = workspace.resolve(strict=True)
        work = Path(requested).resolve(strict=True)
        if not work.is_dir() or not work.is_relative_to(root):
            raise ValueError("MCP cwd is outside the mounted workspace")
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL", "TZ"}}
    user_env = definition.get("env", {})
    if not isinstance(user_env, dict):
        raise ValueError("Invalid MCP environment")
    env.update({str(k): str(v) for k, v in user_env.items()})
    cache = Path("/tmp/mcp-cache") / profile
    temporary = cache / "tmp"
    temporary.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Host scratch paths cannot be used in an isolated executor. Like HOME,
    # temporary directories belong to the executor and the selected profile.
    env.update(TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
    env.update(
        HOME=str(private),
        XDG_CACHE_HOME=str(private / ".cache"),
        NPM_CONFIG_CACHE=str(cache / "npm"),
        UV_CACHE_DIR=str(cache / "uv"),
        UV_PYTHON_INSTALL_DIR=str(cache / "python"),
    )
    return [command, *args], work, env


def _management_request(request, directory, workspace=None):
    """Admin-only operation carried over the authenticated MCP SSH channel."""
    if not isinstance(request, dict) or request.get("operation") != "manage":
        raise ValueError("Invalid management operation")
    action = request.get("action")
    profile = request.get("profile")
    if (
        action not in {"list", "plan", "apply", "restore"}
        or not isinstance(profile, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", profile)
    ):
        raise ValueError("Invalid management request")
    items = request.get("servers")
    if not isinstance(items, list) or len(items) > 128:
        raise ValueError("Invalid management server list")
    results = []
    for item in items:
        name = item.get("name") if isinstance(item, dict) else None
        definition = item.get("definition") if isinstance(item, dict) else None
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name)
            or not isinstance(definition, dict)
        ):
            results.append(
                {
                    "name": "invalid",
                    "status": "failed",
                    "error": "Invalid server definition",
                }
            )
            continue
        command, args = definition.get("command"), definition.get("args", [])
        if isinstance(definition.get("url"), str):
            results.append({"name": name, "status": "skipped-http", "provider_managed": True})
            continue
        if (
            not isinstance(command, str)
            or not isinstance(args, list)
            or not all(isinstance(x, str) for x in args)
        ):
            results.append({"name": name, "status": "skipped-unsupported"})
            continue
        argv = package_command([command, *args])
        if argv[0] not in {"npx", "uvx"}:
            results.append({"name": name, "status": "skipped-unsupported"})
            continue
        try:
            update_definition = {
                **definition,
                "command": argv[0],
                "args": argv[1:],
                "profile": profile,
            }
            argv, cwd, env = child_configuration(update_definition, directory, workspace)
        except (ValueError, OSError):
            results.append(
                {
                    "name": name,
                    "status": "failed",
                    "error": "Invalid MCP definition or workspace path",
                }
            )
            continue
        profile_home = Path(env["HOME"])
        cache = Path("/tmp/mcp-packages") / profile
        policy = package_version(argv)
        try:
            override_active = has_active_override(argv, profile_home)
        except ValueError:
            results.append(
                {
                    "name": name,
                    "status": "failed",
                    "error": "Persistent package generation is invalid; refused fallback",
                }
            )
            continue
        version = item.get("version")
        if version is not None and (
            not isinstance(version, str)
            or len(version) > 128
            or not re.fullmatch(r"[A-Za-z0-9.+_-]+", version)
        ):
            results.append(
                {
                    "name": name,
                    "status": "failed",
                    "error": "Invalid requested package version",
                }
            )
            continue
        if action == "list":
            results.append(
                {
                    "name": name,
                    "status": "configured",
                    "package_policy": package_version(argv),
                    "override_active": override_active,
                    "active_versions": active_package_versions(argv, profile_home),
                    "installation_recipe": installation_recipe(argv, profile_home),
                }
            )
        elif action == "plan":
            policy = package_version(argv)
            results.append(
                {
                    "name": name,
                    "status": "would-update" if version or policy == "floating" else "pinned",
                    "package_policy": policy,
                    "override_active": override_active,
                }
            )
        elif action == "restore":
            try:
                outcome = restore_package(
                    argv,
                    profile_home,
                    env,
                    item.get("recipe"),
                    cache=cache,
                    smoke=lambda cmd, child_env: smoke_mcp(cmd, child_env, cwd=cwd),
                )
                results.append({"name": name, **outcome})
            except Exception:
                results.append(
                    {
                        "name": name,
                        "status": "failed",
                        "error": "Locked installation failed; previous generation remains active",
                    }
                )
        elif policy == "pinned" and version is None:
            results.append({"name": name, "status": "skipped-pinned", "package_policy": policy})
        else:
            try:
                outcome = update_package(
                    argv,
                    profile_home,
                    env,
                    cache=cache,
                    version=version,
                    smoke=lambda cmd, child_env: smoke_mcp(cmd, child_env, cwd=cwd),
                )
                results.append({"name": name, **outcome})
            except Exception:
                results.append(
                    {
                        "name": name,
                        "status": "failed",
                        "error": "Install or MCP smoke test failed; previous generation remains active",
                    }
                )
    return {
        "operation": "manage",
        "action": action,
        "profile": profile,
        "results": results,
        "ok": all(item.get("status") not in {"failed"} for item in results),
    }


async def copy(source, destination):
    while chunk := await source.read(65536):
        destination.write(chunk)
        await destination.drain()


async def feed(source, destination):
    try:
        await copy(source, destination)
    finally:
        destination.close()


async def stop(child):
    # Each subprocess starts a new session. Kill that owned group, including
    # npm descendants, even when the direct child has already exited.
    with suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(child.wait(), 3)
    except TimeoutError:
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        await child.wait()
    finally:
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)


class Runner:
    def __init__(self, directory, workspace=None):
        self.directory = directory
        self.workspace = workspace
        self.children = set()

    async def handle(self, channel):
        if channel.command != "hermes-mcp" or channel.get_extra_info("username") != "mcp":
            channel.exit(126)
            return
        child = None
        tasks = []
        try:
            line = await asyncio.wait_for(channel.stdin.readline(), 20)
            if len(line) > MAX_DEFINITION:
                raise ValueError("MCP definition exceeds the limit")
            definition = json.loads(line)
            if not isinstance(definition, dict):
                raise ValueError("Invalid MCP definition")
            if definition.get("operation") == "manage":
                result = await asyncio.to_thread(
                    _management_request, definition, self.directory, self.workspace
                )
                channel.stdout.write((json.dumps(result, sort_keys=True) + "\n").encode())
                channel.exit(0 if result["ok"] else 1)
                return
            argv, cwd, env = child_configuration(definition, self.directory, self.workspace)
            private = Path(env["HOME"])
            package_argv = package_command(argv)
            if not has_active_override(package_argv, private):
                argv = packaged_command(package_argv, dynamic=True)
            else:
                argv = package_argv
            argv = await asyncio.to_thread(
                prepare_package,
                argv,
                private,
                env,
                cache=Path("/tmp/mcp-packages") / definition.get("profile", "default"),
            )
            child = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            self.children.add(child)
            incoming = asyncio.create_task(feed(channel.stdin, child.stdin))
            outgoing = asyncio.create_task(copy(child.stdout, channel.stdout))
            errors = asyncio.create_task(copy(child.stderr, channel.stderr))
            finished = asyncio.create_task(child.wait())
            tasks = [incoming, outgoing, errors, finished]
            await asyncio.wait([incoming, finished], return_when=asyncio.FIRST_COMPLETED)
            if not finished.done():
                try:
                    await asyncio.wait_for(asyncio.shield(finished), 3)
                except TimeoutError:
                    await stop(child)
            else:
                await stop(child)
            await asyncio.gather(outgoing, errors)
            channel.exit(child.returncode)
        except (ValueError, OSError, TimeoutError):
            channel.stderr.write(b"MCP runner could not start the configured process\n")
            channel.exit(1)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if child is not None:
                await stop(child)
                self.children.discard(child)

    async def close(self):
        await asyncio.gather(*(stop(child) for child in list(self.children)))


async def listen(runner, host_key, authorized_keys, *, host="0.0.0.0", port=2222):
    return await asyncssh.listen(
        host,
        port,
        server_host_keys=[str(host_key)],
        authorized_client_keys=str(authorized_keys),
        process_factory=runner.handle,
        encoding=None,
        sftp_factory=None,
        allow_scp=False,
    )


async def main(args):
    os.umask(0o077)
    runner = Runner(args.directory, args.workspace)
    server = await listen(runner, args.host_key, args.authorized_keys, port=args.port)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    try:
        await stopped.wait()
    finally:
        server.close()
        await server.wait_closed()
        await runner.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("/var/lib/mcp"))
    parser.add_argument(
        "--workspace",
        type=Path,
        help="Optional project mount; never the Hermes state PVC",
    )
    parser.add_argument("--host-key", type=Path, default=Path("/etc/mcp-ssh/host_key"))
    parser.add_argument(
        "--authorized-keys", type=Path, default=Path("/etc/mcp-ssh/authorized_keys")
    )
    parser.add_argument("--port", type=int, default=2222)
    asyncio.run(main(parser.parse_args()))
