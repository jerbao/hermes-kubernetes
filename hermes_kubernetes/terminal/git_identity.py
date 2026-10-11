"""Configure assistant attribution without replacing user identity or Git hooks."""

import argparse
import json
import re
import shlex
import subprocess


def setting(key, value=None):
    command = ["git", "config", "--global", key]
    if value is not None:
        command.append(value)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode not in {0, 1} or value is not None and result.returncode:
        raise ValueError("Cannot configure Git identity: " + key)
    return result.stdout.strip() or None


def configure(name, email):
    if not name.strip() or any(c in name for c in "\r\n<>"):
        raise ValueError("Supply the assistant's display name")
    if not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+", email or ""):
        raise ValueError("Supply the assistant's existing commit email; never invent one")
    if not setting("user.name") or not setting("user.email"):
        raise ValueError("Configure the owner's Git user.name and user.email first")
    trailer = shlex.quote(f"Co-authored-by: {name} <{email}>")
    setting("hermes.coauthorName", name)
    setting("hermes.coauthorEmail", email)
    setting(
        "alias.hermes-commit",
        "!f() { git -c trailer.ifexists=addIfDifferent commit --trailer " + trailer + ' "$@"; }; f',
    )
    return report()


def report():
    return {
        "author_name": setting("user.name"),
        "author_email": setting("user.email"),
        "coauthor_name": setting("hermes.coauthorName"),
        "coauthor_email": setting("hermes.coauthorEmail"),
        "configured": bool(setting("alias.hermes-commit") and setting("hermes.coauthorEmail")),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--coauthor-name", default="Hermes")
    parser.add_argument("--coauthor-email")
    args = parser.parse_args(argv)
    print(
        json.dumps(report() if args.check else configure(args.coauthor_name, args.coauthor_email))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
