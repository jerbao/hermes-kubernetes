"""Authorize a private Google Drive backup remote; never print credentials."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import configparser
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile


def configure(config, *, oauth_client, rclone="rclone", reauthorize=False):
    config = Path(config).absolute()
    if any(p.is_symlink() for p in [config, *config.parents]):
        raise ValueError("Configuration must not use symlinks")
    if config.exists() and not reauthorize:
        raise FileExistsError("Keep the existing recovery key; configuration already exists")
    client = json.loads(Path(oauth_client).read_text()).get("installed", {})
    if not client.get("client_id") or not client.get("client_secret"):
        raise ValueError("Expected a Google Desktop app OAuth client JSON")
    value = configparser.RawConfigParser()
    if config.exists():
        value.read(config)
        if value.get("hermes-backup", "type", fallback="") != "crypt":
            raise ValueError("Reauthorization requires the existing encryption configuration")
    value["hermes-drive"] = {
        "type": "drive",
        "scope": "drive.file",
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
    }
    config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".drive-auth-", dir=config.parent) as temporary:
        candidate = Path(temporary) / "rclone.conf"
        with os.fdopen(
            os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as output:
            value.write(output)
        # rclone opens the browser itself. Its output can contain OAuth tokens;
        # keep it private, including on failure, and never forward it to logs.
        log = Path(temporary) / "authorization.log"
        print("Authorize Google Drive in the browser opened by rclone.", flush=True)
        with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
            result = subprocess.run(
                [
                    rclone,
                    "--config",
                    str(candidate),
                    "config",
                    "reconnect",
                    "hermes-drive:",
                    "--auto-confirm",
                ],
                stdout=output,
                stderr=output,
                timeout=900,
            )
        if result.returncode:
            raise RuntimeError("Google Drive authorization failed")
        value = configparser.RawConfigParser()
        value.read(candidate)
        if not value.has_option("hermes-drive", "token"):
            raise RuntimeError("Authorization did not produce a token")
        if not value.has_section("hermes-backup"):
            passwords = []
            for _ in range(2):
                result = subprocess.run(
                    [rclone, "obscure", "-"],
                    input=secrets.token_urlsafe(48),
                    text=True,
                    capture_output=True,
                    check=True,
                )
                passwords.append(result.stdout.strip())
            value["hermes-backup"] = {
                "type": "crypt",
                "remote": "hermes-drive:hermes-backups/default",
                "filename_encryption": "standard",
                "directory_name_encryption": "true",
                "password": passwords[0],
                "password2": passwords[1],
            }
        with os.fdopen(
            os.open(candidate, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w"
        ) as output:
            value.write(output)
            output.flush()
            os.fsync(output.fileno())
        candidate.chmod(0o600)
        if config.exists():
            old = config.with_name("before-oauth-" + secrets.token_hex(6) + ".conf")
            os.link(config, old)
            candidate.replace(config)
        else:
            os.link(candidate, config)
    print(
        "Private configuration created. Keep a copy outside the terminal and the encrypted Drive.",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path.home() / ".config/hermes-backup/rclone.conf"
    )
    parser.add_argument("--rclone", default="rclone")
    parser.add_argument("--oauth-client", required=True, type=Path)
    parser.add_argument("--reauthorize", action="store_true")
    args = parser.parse_args()
    try:
        configure(
            args.config,
            oauth_client=args.oauth_client,
            rclone=args.rclone,
            reauthorize=args.reauthorize,
        )
    except Exception as error:
        raise SystemExit("Drive configuration failed: " + type(error).__name__)
