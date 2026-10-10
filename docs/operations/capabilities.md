# Capabilities interface

The Desktop/API retains native plugin and MCP management. Plugin dependencies
are installed by Hermes' package manager into persistent runtime state. Stdio
MCP packages are installed into isolated executors, not the Hermes image.

Use --hermes-executor=api, --hermes-executor=browser or
--hermes-executor=workspace when adding an unknown MCP that needs that executor.
The flag is removed before invoking the package. Known package identities may
select an executor from config/mcp-executors.json. Arbitrary package capabilities
are not inferred; explicit policy and dedicated credentials remain necessary.
HTTP MCPs use native HTTP transport. VM MCP routing requires a separately
provisioned target/key; it never silently reuses the terminal key.
