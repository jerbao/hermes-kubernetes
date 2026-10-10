"""Install a fixed official rclone archive after checking its SHA-256."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import zipfile


def install(lock, destination):
    value = json.loads(Path(lock).read_text())
    system = {"Linux": "linux", "Darwin": "osx"}[platform.system()]
    machine = {"x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}[platform.machine()]
    target = system + "-" + machine
    version = value["version"]
    expected = value["archives"][target]
    name = f"rclone-v{version}-{target}"
    with tempfile.TemporaryDirectory(prefix="rclone-install-") as temporary:
        archive = Path(temporary) / "rclone.zip"
        subprocess.run(
            [
                "curl",
                "-fsSL",
                "--retry",
                "3",
                "--connect-timeout",
                "30",
                "--max-time",
                "300",
                "-o",
                str(archive),
                f"https://github.com/rclone/rclone/releases/download/v{version}/{name}.zip",
            ],
            check=True,
        )
        checksum = hashlib.sha256()
        with archive.open("rb") as reader:
            while chunk := reader.read(1024 * 1024):
                checksum.update(chunk)
        if checksum.hexdigest() != expected:
            raise ValueError("Official rclone archive checksum mismatch")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as reader, destination.open("xb") as output:
            with reader.open(name + "/rclone") as binary:
                shutil.copyfileobj(binary, output)
        destination.chmod(0o755)
    subprocess.run([str(destination), "version"], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    install(args.lock, args.destination)
