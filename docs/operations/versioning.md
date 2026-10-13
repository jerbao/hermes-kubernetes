# State and versioning

Shared source belongs to the public distribution. Per-profile configuration,
complete `profile.yaml` metadata, personas, skills, plugin sources and installation
locks belong to the private instance under `customizations/<profile>`. The
metadata projection is canonical JSON at `profile.json`; reconciliation translates
it to the native YAML file while preserving unknown profile and plugin fields.
Native changes are reconciled with Git and a private baseline; concurrent edits
are preserved as explicit conflicts. Profile creation/deletion remains owned by
the native profile manager; deleting `profile.json` only removes profile metadata.

Run `python3 hermes.py sync-status` from the terminal Git root to inspect the
user-level synchronization timer, the last/next timer trigger, and the service's
current or failed state. `python3 hermes.py sync --publish` runs reconciliation
and publication immediately and prints a JSON result. Never infer that a local
projection is live just because the file was edited.

The SSH terminal's `~/.hermes/config.yaml` is not the active pod configuration.
Do not edit it to change runtime settings. If a file tool refuses a write because
a path is outside its safe root, stop; do not retry with shell or Python. Use the
native Desktop/API or `python3 hermes.py runtime exec -- hermes --profile PROFILE config set ...`
for live runtime changes. The safe root is a boundary, not an obstacle to work around.


SQLite/WAL/SHM, OAuth sessions, .env, credentials and dependency directories are
never committed. Credential references in sanitized projections are resolved
from a private PVC vault. Git and images do not replace durable-state backups.
A plugin installation uses the native package manager on the pod through the
interface or operations bridge, and does not require rebuilding the image.

At boot the pod exports `HERMES_ENVIRONMENT_HINT`, built from this installation's
`instance.json` by `hermes_kubernetes/runtime/environment_hint.py`, so the agent knows it
runs in a pod, where its terminal and MCP executors are, and that the safe root is a
boundary. Set the variable yourself to override the generated text. Changing the topology
changes the hint only after a new pod rolls out.
