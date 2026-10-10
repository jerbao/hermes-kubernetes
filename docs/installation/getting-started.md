# Installation

Use the [environment-driven installer](environment.md): generate a private
instance, fill its `.env`, review `python3 hermes.py setup plan`, and run
`python3 hermes.py setup install`. It can connect an existing SSH terminal or
provision a dedicated Ubuntu VM/LXC on Proxmox using OpenTofu and Ansible.
The environment guide explains credentials, durable storage, NetworkPolicy,
Google Drive authorization and optional private Git synchronization.

The runtime keeps native Desktop/API management and independent profile `.env`
files. The terminal receives the private Git project and pinned core source for
inspection, without cluster-admin credentials. Persistent state/workspace lives
outside the replaceable OS; databases and OAuth files never enter Git.

Tailscale is optional with existing cluster connectivity. A dedicated tailnet,
model proxy, Kubernetes cluster and storage services remain infrastructure
prerequisites, configured independently from this distribution.

The base does not require MCP executors. Add the versioned executor resources
when needed, with their dedicated SSH identities and explicit workspace/browser
permissions. See [MCP operations](../operations/mcp.md).
