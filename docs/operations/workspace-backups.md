# Workspace backups

Preserve durable Hermes state and associated work. Exclude reconstructible
packages and caches by policy, not by deleting every directory named .cache:
browser sessions may contain durable data there. Backup roots and extra work
directories are instance configuration, never this distribution's defaults.
Recreating a terminal must retain its separately stored durable workspace.
