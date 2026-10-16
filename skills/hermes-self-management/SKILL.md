---
name: hermes-self-management
description: Manage this Hermes Kubernetes instance through its active runtime, preserving private profiles and using the shared distribution for code changes.
---

The shell tool runs on an external SSH terminal. Gateway and Desktop/API run in
Kubernetes. The private instance Git repository is `~/.hermes` on the terminal;
the active `HERMES_HOME` is on the pod's PVC. Similar paths do not mean that the
terminal and pod share a Python runtime or a writable core installation.

Run workspace Git commands in the native SSH terminal as its configured owner.
`runtime exec` runs inside the pod with a different UID; it is not the place to
stage or commit terminal projects. Do not use `su` or disable Git ownership checks
to compensate for choosing the wrong executor. OS tools such as `gh` belong to
the terminal's reproducible provisioning, and authentication belongs to that user.

Keep the owner's existing Git author identity. If assistant attribution is
configured, use `git hermes-commit` with normal commit arguments. Inspect it with
`python3 hermes.py git-identity --check`; configure it using `--coauthor-name` and
`--coauthor-email` only with an existing identity supplied by the owner. Never
invent an email or reuse another assistant's identity to obtain its avatar.

## Inspect before administering the active runtime

```sh
cd ~/.hermes
python3 hermes.py runtime info
```

Read the instance name, roots and provenance. Use the requested profile explicitly,
including `default`. If the operations bridge is unavailable, report the missing
connection; do not administer an unrelated local installation instead.
Native profile-aware tools and the connected Desktop/API already act on the pod.

## Plugins, profiles and MCPs

Use the native interface or the operations bridge for production administration:

```sh
python3 hermes.py runtime exec --timeout 3600 -- \
  hermes --profile default plugins install IDENTIFIER --yes-deps --enable
```

Read [plugin management](references/plugins.md) before operating through chat.
Preserve native prompts and approvals. Use `--no-enable` when only installation
was requested, and inspect results in the same profile. Desktop-local components
still require installation on the user's Desktop.

Manage profile identity and lifecycle with Hermes' native profile commands. These
commands operate on the live pod through the bridge; use the exact requested
profile and consult native help before destructive operations:

```sh
python3 hermes.py runtime exec -- hermes profile list
python3 hermes.py runtime exec -- hermes profile show PROFILE
python3 hermes.py runtime exec -- hermes profile describe PROFILE
python3 hermes.py runtime exec -- hermes profile describe PROFILE "Description"
python3 hermes.py runtime exec -- hermes profile create --help
python3 hermes.py runtime exec -- hermes profile delete --help
```

For reproducible profile metadata, edit
`customizations/PROFILE/profile.json` in the terminal Git workspace. It mirrors
the complete mapping in the live `HERMES_HOME/profiles/PROFILE/profile.yaml`
(and the default profile's root `profile.yaml`), including fields unknown to
Hermes such as plugin-owned namespaces. `profile.json` is canonical JSON; the
sync controller converts it to native YAML and preserves unrecognized keys.
Credentials are replaced with private references; never put tokens, passwords,
or `.env` contents in plugin metadata. Native description changes and workspace
metadata edits reconcile with compare-and-swap. If both sides changed since the
last sync, the controller reports a conflict and preserves both copies.

Keep plugin-owned configuration with its source, for example
`customizations/PROFILE/plugins/PLUGIN/bots.yaml`, and have that plugin read it.
This keeps custom plugin configuration under Git ownership without adding it to
the core profile schema. Do not delete a profile by removing its customization
directory; use the native profile manager.

Never run production `pip install`, `hermes plugins install` or plugin clones in
the terminal development environment or the read-only image core. The pod's
native package manager owns persistent plugin dependencies. MCP stdio packages
belong to isolated executors, never the Hermes Python environment.

Known MCPs use the executor map. Unknown packages may require an explicit
`--hermes-executor=api`, `--hermes-executor=browser` or
`--hermes-executor=workspace` argument. The bridge removes this flag before
execution. Do not infer arbitrary package capabilities. `vm` requires its own
provisioned MCP target and key; it is not a terminal-key fallback. HTTP MCPs use
their native HTTP transport and the private installation's egress policy.

## Personalizations and source ownership

Create personal skills using `skill_manage` on the active profile. Native skills,
plugins, personas, sanitized configuration and installation locks are projected
into the private repository's `customizations/<profile>/`. No per-skill allowlist
is required. Every profile has an independent private `.env`.

The public distribution owns runtime code, patches and `skills/`. The private
repository pins that dependency in `platform.lock.json`; inspectable shared
source is cached in `.local/platform/<revision>/`. Do not edit the immutable
cache to maintain a private copy of common code. Submit common fixes in a
separate development checkout of the public repository.

Use `python3 hermes.py source` to prepare `hermes-agent/` from the pinned upstream
and patches, and `python3 hermes.py source-status` to inspect its identity. The
core checkout is ignored, independent Git source. Read its native `AGENTS.md`
before editing, preserve local work and active processes, and export intended
changes as focused patches in a public distribution development checkout.
Never reset or clean a personal home to force an update.

Check background workspace synchronization explicitly. Its systemd units belong
to the installation owner's **user** manager, so use `--user` when inspecting or
enabling them. `sync-status` reports whether the timer is enabled/active, the last/next
trigger, and whether a sync is running or failed:

```sh
python3 hermes.py sync-status
python3 hermes.py sync --publish
```

The one-minute timer performs Git fast-forward, profile/source reconciliation,
and stable publication on `main`. A successful `sync --publish` command reports
its workspace, runtime projection and publication result as JSON. A conflict,
missing bridge, disabled timer or failed service must be surfaced and resolved;
do not assume a local edit reached the live profile. If the timer is disabled,
the operator can restore the existing user unit with
`systemctl --user enable --now hermes-workspace-sync.timer` and recheck
`sync-status`. If a service run failed, inspect its user journal with
`journalctl --user -u hermes-workspace-sync.service -n 50 --no-pager`. Git
commits and pushes stay in `~/.hermes` as its owner.

## Source ownership and delivery evidence

Choose the source owner before the first write. Reusable runtime code, patches,
skills and operating guidance belong in the public distribution. Fix and test
there first; private consumers adopt its release. An instance-only override does
not complete a shared fix. Instance policies, personas and profile customizations
belong in the private repository. Keep personal repository names and endpoints
out of shared instructions.

Native operations such as credentials, profile lifecycle and dependency
installation still use their owning CLI/API with native approvals. For versionable
files, the installation's source-authoring policy takes precedence over generic
native-authoring examples in this guide; without a repository-first policy, use
native `skill_manage` for personal skills. Reconcile sanitized native projections
afterwards, without putting secrets in Git.

For each requested outcome report its target, exact evidence and pending work:

- **Repository publication:** when requested, record the repository, commit and
  paths; read the exact intended files from the agreed delivery ref. A PR-only
  request ends at its verified PR head; a merged-publication request requires the
  target branch. A timer, local commit or successful sync is not remote readback.
- **Runtime delivery:** when requested, read the exact profile's installed files
  and compare contents or hashes with the intended artifact. Check enabled or
  disabled state separately. Registration/doctor does not prove hook execution
  or that an existing conversation reloaded; report activation timing honestly.
- **Release adoption:** when required, verify the distribution release, private
  adoption and deployed runtime separately. A merged public PR is not deployment.

Do not impose repository publication on a native-only request that has no such
requirement. Preserve unrelated work and report partial results as partial; do
not let a successful check stand in for another requested outcome.

## Updates and conflicts

Common fixes are tested and released by the public project. The private update
workflow proposes a PR with the version lock, digest pins and reconciled skills.
The private deployment controller owns activation. The public workflow has no
credential to deploy anyone's Hermes.

Three-way skill reconciliation compares the old template, new template and
personal edit. Compatible changes survive; conflicting candidates stop and
notify the owner on GitHub. Do not place conflict markers in active skills or
ask the active agent to choose between contradictory instructions. Identical
managed copies are reconstructed from the image; personal overrides stay in Git.

Do not use `hermes update` or `git pull` in the read-only pod core. Do not bypass
`waiting-for-runtime`: the controller waits for the image matching the pinned
source before applying dependent personalizations. A local edit, commit or
successful image build does not prove production activation.

Read [structure and operations](references/structure.md) for state paths,
compare-and-swap editing, MCP updates and backup boundaries. Use reusable scripts
for changed behavior and pending failures. Do not repeat model/API calls for
unchanged capabilities or run a complete backup for ordinary plugin management.
