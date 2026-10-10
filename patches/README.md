# Focused upstream patches

Builds download the exact `upstream.lock.json` revision, apply patches in order
and verify every SHA-256. Prefer native skills, plugins and the distribution
package for changes that do not require editing Hermes core.

| Patch | Responsibility | Verification |
|---|---|---|
| `0001-runtime-almalinux.patch` | Upstream Dockerfile: AlmaLinux x86-64-v2, libraries and PM dependencies | Image build and bootstrap |
| `0002-ssh-terminal.patch` | SSH batching, synchronization and profile/home isolation | Selected native SSH/synchronization tests |
| `0003-anthropic-usage.patch` | Auxiliary Anthropic token and cache accounting | Selected native Anthropic/MoA tests |
| `0004-mcp-stdio-executor.patch` | External stdio hook and GUI catalog transport metadata | Native executor and catalog RPC tests |
| `0005-managed-skills.patch` | Official self-management instructions for the separated runtime | Locked patch replay and instruction comparison |

`series.json` assigns upstream files and tests to each group. The exporter rejects
unclassified changes and duplicate ownership. New fixes require an appropriate
group rather than a catch-all patch. Groups without upstream differences produce
no patch.

Edit the ignored `hermes-agent/` checkout for core changes, run affected native
tests, then export:

```sh
python3 -m hermes_kubernetes.source.patches --source PATH_TO_CHECKOUT
```

Stage new files using `git -C hermes-agent add PATH` before export. The exporter
compares with the locked base and generates separate patches. `--test PATH`
checks test ownership. Review patches and lock before committing. The lock records
order, hashes and per-patch tests; the global native test list is their union.

`0001` modifies the **upstream** Dockerfile downloaded with Hermes. Our own
`containers/hermes.Dockerfile` wraps that base and is edited directly.

`0005` changes Markdown instructions. Reconciliation in
`hermes_kubernetes/state/skills.py` preserves compatible profile edits during
release preparation and runtime capture/bootstrap. Every profile also receives
`hermes-self-management`. Conflicts block writes before changing active skills.

For explicitly requested one-time publication to an existing installation:

```sh
python3 -m hermes_kubernetes.source.skill_patches \
  --kubeconfig PATH --publish --base-revision PREVIOUS_COMMIT
```

This reconstructs old/new instructions, merges three versions, refuses ambiguous
results and writes through compare-and-swap with hash verification. Merged edits
return to Git. Without `--publish`, it only checks. It does not install plugins,
restart services or call models. New sessions load updated instructions.
