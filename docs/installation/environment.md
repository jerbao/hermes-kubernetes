# Configure an installation with .env

Each private instance has one ignored **installer `.env`**. The tracked
`.env.example` lists every supported setting. The installer reads this file as
data, without shell execution or variable expansion; do not `source` it.
Environment variables with the same names can override file values.

This file configures infrastructure. Native Hermes profile `.env` files live in
the runtime PVC, remain independently editable through Desktop/API and are never
symlinked together. The installer seeds the default profile; existing credentials
are preserved. Subsequent installer changes reconcile only values still owned by
the installer. A conflicting Desktop edit blocks that credential update with an
explicit error. Named profiles retain their independent values.

## Quick start

Clone the public distribution, then generate a private installation:

```sh
python3 -m hermes_kubernetes.instances.generator --directory ../my-hermes --release v0.2.0
cd ../my-hermes
python3 hermes.py setup init
# Edit .env. Only this installation's settings and private credential file paths.
python3 hermes.py setup plan
python3 hermes.py setup configure
```

`configure` generates and commits the non-secret desired state. It refuses staged
unrelated changes and manual edits to its generated files. It leaves other files
alone. `plan` does not contact infrastructure or write anything unless `--infra`
is explicitly selected. It reports missing prerequisites without printing secrets.

To use private Git synchronization and update PRs, create the private repository
before installation and set `INSTANCE_REPOSITORY` to its GitHub SSH URL:

```sh
gh repo create my-hermes --private --source . --remote origin --push
# If GitHub CLI used HTTPS, align origin with INSTANCE_REPOSITORY:
git remote set-url origin git@github.com:YOUR_OWNER/my-hermes.git
python3 hermes.py setup install
python3 hermes.py setup connect
```

Open `http://127.0.0.1:9119`, or use that address in Hermes Desktop while
`setup connect` is running. The dashboard token is in
`.local/credentials/dashboard-token`, unless you supplied `DASHBOARD_TOKEN`.
The Service has no public ingress by default.

The private bootstrap prepares an ignored Python environment with pinned PyYAML
when setup needs it. Operator tools are Python 3.12+, Git, OpenSSH and kubectl;
managed guests additionally require OpenTofu and Ansible. The CLI checks tools
and credentials before provisioning. During distribution development, the same
commands are available through `scripts/setup.sh --root ../my-hermes ACTION`.
The installer requires a published distribution v0.2.0 or newer.

## Minimum configuration for an existing terminal

Set these in `.env`:

```dotenv
ASSISTANT_NAME="My Assistant"
KUBERNETES_NAMESPACE=my-hermes
MODEL=your-model-id
MODEL_BASE_URL=http://cli-proxy-api.proxy.svc.cluster.local:8317/v1
MODEL_API_KEY="your-provider-key"
MODEL_SERVICE_NAMESPACE=proxy
MODEL_SERVICE_SELECTOR=app.kubernetes.io/name=cli-proxy-api
TERMINAL_KIND=existing
TERMINAL_HOST=192.0.2.10
TERMINAL_USER=hermes
TERMINAL_WORKSPACE=/home/hermes/.hermes
TERMINAL_SSH_KEY_FILE=.local/credentials/terminal_ed25519
TERMINAL_KNOWN_HOSTS_FILE=.local/credentials/terminal_known_hosts
```

Authorize the runtime public key on that terminal. Obtain its SSH host public
key over an already trusted connection, then pin it in the known_hosts file.
The installer never trusts an unauthenticated `ssh-keyscan` result or disables
host-key verification. Private key and credential files must be mode 0600 or 0400.
The existing terminal needs Python 3.12+, Git and outbound access to GitHub. Its
workspace must already be durable if you intend to discard its operating system.

`MODEL_SERVICE_SELECTOR` is the model server's **pod label**, not its Service
name. For an external provider, clear `MODEL_SERVICE_NAMESPACE` and set
`MODEL_EGRESS_CIDRS` to its allowed address ranges. Terminal IPs become explicit
/32 or /128 rules automatically. Hostnames require `TERMINAL_EGRESS_CIDRS`;
the installer does not resolve a hostname once and pretend its IP will stay fixed.

`RUNTIME_ENV_FILE` optionally imports native channel/integration variables for
the default profile. It is an independent, private file; put Telegram/Discord
credentials there or configure channels in Desktop after installation. The
installer never transfers its own `.env` to the terminal or the runtime PVC.
For example, `MODEL_API_KEY` becomes `CLI_PROXY_API_KEY`, rather than exposing
Proxmox settings or Drive recovery keys to the agent.

Kubernetes needs an enforcing NetworkPolicy CNI and a suitable default
StorageClass, or `STATE_STORAGE_CLASS` explicitly set. `STATE_SIZE` controls the
runtime PVC. The installer does not create the Kubernetes cluster, inference
provider, storage cluster or a dedicated tailnet. Existing cluster Tailscale
connectivity can provide the configured terminal route.

## Create a Proxmox LXC or VM

Select `TERMINAL_KIND=lxc` or `vm`. Fill the Proxmox, address and workspace
sections of `.env.example`; internal files do not need editing.

The LXC installer downloads the pinned Ubuntu 26.04 appliance and verifies its
SHA-512. It creates an unprivileged container with Docker nesting, keyctl, FUSE,
zero swap and the configured memory limit. FUSE/keyctl require `root@pam`
authentication; put that password in the private file selected by
`PROXMOX_ROOT_PASSWORD_FILE`. The VM path clones your Ubuntu cloud-init template
selected by `PROXMOX_VM_TEMPLATE_ID`, with Python, sudo and QEMU guest agent
already installed. The VM CPU is explicitly **x86-64-v2**.

Both paths require a dedicated guest ID, static `TERMINAL_IPV4_CIDR`, gateway,
node/storage names and authenticated SSH administration of that Proxmox node.
VM template and target must be on the selected node. Node SSH reads the initial
guest host key using `pct exec` or `qm guest exec`; Ansible then installs a
preserved instance-specific host key. Runtime and administrator SSH keys differ;
the runtime never receives the Proxmox/admin keys.

Managed guests mount a pre-existing dedicated CephFS subvolume with a limited
client, selected by `WORKSPACE_CEPH_*`. Provide that client's credential file;
never supply a cluster admin key. The installer does not create or own the
CephFS data, so it cannot delete it when replacing the OS. CephFS FUSE works for
both VM and unprivileged LXC. SSH and Docker require the durable mount at boot.
Reuse an existing terminal when your storage uses another backend.

```sh
python3 hermes.py setup plan --infra
python3 hermes.py setup install
```

`plan --infra` initializes a private OpenTofu state, validates the provider schema
and saves a plan without applying it. `install` generates a fresh plan and checks
it for destructive actions before applying. `prevent_destroy` also blocks guest
replacement. There is deliberately no implicit destroy/recreate operation.
OpenTofu configuration, state, plans and generated identities stay under ignored
`.local/installation` and `.local/credentials`. Preserve those directories and
the `.env` outside the disposable guest to administer it again.

`INSTANCE_REPOSITORY`, `GIT_SSH_KEY_FILE` and `GIT_KNOWN_HOSTS_FILE` enable private
Git synchronization. Use a deploy key scoped to this instance repository. The
installer preserves the source Git history via a bundle, then enables the user
sync timer; subsequent installs fast-forward clean worktrees and refuse divergent
or uncommitted terminal edits. Without Git access, the terminal still gets the
pinned code for inspection, and automatic publishing stays disabled.

## Google Drive backups

Set `DRIVE_OAUTH_CLIENT_FILE` to your Google **Desktop app** OAuth JSON, then:

```sh
python3 hermes.py setup drive-auth
# Save an independent recovery copy of DRIVE_RCLONE_CONFIG_FILE.
# Set BACKUP_ENABLED=true, and permit Google HTTPS egress in .env.
python3 hermes.py setup install
```

This uses the pinned rclone binary and your own OAuth client. It creates an
encrypted remote in the private file selected by `DRIVE_RCLONE_CONFIG_FILE`.
The OAuth/client and encryption recovery configuration never enter Git or command
arguments. An already configured rclone file can be supplied directly.

Backups run in a dedicated sidecar at `BACKUP_INTERVAL_HOURS`, starting one
interval after installation; installation does **not** trigger a backup. The
operations protocol produces a consistent runtime snapshot, and a separate
selected snapshot covers terminal `customizations`. Workspace consistency is
per regular file and per SQLite database: each file is staged from one open
descriptor and checked before and after its read, while each database and its
recovery files are copied and validated together. Regular files up to 8 MiB are
spooled in bounded memory; larger files spill to the backup staging volume. The
archive is a time-window capture, not a point-in-time snapshot of the entire
workspace; directory entries are enumerated once, so concurrent additions
after enumeration are included on the next run. `BACKUP_EXTRA_PATHS` adds literal paths relative to
`TERMINAL_WORKSPACE`; the installer never selects the
whole home or dependency caches implicitly. `BACKUP_KEEP` controls remote
retention per state/workspace collection. Failed jobs produce a `backup-failed`
container log and exit unsuccessfully. A workspace or SQLite snapshot that detects
a file changing during its own staging gets one bounded retry; changes to other
files after they have been staged do not invalidate those stable copies.
Persistent changes still fail without publishing a partial archive or pruning
the last verified backup.

For API HTTPS access, `INTERNET_EGRESS=external` explicitly allows TCP 80/443 to
public addresses, excluding private infrastructure and tailnet ranges. With
`restricted`, supply `API_EGRESS_CIDRS` for the APIs you need. Private SSH and model
endpoints remain separately allowlisted. MCP executors and browser/desktop
services are optional extensions; installing this base does not silently create
those unrelated services.

## Desired state and GitOps

`instance.json`, `kustomization.yaml` and `config/installation/` are sanitized,
versioned desired state. `.env`, `.local/`, native profile credentials, OAuth,
SQLite and archives are ignored. Secrets go directly to Kubernetes on stdin;
they are not rendered as tracked manifests. Credential changes update the pod's
configuration hash and roll its consumers once.

The first installation uses kubectl. Afterwards, point your existing ArgoCD
Application at the private instance overlay to reconcile reviewed manifest
commits. ArgoCD/private repository credentials remain operator-managed; this
public project's CI never gains access to your cluster. Backup and runtime
configuration changes made through the interface retain their native persistence;
reinstalling does not reset existing persona, preferences, skills or profile envs.
