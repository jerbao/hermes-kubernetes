"""Observe one GitHub workflow run; no production checks or model calls."""

import argparse, json, subprocess, time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repository", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--wait-seconds", type=int, default=0)
    a = p.parse_args()
    deadline = time.monotonic() + a.wait_seconds
    while True:
        r = json.loads(
            subprocess.check_output(
                [
                    "gh",
                    "run",
                    "view",
                    a.run,
                    "--repo",
                    a.repository,
                    "--json",
                    "status,conclusion,jobs,url",
                ],
                text=True,
            )
        )
        summary = {
            "status": r["status"],
            "conclusion": r["conclusion"],
            "url": r["url"],
            "jobs": [
                {
                    "name": j["name"],
                    "status": j["status"],
                    "conclusion": j["conclusion"],
                    "step": next(
                        (s["name"] for s in j["steps"] if s["status"] == "in_progress"),
                        None,
                    ),
                }
                for j in r["jobs"]
            ],
        }
        print(json.dumps(summary), flush=True)
        if r["status"] == "completed":
            if r["conclusion"] != "success":
                logs = subprocess.run(
                    ["gh", "run", "view", a.run, "--repo", a.repository, "--log-failed"],
                    text=True,
                    capture_output=True,
                )
                print(logs.stdout[-14000:])
            raise SystemExit(0 if r["conclusion"] == "success" else 1)
        if time.monotonic() >= deadline:
            break
        time.sleep(min(20, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    main()
