# Private Hermes instance

This repository owns this installation's configuration and personalizations.
Shared runtime source is pinned by platform.lock.json and cached in .local.

- Customize instance.json and the Kubernetes overlay.
- Configure private SSH/runtime Secrets and dashboard authentication.
- Add explicit NetworkPolicy egress destinations before activation.
- Use `python3 hermes.py runtime info` to inspect the active pod.
- Use `python3 hermes.py sync --publish` to synchronize native personalizations.
- Use `python3 hermes.py source` to inspect/debug the pinned patched core.
- Use `python3 hermes.py update` to prepare an update, never overwrite live state.

Keep this repository PRIVATE. Runtime secrets, state and dependency caches stay
outside Git. The generated update workflow proposes PRs; GitOps owns deployment.
Enable Issues and allow Actions to create pull requests in this repository's
settings. Set `UPSTREAM_ALERT_USERS` to maintainer usernames for explicit alerts,
especially when the repository owner is an organization.
