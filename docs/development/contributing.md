# Development

Use `make checkout` to prepare the pinned upstream and focused patches. Read
`hermes-agent/AGENTS.md` before editing core source. Export upstream changes with
`python3 -m hermes_kubernetes.source.patches` and `patches/series.json`.
`make test-hermes` selects affected patch tests; `make test-hermes-full` explicitly
requests the full upstream suite. CI may run all distribution checks.

Keep reusable behavior in `hermes_kubernetes/<domain>/`; use qualified imports
rather than modifying import paths between scripts. Modules may expose a small
CLI under a main guard. Shell files in `scripts/` only coordinate external tools.
Do not split functions into arbitrary files: create a module for a distinct
responsibility or dependency boundary. Tests mirror these domains; end-to-end
scripts live under `tests/integration/`.

```sh
ruff format hermes_kubernetes tests examples/instance/hermes.py
ruff check hermes_kubernetes tests examples/instance/hermes.py
make validate test
```

The formatter/linter version is pinned in `requirements-dev.txt`. `make validate`
also checks package imports, English documentation and both isolated image
contexts. Build inputs come from Git's inventory, never an entire native home.

Validate a published release independently of GitHub authentication:

```sh
python3 -m tests.integration.released_instance --release v0.1.0 --core-source
```

This verifies public image manifests, creates two disposable private consumers,
fetches their pinned dependency anonymously, renders their Kubernetes overlays
and checks an independent patched core checkout. It never activates a gateway,
changes a live installation or calls a model. Omit `--core-source` when only the
release/bootstrap protocol changed and core checkout behavior is unchanged.

Development homes and environments stay in `.local/`. Terminal core checkouts
remain inspectable/editable, but production changes require public CI, a release
and a reviewed private deployment update. A pinned dependency cache is immutable;
submit shared changes from a separate development clone. Do not reset, clean or
overwrite native home state to force an update.
