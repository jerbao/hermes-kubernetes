# Repository structure

| Directory | Purpose |
|---|---|
| runtime/ | Shared pod bootstrap, operations bridge, state projection and backups |
| patches/ | Focused changes applied to the pinned official Hermes source |
| skills/ | Distribution self-management instructions; private edits stay private |
| scripts/ | Build, validation, release publication and instance generation |
| vm/ | External terminal tooling, source checkout and native state synchronization |
| mcp/ | Separate package runners and images; no dependency installation into Hermes |
| distribution/ | Private-instance commands and release/update protocol |
| examples/instance/ | Minimal private consumer; imports shared code by pinned commit |
| config/ | Generic first-boot preferences, pinned tools and catalog routing |
| tests/ | Isolated fixtures and automated checks; no model calls |
| hermes-agent/ | Ignored official source checkout plus patches, reproducible from lock |
| .local/, .build/ | Ignored diagnostics, source caches and build output |

Private repositories contain instance.json, platform.lock.json, customizations/
and their deployment configuration. Shared source is cached under
.local/platform/<commit>/ and remains inspectable without being copied into every
private repository's Git history. Runtime .env files are independent per profile.

The container integration directory /opt/hermes-personal is a stable compatibility
path. It does not refer to the private repository or a hardcoded user.
