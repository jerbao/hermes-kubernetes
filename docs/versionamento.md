# State and versioning

Shared source belongs to the public distribution. Per-profile configuration,
personas, skills, plugin sources and installation locks belong to the private
instance under customizations/<profile>. Native changes are reconciled with Git
and a private baseline; concurrent edits are preserved as explicit conflicts.

SQLite/WAL/SHM, OAuth sessions, .env, credentials and dependency directories are
never committed. Credential references in sanitized projections are resolved
from a private PVC vault. Git and images do not replace durable-state backups.
A plugin installation uses the native package manager on the pod through the
interface or operations bridge, and does not require rebuilding the image.
