# Plugins in the correct profile and runtime

Run from `~/.hermes` on the SSH terminal. The operations bridge executes the
native CLI in the `operations` container with the Hermes user and active PVC.
Always specify `--profile` before the command; otherwise another persisted
profile selection may apply.

```sh
python3 hermes.py runtime exec --timeout 3600 -- \
  hermes --profile default plugins install IDENTIFIER --yes-deps --enable
python3 hermes.py runtime exec -- hermes --profile default plugins list --user --json
```

Use a catalog name, Git URL or `owner/repo`. `--ref` accepts a full 40-character
Git commit. `exec` has no interactive stdin. `--yes-deps` consents to installing
the requested dependencies; `--enable` requests activation. `--no-enable` installs
without activation. `--no-deps` leaves the plugin disabled and without its
dependencies; do not report this as a functional installation.

Portable Agent Plugins v1 have their own admission rules and may remain disabled.
Inspect the returned registry and name, then explicitly enable when appropriate.
Preserve admission, permission and tool replacement errors. Do not silently add
`--force`, `--allow-removed` or `--allow-tool-override`. Downloading source alone
does not prove success. Verify only the affected plugin using a script.

```sh
python3 hermes.py runtime exec -- hermes --profile default plugins disable NAME
python3 hermes.py runtime exec -- hermes --profile default plugins enable NAME
python3 hermes.py runtime exec -- hermes --profile default plugins check-updates --json
python3 hermes.py runtime exec --timeout 3600 -- hermes --profile default plugins update NAME
python3 hermes.py runtime exec -- hermes --profile default plugins remove NAME
```

`check-updates` only queries; `update NAME` changes the selected plugin. Catalog
installs follow their pins. Git installs pinned by SHA require an explicit new
revision, as reported by the native manager. Avoid arbitrary `git pull` in plugin
directories. When confirmation requires interactive stdin, use the native GUI/API
consent flow rather than simulating responses or editing internal manager state.

Configuration remains available in **Capabilities → Plugins**. Native JSON-RPC
uses `plugins.manage` with `list`, `install`, `toggle`, `settings`, `update` and
`remove`, in the authenticated profile using the installed API contract. The
operations bridge is a separate command interface.

Source lives in each profile's `HERMES_HOME/plugins`. Native PM stores tools,
locks and generations on the PVC. Avoid global pip, the debug checkout's virtual
environment and temporary installations in `/opt/hermes`.

Installation/activation may reload hooks. Tools and prompts follow native
new-session behavior; disabling may leave handlers in an existing session.
Respect `restart_required` and explain pending activation. Kubernetes supervises
the gateway: do not start another gateway on the terminal, kill processes by name
or use terminal systemd to restart a pod. Deployment restarts require cluster
administration.

Electron plugins may need separate installation in the user's Hermes Desktop.
Backend installation does not install that component. Check display, Docker,
local path and external process dependencies against the terminal/pod boundary.

The controller records sanitized configuration, source, revisions and locks in
`customizations/<profile>/`. Credentials, databases and personal data stay in
private state and encrypted backups. This projection complements the native manager.
