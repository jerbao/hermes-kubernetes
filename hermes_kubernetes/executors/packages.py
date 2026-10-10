"""Prepare MCP packages outside Hermes, retaining locks for disposable executors."""

import fcntl
import hashlib
import json
import os
import contextlib
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid


def run(argv, *, env, log, timeout=240, cwd=None):
    process = subprocess.Popen(
        argv,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        cwd=cwd,
    )
    try:
        output, errors = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise ValueError("MCP package preparation timed out") from None
    with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as handle:
        handle.write((output + errors)[-524288:])
    if process.returncode:
        raise ValueError("MCP package preparation failed; see the executor installation log")
    return output.decode().strip()


def npm_request(argv):
    args = list(argv[1:])
    packages = []
    while args:
        value = args[0]
        if value in {"-y", "--yes"}:
            args.pop(0)
        elif value in {"-p", "--package"}:
            args.pop(0)
            if not args:
                raise ValueError("Missing npm package")
            packages.append(args.pop(0))
        elif value.startswith("--package="):
            packages.append(args.pop(0).split("=", 1)[1])
        elif value == "--":
            args.pop(0)
            break
        else:
            break
    if not args:
        raise ValueError("Missing npm package or executable")
    if packages:
        executable = args.pop(0)
    else:
        packages = [args.pop(0)]
        executable = None
    for package in packages:
        if not package or package.startswith("-") or any(c in package for c in "\n\r\0"):
            raise ValueError("Invalid npm package specifier")
    if executable and Path(executable).name != executable:
        raise ValueError("Invalid npm executable")
    return packages, executable, args


def python_request(argv):
    args = list(argv[1:])
    package, python, extra = None, "/usr/bin/python3", []
    while args and args[0].startswith("-"):
        option = args.pop(0)
        if option in {"--from", "--python", "--with"} and args:
            value = args.pop(0)
            if option == "--from":
                package = value
            elif option == "--python":
                python = value
            else:
                extra.append(value)
        elif option == "--":
            break
        else:
            raise ValueError("Unsupported uvx install option")
    if not args:
        raise ValueError("Missing Python MCP executable")
    executable = args.pop(0)
    if package is None:
        package = executable
        executable = re.split(r"[<>=!~\[]", executable)[0]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", executable):
        raise ValueError("Invalid Python MCP executable")
    for requirement in [package, *extra]:
        if not re.fullmatch(
            r"[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?(?:[<>=!~][A-Za-z0-9.*<>=!~,+-]+)?",
            requirement,
        ):
            raise ValueError("Use registry Python packages for automatic MCP installation")
    if python != "/usr/bin/python3" and not re.fullmatch(r"3\.\d+(?:\.\d+)?", python):
        raise ValueError("Use a Python version available to uv")
    return [package, *extra], executable, python, args


def write_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    temporary.replace(path)


def package_identity(argv):
    """Return the stable request key used by image packages and profile overrides."""
    if not argv:
        return None
    kind = Path(argv[0]).name
    if kind not in {"npx", "uvx"}:
        return None
    request = npm_request(argv) if kind == "npx" else python_request(argv)
    identity = hashlib.sha256(json.dumps([kind, request[:-1]], sort_keys=True).encode()).hexdigest()
    return kind, request, identity


def _pointer_path(private, identity):
    return private / "package-pointers" / (identity + ".json")


def _active_record(private, identity):
    pointer = _pointer_path(private, identity)
    if not pointer.is_file():
        if pointer.exists() or pointer.is_symlink():
            raise ValueError("MCP package generation pointer is invalid; refusing package fallback")
        return None
    try:
        if pointer.is_symlink():
            raise ValueError("MCP package generation pointer is invalid; refusing package fallback")
        value = json.loads(pointer.read_text())
        generation = value["generation"]
        if not re.fullmatch(r"[a-f0-9]{32}", generation):
            raise ValueError("MCP package generation pointer is invalid; refusing package fallback")
        record = private / "package-generations" / identity / (generation + ".json")
        if record.is_symlink():
            raise ValueError("MCP package generation record is invalid; refusing package fallback")
        recipe = json.loads(record.read_text())
        if recipe.get("identity") != identity or recipe.get("generation") != generation:
            raise ValueError("MCP package generation record is invalid; refusing package fallback")
        return value, recipe
    except ValueError as exc:
        if isinstance(exc, json.JSONDecodeError):
            raise ValueError(
                "MCP package generation record is invalid; refusing package fallback"
            ) from exc
        raise
    except (OSError, KeyError, TypeError) as exc:
        raise ValueError(
            "MCP package generation record is invalid; refusing package fallback"
        ) from exc


def _locked_command(kind, recipe, request, destination):
    binary = destination / recipe["binary"]
    if not binary.is_file():
        raise ValueError("MCP package executable is missing")
    if kind == "uvx":
        return [str(destination / "bin/python"), str(binary), *request[-1]]
    return [str(binary), *request[-1]]


def _replace_directory(stage, destination):
    """Publish a fully built directory while keeping the previous one recoverable."""
    backup = destination.with_name(destination.name + ".previous-" + uuid.uuid4().hex)
    if destination.exists():
        destination.replace(backup)
    try:
        stage.replace(destination)
    except Exception:
        if backup.exists() and not destination.exists():
            backup.replace(destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def has_active_override(argv, private):
    identity = package_identity(argv)
    return bool(identity and _active_record(private, identity[2]))


def active_package_versions(argv, private):
    identity = package_identity(argv)
    active = _active_record(private, identity[2]) if identity else None
    if active:
        return _provenance("npm" if identity[0] == "npx" else "python", active[1])
    return baked_package_versions(argv)


def baked_package_versions(argv, manifest=None):
    """Return pinned npm versions shipped in the executor image for this request."""
    identity = package_identity(argv)
    if not identity or identity[0] != "npx":
        return []
    manifest = (
        Path(manifest)
        if manifest
        else Path(__file__).resolve().parents[2] / "dependencies/mcp/package.json"
    )
    try:
        dependencies = json.loads(manifest.read_text()).get("dependencies", {})
    except (OSError, json.JSONDecodeError, AttributeError):
        return []
    packages, _, _ = npm_request(argv)
    versions = []
    for spec in packages:
        name = (
            spec.rsplit("@", 1)[0]
            if spec.startswith("@") and "@" in spec[1:]
            else (spec.split("@", 1)[0] if not spec.startswith("@") else spec)
        )
        version = dependencies.get(name)
        if isinstance(version, str) and version:
            versions.append(name + "@" + version)
    return sorted(set(versions))


def _versioned_request(argv, version=None):
    """Build an update request; explicit versions are applied to install specs only."""
    kind = Path(argv[0]).name
    if kind == "npx":
        packages, executable, extra = npm_request(argv)
        if version is not None:
            if len(packages) != 1:
                raise ValueError("Explicit npm version requires exactly one package")
            base = packages[0].rsplit("@", 1)[0] if "@" in packages[0][1:] else packages[0]
            packages[0] = base + "@" + version
        if executable is None:
            return ["npx", "-y", *packages, *extra]
        package_options = sum((["--package", item] for item in packages), [])
        return ["npx", *package_options, "--", executable, *extra]
    if kind == "uvx":
        requirements, executable, python, extra = python_request(argv)
        if version is not None:
            base = re.split(r"[<>=!~]", requirements[0])[0]
            requirements[0] = base + "==" + version
        return [
            "uvx",
            "--from",
            requirements[0],
            *sum((["--with", item] for item in requirements[1:]), []),
            "--python",
            python,
            executable,
            *extra,
        ]
    raise ValueError("Only npx and uvx MCP packages can be updated")


def package_version(argv):
    """Whether configured package specs are floating or explicitly pinned."""
    kind = Path(argv[0]).name
    if kind == "npx":
        packages, _, _ = npm_request(argv)
        return (
            "floating"
            if any(
                "@" not in item[1:] or item.rsplit("@", 1)[-1] in {"latest", "*"}
                for item in packages
            )
            else "pinned"
        )
    if kind == "uvx":
        requirements, _, _, _ = python_request(argv)
        return (
            "floating"
            if any(not re.search(r"==[^,]+$", item) for item in requirements)
            else "pinned"
        )
    return "unsupported"


def _provenance(kind, recipe):
    versions = []
    if kind == "npm":
        for path, item in recipe.get("lock", {}).get("packages", {}).items():
            name = item.get("name") or path.removeprefix("node_modules/")
            if name and item.get("version"):
                versions.append(name + "@" + item["version"])
    else:
        versions = [line for line in recipe.get("requirements", "").splitlines() if "==" in line]
    return sorted(set(versions))


def update_package(argv, private, env, *, cache=None, version=None, smoke=None):
    """Stage, smoke-test, and atomically activate one profile package generation."""
    identity_data = package_identity(argv)
    if identity_data is None:
        raise ValueError("Only npx and uvx MCP packages can be updated")
    kind, original_request, identity = identity_data
    update_argv = _versioned_request(argv, version)
    request = npm_request(update_argv) if kind == "npx" else python_request(update_argv)
    records = private / "package-recipes"
    records.mkdir(mode=0o700, parents=True, exist_ok=True)
    generations = private / "package-generations" / identity
    generations.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache = cache or private / ".cache/packages"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (records / (identity + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old_active = _active_record(private, identity)
        generation = uuid.uuid4().hex
        stage = Path(tempfile.mkdtemp(prefix="update-", dir=cache))
        try:
            recipe = (
                install_npm(stage, request, None, env)
                if kind == "npx"
                else install_python(stage, request, None, env, refresh=True)
            )
            command = _locked_command(kind, recipe, request, stage)
            if smoke is not None:
                smoke(command, env)
            recipe = {**recipe, "identity": identity, "generation": generation}
            record = generations / (generation + ".json")
            write_json(record, recipe)
            destination = cache / (identity + "-" + generation)
            _replace_directory(stage, destination)
            write_json(
                _pointer_path(private, identity),
                {"identity": identity, "generation": generation},
            )
            return {
                "status": "updated",
                "generation": generation,
                "activation": "next-connection",
                "versions": _provenance("npm" if kind == "npx" else "python", recipe),
                "previous_generation": old_active[0]["generation"] if old_active else None,
            }
        except Exception:
            if stage.exists():
                log = stage / "installation.log"
                if log.is_file():
                    log.replace(cache / (identity + ".failure.log"))
                shutil.rmtree(stage, ignore_errors=True)
            raise


def smoke_mcp(command, env, *, cwd=None, timeout=20):
    """Initialize MCP and probe whichever tools/resources/prompts it advertises."""
    import queue
    import threading

    process = subprocess.Popen(
        command,
        env=env,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        text=True,
        bufsize=1,
    )
    messages = queue.Queue()

    def read_output():
        try:
            for line in process.stdout:
                messages.put(line)
        finally:
            messages.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()

    def receive(expected, deadline):
        while time.monotonic() < deadline:
            try:
                line = messages.get(timeout=max(0.01, min(0.25, deadline - time.monotonic())))
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            if line is None:
                break
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == expected:
                if "error" in message:
                    raise ValueError("MCP smoke test returned a protocol error")
                return message.get("result")
        raise ValueError("MCP smoke test timed out or returned no response")

    try:
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "hermes-mcp-update", "version": "1"},
                    },
                }
            )
            + "\n"
        )
        process.stdin.flush()
        result = receive(1, time.monotonic() + timeout)
        if not isinstance(result, dict) or not isinstance(result.get("protocolVersion"), str):
            raise ValueError("MCP initialize response was invalid")
        capabilities = result.get("capabilities", {})
        if not isinstance(capabilities, dict):
            raise ValueError("MCP initialize capabilities were invalid")
        process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        probes = (
            ("tools", "tools/list", "tools"),
            ("resources", "resources/list", "resources"),
            ("prompts", "prompts/list", "prompts"),
        )
        request_id = 2
        for capability, method, result_key in probes:
            if capability not in capabilities:
                continue
            process.stdin.write(
                json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": {}})
                + "\n"
            )
            process.stdin.flush()
            response = receive(request_id, time.monotonic() + timeout)
            if not isinstance(response, dict) or not isinstance(response.get(result_key), list):
                raise ValueError(f"MCP {method} response was invalid")
            request_id += 1
        process.stdin.flush()
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def prepare(argv, private, env, *, cache=None):
    """Select one complete install; failed installs never replace a usable one."""
    identity_data = package_identity(argv)
    if identity_data is None:
        return argv
    kind, request, identity = identity_data
    records = private / "package-recipes"
    records.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache = cache or private / ".cache/packages"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = cache / identity
    recipe_path = records / (identity + ".json")
    with (records / (identity + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        active = _active_record(private, identity)
        if active:
            pointer, recipe = active
            generation = pointer["generation"]
            destination = cache / (identity + "-" + generation)
            if not (destination.is_dir() and (destination / recipe["binary"]).is_file()):
                stage = Path(tempfile.mkdtemp(prefix="restore-", dir=cache))
                try:
                    rebuilt = (
                        install_npm(stage, request, recipe, env)
                        if kind == "npx"
                        else install_python(stage, request, recipe, env)
                    )
                    # Keep the durable lock and generation identity selected by the pointer.
                    if rebuilt.get("binary") != recipe.get("binary"):
                        raise ValueError("MCP package executable changed while rebuilding its lock")
                    _replace_directory(stage, destination)
                except Exception:
                    if stage.exists():
                        shutil.rmtree(stage)
                    raise
            return _locked_command(kind, recipe, request, destination)
        if destination.is_dir() and recipe_path.is_file():
            recipe = json.loads(recipe_path.read_text())
        else:
            recipe = json.loads(recipe_path.read_text()) if recipe_path.exists() else None
            stage = Path(tempfile.mkdtemp(prefix="install-", dir=cache))
            try:
                recipe = (
                    install_npm(stage, request, recipe, env)
                    if kind == "npx"
                    else install_python(stage, request, recipe, env)
                )
                _replace_directory(stage, destination)
                write_json(recipe_path, recipe)
                (cache / (identity + ".failure.log")).unlink(missing_ok=True)
            except Exception:
                log = stage / "installation.log"
                if log.is_file():
                    log.replace(cache / (identity + ".failure.log"))
                raise
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        binary = destination / recipe["binary"]
        if not binary.is_file():
            raise ValueError("MCP package executable is missing")
        # uv scripts embed the venv's original staging path; invoke through the
        # relocated interpreter instead of relying on that shebang.
        prefix = [str(destination / "bin/python"), str(binary)] if kind == "uvx" else [str(binary)]
        return [*prefix, *request[-1]]


def install_npm(stage, request, recipe, env):
    stage = stage.resolve()
    packages, executable, _ = request
    log = stage / "installation.log"
    if recipe:
        write_json(stage / "package.json", recipe["manifest"])
        write_json(stage / "package-lock.json", recipe["lock"])
    else:
        write_json(
            stage / "package.json",
            {"name": "hermes-mcp-install", "version": "1.0.0", "private": True},
        )
        run(
            [
                "npm",
                "install",
                "--prefix",
                str(stage),
                "--package-lock-only",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--prefer-online",
                "--",
                *packages,
            ],
            env=env,
            log=log,
            cwd=stage,
        )
    run(
        ["npm", "ci", "--prefix", str(stage), "--no-audit", "--no-fund"],
        env=env,
        log=log,
        cwd=stage,
    )
    if executable is None:
        dependencies = json.loads((stage / "package.json").read_text()).get("dependencies", {})
        if len(dependencies) != 1:
            raise ValueError("Specify the npm executable with --package")
        name = next(iter(dependencies))
        manifest = json.loads((stage / "node_modules" / name / "package.json").read_text())
        bins = manifest.get("bin", {})
        if isinstance(bins, str):
            executable = name.rsplit("/", 1)[-1]
        elif len(bins) == 1:
            executable = next(iter(bins))
        elif name.rsplit("/", 1)[-1] in bins:
            executable = name.rsplit("/", 1)[-1]
        else:
            raise ValueError("Specify the npm executable with --package")
    binary = Path("node_modules/.bin") / executable
    if not (stage / binary).is_file():
        raise ValueError("The npm package does not provide the configured executable")
    return {
        "kind": "npm",
        "binary": str(binary),
        "manifest": json.loads((stage / "package.json").read_text()),
        "lock": json.loads((stage / "package-lock.json").read_text()),
    }


def install_python(stage, request, recipe, env, *, refresh=False):
    requirements, executable, python, _ = request
    log = stage / "installation.log"
    run(["uv", "venv", "--python", python, str(stage)], env=env, log=log)
    requirements_file = stage / "requirements.txt"
    if recipe:
        requirements_file.write_text(recipe["requirements"] + "\n")
        args = ["--requirement", str(requirements_file)]
    else:
        args = ["--", *requirements]
    refresh_args = ["--refresh"] if refresh and recipe is None else []
    run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(stage / "bin/python"),
            *refresh_args,
            *args,
        ],
        env=env,
        log=log,
    )
    frozen = run(["uv", "pip", "freeze", "--python", str(stage / "bin/python")], env=env, log=log)
    if not (stage / "bin" / executable).is_file():
        raise ValueError("The Python package does not provide the configured executable")
    return {
        "kind": "python",
        "binary": "bin/" + executable,
        "requirements": frozen,
        "python": python,
    }


def installation_recipe(argv, private, baked=Path("/opt/mcp")):
    """Export exact dependency locks, never installed files or process credentials."""
    identity = package_identity(argv)
    if not identity:
        return None
    kind, request, key = identity
    active = _active_record(private, key)
    legacy = private / "package-recipes" / (key + ".json")
    recipe = active[1] if active else json.loads(legacy.read_text()) if legacy.is_file() else None
    if recipe:
        return {
            "kind": kind,
            "identity": key,
            "recipe": {k: v for k, v in recipe.items() if k not in {"identity", "generation"}},
        }
    if kind == "npx" and (baked / "package-lock.json").is_file():
        packages = request[0]
        name = (
            packages[0].rsplit("@", 1)[0]
            if len(packages) == 1 and "@" in packages[0][1:]
            else packages[0]
            if len(packages) == 1
            else None
        )
        if name and (baked / "node_modules" / name / "package.json").is_file():
            module = json.loads((baked / "node_modules" / name / "package.json").read_text())
            bins = module.get("bin", {})
            binary = request[1] or (
                name.rsplit("/", 1)[-1]
                if isinstance(bins, str)
                else next(iter(bins))
                if len(bins) == 1
                else name.rsplit("/", 1)[-1]
            )
            return {
                "kind": kind,
                "identity": key,
                "recipe": {
                    "binary": "node_modules/.bin/" + binary,
                    "manifest": json.loads((baked / "package.json").read_text()),
                    "lock": json.loads((baked / "package-lock.json").read_text()),
                },
            }
    return None


def restore_package(argv, private, env, exported, *, cache=None, smoke=None):
    """Rebuild a versioned lock into a new generation; publish only after its handshake."""
    kind, request, identity = package_identity(argv)
    if (
        not isinstance(exported, dict)
        or exported.get("identity") != identity
        or exported.get("kind") != kind
    ):
        raise ValueError("Installation recipe does not match the configured package")
    recipe = exported["recipe"]
    binary = Path(recipe["binary"])
    if binary.is_absolute() or ".." in binary.parts:
        raise ValueError("Invalid recipe executable")
    request = list(request)
    if kind == "npx":
        request[1] = binary.name
    records = private / "package-recipes"
    records.mkdir(mode=0o700, parents=True, exist_ok=True)
    generations = private / "package-generations" / identity
    generations.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache = cache or private / ".cache/packages"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (records / (identity + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        generation = uuid.uuid4().hex
        stage = Path(tempfile.mkdtemp(prefix="restore-", dir=cache))
        try:
            rebuilt = (
                install_npm(stage, request, recipe, env)
                if kind == "npx"
                else install_python(stage, request, recipe, env)
            )
            if rebuilt["binary"] != recipe["binary"]:
                raise ValueError("Recipe executable changed")
            command = _locked_command(kind, rebuilt, request, stage)
            if smoke:
                smoke(command, env)
            rebuilt = {**rebuilt, "identity": identity, "generation": generation}
            write_json(generations / (generation + ".json"), rebuilt)
            _replace_directory(stage, cache / (identity + "-" + generation))
            write_json(
                _pointer_path(private, identity),
                {"identity": identity, "generation": generation},
            )
            return {
                "status": "restored",
                "generation": generation,
                "activation": "next-connection",
                "versions": _provenance("npm" if kind == "npx" else "python", rebuilt),
            }
        finally:
            if stage.exists():
                shutil.rmtree(stage)
