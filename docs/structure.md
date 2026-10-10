# Repository structure

The shared Python package is organized by responsibility. Import reusable logic
through its qualified module name; run commands with `python3 -m
hermes_kubernetes.<domain>.<module>`. Direct file entry points remain supported
where container supervisors or bootstrap scripts require absolute paths.

| Directory | Responsibility |
|---|---|
| `hermes_kubernetes/runtime/` | Pod startup, configuration, dependencies and health |
| `hermes_kubernetes/operations/` | Private operations protocol, server, client and SSH tunnel |
| `hermes_kubernetes/mcp/` | Routing, catalog and package update requests |
| `hermes_kubernetes/executors/` | Isolated SSH executor and package installation generations |
| `hermes_kubernetes/state/` | Sanitized state projection, profile preparation and managed skills |
| `hermes_kubernetes/backup/` | Archives, encrypted Drive storage, workspace policy and restore |
| `hermes_kubernetes/terminal/` | Terminal workspace adoption, Git synchronization and publication |
| `hermes_kubernetes/source/` | Pinned upstream checkout, patches and native test selection |
| `hermes_kubernetes/releases/` | Release manifests, registry verification, publication and alerts |
| `hermes_kubernetes/instances/` | Private scaffold, CLI and transactional release/skill adoption |
| `hermes_kubernetes/installation/` | Environment schema, planning, OpenTofu/Ansible provisioning, Secrets and deployment |
| `hermes_kubernetes/project/` | Git inventory, source review, validation and isolated image contexts |
| `hermes_kubernetes/checks/` | Reusable automated verification commands |
| `scripts/` | Shell entry points for image builds, smoke checks and local development |
| `containers/` | Dockerfiles for Hermes and MCP variants |
| `dependencies/mcp/` | Node/Python dependency manifests and locks |
| `dependencies/proxmox/` | Pinned OpenTofu provider checksums |
| `deploy/kubernetes/` | Generic Kustomize base and optional executor overlay |
| `deploy/ansible/` | Disposable terminal configuration with durable CephFS home |
| `systemd/` | Terminal controller and backup unit templates |
| `config/` | Generic configuration, tool pins and executor catalog |
| `patches/` | Focused changes to official Hermes; never patches for our own files |
| `skills/` | Distribution self-management instructions |
| `examples/instance/` | Minimal private repository scaffold with its own CLI/workflow/units |
| `tests/` | Domain-oriented tests and integration scripts; no model calls |
| `docs/` | Installation, operations and development documentation |
| `hermes-agent/` | Ignored upstream checkout plus patches, reproducible from lock |
| `.local/`, `.build/` | Ignored diagnostics, development caches and build output |

Source, locks, templates, tests and documentation are committed. Credentials,
SQLite/WAL/SHM, conversations, installed dependencies and caches are excluded.
Image contexts are selected from the Git inventory; a direct build of a native
home cannot transmit private state.

Private consumers own `instance.json`, `platform.lock.json`, `customizations/`
and deployment overlays. Shared source stays inspectable in
`.local/platform/<commit>/` without being copied into each private history.
Every runtime profile has its own `.env`.

The root `.env.example` documents installation settings. Private instances copy
it to an ignored installer `.env`; it is separate from runtime profile `.env`
files. Generated non-secret manifests live in `config/installation/` and are
committed. Operator credentials and provisioning state stay under `.local/`.

`/opt/hermes-personal` remains the container integration root for compatibility;
it contains `hermes_kubernetes/`, generic configuration and distribution skills.
It does not identify a private repository or user.
