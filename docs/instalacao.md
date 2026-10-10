# Installation

Generate an independent private repository using scripts/create_instance.py.
The generated instance.json must be customized: terminal hostname, SSH user,
timezone, model and proxy URL belong to that installation. Configure state and
workspace storage separately from the replaceable terminal OS.

The generated Kubernetes overlay uses the versioned public base and digest pins.
Create hermes-ssh from a dedicated SSH private key plus pinned known_hosts,
and hermes-runtime-env with the private .env. No credentials are generated or
committed. Configure dashboard authentication before exposing its Service.
The default NetworkPolicy denies ingress and egress except namespace-local
traffic and DNS. Add explicit allowed destinations for SSH and model/API calls.
The example does not deploy a shared inference proxy or guess your network.

A private repository can live at ~/.hermes on the terminal. Set up the existing
pod-to-terminal operations tunnel, then use `python3 hermes.py runtime info`.
For synchronization, install the generated systemd user service and timer. The
service runs `python3 ~/.hermes/hermes.py sync --publish`; enable lingering if the
host should run it without an interactive session. Git requires credentials
scoped to this private instance repository. Do not give the terminal cluster-admin.

Tailscale is optional when suitable cluster connectivity already exists. An
independent user's installation can use a dedicated tailnet and its own proxy.
Keep allowlisted infrastructure destinations explicit and enforce NetworkPolicy
with a CNI that supports it. Executor-specific privileges are not inferred from
an arbitrary MCP package name.

The reusable base provides Hermes without mandatory MCP executors. Add `distribution/kubernetes/executors` from the same release commit to the
private overlay for API, browser and workspace executors, or select individual
executor resources. Configure
their dedicated SSH credentials, policy and durable dependency stores first.
See mcp/README.md. Backups are a separately configured operation, not enabled by
merely installing this distribution. Do not activate a new gateway against an
existing live gateway's state.
