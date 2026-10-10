# Optional MCP executors

Add this kustomization (or only the executor YAML files you need) to the private
instance overlay using the same release commit as its main base. The parent's
NetworkPolicy permits namespace-local SSH. External APIs and package registries
still require explicit egress policies.

Create hermes-mcp-server-ssh with host_key and authorized_keys. The Hermes
client requires a separate hermes-mcp-ssh Secret with id_ed25519 and known_hosts,
mounted at /opt/hermes-mcp-ssh. Then configure instance.json's MCP executor policy.
See the runtime configuration schema and tests/test_mcp_routing.py for examples.
Keys and SSH host aliases must match the Service targets; never disable host-key
verification. Do not use another person's keys.

The workspace executor requires an existing hermes-workspace PVC containing only
authorized projects. It mounts /workspace; use matching configured roots and
terminal paths, or patch the mount path explicitly. It does not mount Hermes state.
The browser executor needs a browser endpoint configured in its MCP arguments;
if the browser runs on the terminal, configure a dedicated SSH tunnel or an
explicitly allowed endpoint. The generic image does not bundle a browser or
silently reuse the terminal's administrative SSH key.

Persist each executor's data independently. Backup policies and additional
workspace storage are private-instance choices, not fixed storage classes here.
