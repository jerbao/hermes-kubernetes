# Runtime structure

Use `python3 hermes.py runtime info`, instance policy and the public dependency's
upstream lock for current paths and versions. Infrastructure values belong in
private manifests, never in shared defaults.

| Component | Responsibility and persistence |
|---|---|
| SSH terminal | Shell, projects, Docker and desktop; durable home/workspace independent of the OS |
| Hermes pod | AlmaLinux x86-64-v2 image; active state on a PVC |
| `gateway` | Channels, sessions and agent; one active production gateway |
| `dashboard` | Desktop API, plugins, Connectors and SSH directory browsing |
| `diagnostics` | Selected logs and provenance copied to private terminal storage |
| `operations` | Private bridge initiated from the pod over SSH, exposing a terminal socket |
| MCP `api` | Optional Alpine executor with its own packages/PVC |
| MCP `browser` | Optional separate Alpine executor with a configured browser backend |
| MCP `workspace` | Optional AlmaLinux/glibc executor with indexes and authorized project mounts |
| CLIProxyAPI | Independently administered proxy configured by the instance |
| Network | Existing cluster connectivity or dedicated tailnet; explicit destination policies |

Recreating the OS does not authorize deleting persistent home storage or PVCs.
Persistence survives recreation; backups recover deletions.

## Paths and repositories

On the terminal, `~/.hermes` is the private Git root. `hermes-agent/` is an ignored,
independent checkout reconstructed from pinned upstream plus patches. Shared code
is cached in `.local/platform/<revision>/`, selected by `platform.lock.json`.
`.local/` also contains private diagnostics, development environments and
controller state. Terminal projections do not replace active pod state.

Bridge scopes are `state` (active HERMES_HOME), `source` (core image code) and
`integration` (distribution). `info` reports their roots; image defaults are
`/opt/data`, `/opt/hermes` and `/opt/hermes-personal`. The latter two are read-only.
The terminal socket defaults to `~/.hermes/.local/runtime/control.sock`;
`--socket` selects another socket.

| Repository | Responsibility |
|---|---|
| Public distribution | Shared package, focused patches, images and release protocol |
| Private instance | Policy, sanitized customizations, lock and deployment overlay |
| Private application manifests | PVCs, optional executors, networking, encrypted Secrets and backup jobs |
| Private infrastructure | Cluster, terminal provisioning/recreation and persistent mounts |

The agent has user permissions and bridge access. Do not copy Kubernetes or
Proxmox administrative credentials to the terminal as a shortcut.

## State and profiles

Prefer native tools and GUI/API operations. Bridge `list`, `stat`, `get` and `put`
operate on actual state. Retrieve a private copy before manual editing and write
using its previous hash; `absent` only creates a new file.

```sh
python3 hermes.py runtime stat config.yaml --sha256
python3 hermes.py runtime get config.yaml --output /tmp/config-pod.yaml
python3 hermes.py runtime put config.yaml --input /tmp/config-pod.yaml --expected PREVIOUS_SHA256
```

In `state`, `config.yaml` belongs to default; other profiles use
`profiles/PROFILE/`. Every profile owns configuration, plugins and `.env`.
Never share `.env` through symlinks. Explicitly select
`hermes --profile PROFILE ...` inside `python3 hermes.py runtime exec`.

Native profile CRUD and descriptions are available through
`python3 hermes.py runtime exec -- hermes profile ...`; use `profile --help`
and command-specific help before destructive changes. The Git mirror stores the
complete native profile metadata mapping in
`customizations/PROFILE/profile.json`. It maps to the default profile's root
`profile.yaml` or `profiles/PROFILE/profile.yaml` in the active HERMES_HOME.
Unknown keys are retained for plugin-owned metadata; JSON-compatible values are
supported. Secrets are sanitized into private PVC references. Do not share
profile `.env` files or remove a profile through this metadata file.

SQLite/WAL/SHM cannot be transferred or overwritten as ordinary files. Use
`sqlite-backup` for live databases. GUI directory browsing uses terminal SSH;
these folders do not show where backend plugins were installed. Host, user,
key and cwd must follow effective profile policy.

## MCPs

Use the profile's native Connectors/API. HTTP connects remotely; the
`mcp.stdio_executor` hook routes stdio to isolated executors. Packages are prepared
there, outside Hermes Python and the terminal.

For an unknown package, assess requirements and specify an argument when needed:
`--hermes-executor=api`, `--hermes-executor=workspace` or
`--hermes-executor=browser`. The bridge removes this flag before launch.
Automatic routing recognizes mapped packages and authorized paths; it cannot
infer arbitrary dependencies. `vm` requires a separately provisioned MCP target
and key; it never falls back to terminal credentials. Do not assume it is enabled.

```sh
python3 hermes.py runtime exec --scope integration --timeout 3600 -- \
  /opt/hermes/.venv/bin/python /opt/hermes-personal/hermes_kubernetes/mcp/updates.py plan --all
```

Review the plan, then use `apply NAME` or a requested `apply --all`. Installation
or handshake failure preserves the previous generation. HTTP versions are
provider-managed. Routing details are in the shared capabilities documentation.

## Git, updates and backups

The controller reconciles Git, native state and baselines, preserving conflicts.
It projects sanitized profile metadata, skills, plugin source, persona,
configuration references, provenance and PM/MCP locks to `customizations/<profile>/`. On `main`, admitted
changes publish after 120 stable seconds. Branches, conflicts and distribution
changes require explicit review. Editing a projection does not prove application.

The terminal's user-level `hermes-workspace-sync.timer` runs every minute.
Use `python3 hermes.py sync-status` to confirm the user timer is enabled/active,
see the latest result and determine whether synchronization is still running.
Use `python3 hermes.py sync --publish` for an explicit run. A nonzero result or
reported conflict means the live and Git copies need review; it is not success.

Customizations do not require image builds. Shared/core changes pass public CI,
release publication, private update PR, digest adoption and ArgoCD. Releases are
locked; avoid upstream `git pull` and `hermes update` in the read-only pod.

Official patched skills and distribution skills use three-way reconciliation:
previous baseline, candidate source and profile files. Release review preserves
compatible edits and blocks conflicts with GitHub alerts. Startup prepares the
complete plan before writing managed skills. Private baselines live in
`state/versioning/managed-skills/`. User deletions stay deleted; skills retired
upstream are retained as personal skills.

Files matching managed sources are reconstructed from images without redundant
Git copies. Profile edits remain bidirectional. The controller waits for matching
source/distribution hashes before applying dependent changes. Do not bypass
`waiting-for-runtime` by installing core code or skills on the terminal.

Git/images exclude databases, history, credentials and personal data. Private
references resolve through the PVC vault. Hermes state, workspace and executor
data have separate encrypted Drive backups selected by policy. Do not archive
the entire home/cache or trigger a backup for every operation. Restore into an
empty area and coordinate writers before replacing production state.
