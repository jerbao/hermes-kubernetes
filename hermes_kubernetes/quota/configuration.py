"""Generate a local-only management configuration from a mounted secret."""

import argparse
from pathlib import Path


def prepare(source, key_file, destination):
    import yaml

    value = yaml.safe_load(source.read_text())
    key = key_file.read_text().strip()
    if len(key) < 32:
        raise ValueError("Management key must contain at least 32 characters")
    management = value.setdefault("management", {})
    management.update({"secret-key": key, "allow-remote": False, "disable-control-panel": True})
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(yaml.safe_dump(value, sort_keys=False))
    destination.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source, args.key_file, args.output)


if __name__ == "__main__":
    main()
