# Private instances

Create independent private repositories, rather than forks. Shared code is a
pinned dependency: platform.lock.json records the public repository, release,
exact source commit and image digests. hermes.py fetches only that commit to an
ignored cache and dispatches into the distribution's instance commands.

Each instance owns customizations/<profile>, its own instance policy, Secrets,
state PVC, MCP dependency PVCs and terminal workspace. The same public image
serves multiple people; configuration is never baked into it. The GUI continues
to manage plugins, MCPs and skills on the live pod. The terminal synchronization
controller projects native changes into the private repository, as before.

Use `python3 hermes.py runtime info` to inspect the live runtime and
`python3 hermes.py sync --publish` for native/Git reconciliation. Run
`python3 hermes.py source` for the exact patched core checkout on the terminal.
The ignored cache is replaceable; personal state is not.

`python3 hermes.py update` writes a candidate version lock and Kubernetes image
pins. The private workflow opens a PR after skill reconciliation succeeds. It
uses its own GITHUB_TOKEN, never a central token with access to everyone's repos.
To run CI on bot-created PRs the update workflow performs the candidate checks
itself. Do not assume GitHub will trigger workflows from its own token's pushes.

A release update does not automatically enable newly available capabilities or
add credentials. Unknown MCPs require explicit executor selection where needed.
The public repository has no credential to deploy any instance.
