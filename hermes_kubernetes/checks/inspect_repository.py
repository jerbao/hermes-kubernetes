"""Inspect publication state and workflow handles without accessing running Hermes."""

import argparse, json, subprocess


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repository", required=True)
    a = p.parse_args()

    def gh(*args):
        return json.loads(subprocess.check_output(["gh", *args], text=True))

    print(
        json.dumps(
            {
                "repository": gh(
                    "repo", "view", a.repository, "--json", "nameWithOwner,visibility,url"
                ),
                "runs": gh(
                    "run",
                    "list",
                    "--repo",
                    a.repository,
                    "--limit",
                    "4",
                    "--json",
                    "databaseId,status,conclusion,name,url",
                ),
                "local_changes": subprocess.check_output(
                    ["git", "status", "--porcelain"], text=True
                ).splitlines(),
            }
        )
    )


if __name__ == "__main__":
    main()
