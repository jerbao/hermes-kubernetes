# Separate MCP executors

Executor images contain Node, Python and pinned packages. The locks in
`dependencies/mcp/` include transitive dependencies. Installation happens outside
Hermes Python and the SSH terminal.

Hermes retains native configuration. The stdio bridge authenticates to an executor
using SSH, sends the private definition on stdin and forwards MCP bytes unchanged.
Host keys are verified; credentials are separate from terminal SSH. HTTP retains
native remote transport.

The executor runs without root or access to Hermes state. Its only SSH command is
`hermes-mcp`; shell, SFTP and forwarding are disabled. Profiles have separate homes
and caches, but processes sharing a pod and UID are not a security boundary.
Use separate pods and policies for separate trust boundaries.

Known `npx` packages use baked binaries until an approved persistent profile
override exists. `npx` and `uvx` packages support updates without rebuilding:

```sh
python3 hermes.py runtime exec --scope integration --timeout 3600 -- \
  /opt/hermes/.venv/bin/python /opt/hermes-personal/hermes_kubernetes/mcp/updates.py plan --all
```

Review the plan, then `apply NAME` or a requested `apply --all`. `--version VERSION`
selects an explicit version; otherwise floating specifications update and fixed
pins are skipped. Definitions come from current profile `config.yaml`.
`state/hermes-ssh-mcp.json` restores originals only for servers still using the
bridge. `--definitions FILE` explicitly selects another snapshot.

The executor stages a generation, runs MCP `initialize` and advertised resource
checks without models, then switches the profile pointer. Existing connections
retain their process; reconnection adopts the new generation. Failure preserves
the previous version. npm locks and resolved Python versions stay on durable
profile storage. Installed files/caches under `/tmp` are rebuilt from those locks.
Bulk updates report each server; provider-managed HTTP returns `skipped-http`.
Credentials, configuration, indexes and server data are preserved.

No workspace is mounted by default. `cwd` requires an explicit `--workspace`
mount; escaping paths/symlinks are rejected. Codegraph requires project/index
storage. Browser tools require a browser backend and persistent data. Installing
packages alone does not establish these dependencies.

Code is under `hermes_kubernetes/executors/`, locks under `dependencies/mcp/`,
Dockerfiles under `containers/` and optional manifests under
`deploy/kubernetes/executors/`. API/browser use Alpine; Codegraph uses the
AlmaLinux x86-64-v2 glibc variant.

```sh
make test-mcp
python3 -m hermes_kubernetes.project.image_context --component mcp --output .build/mcp-image
docker build --platform linux/amd64 -t hermes-mcp:local .build/mcp-image
```

CI verifies SSH/image bootstrap, publishes digests and records them in releases.
Private overlays own deployment, networking, PVCs and encrypted Secrets. For an
unknown package, explicitly choose `--hermes-executor=api`, `workspace` or
`browser` in its arguments when mapped package/path routing is insufficient.
The bridge consumes this flag before launching the server.
