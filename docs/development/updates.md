# Updates

There are two review boundaries:

1. Public upstream review: the scheduled workflow finds a stable official Hermes
   release, replays patches, tests the candidate and opens a review PR. Conflicts
   create a GitHub issue; the public main branch stays unchanged.
2. Private adoption: the scheduled instance workflow reads the latest stable
   distribution manifest, verifies exact source and immutable image digests,
   reconciles private skills, and opens one update PR. A conflict opens an issue
   with the run link and leaves deployed state untouched.

Release publication runs only on a `v*` tag or manual dispatch. It
builds/tests Hermes and both MCP images, then attaches distribution.json to the
GitHub release. The manifest pins all image digests and the exact source commit.
No release is emitted if a component fails or an image cannot be pulled
anonymously. Check visibility on first publication: if a new GHCR package remains
private, set it to Public and rerun only the failed publication job. No image
rebuild is needed for that setting. Protect release tags in the repository's
rulesets according to the maintainer's publication policy.

Private updates are opt-in merges. Their GitOps overlay pins the new images;
ArgoCD or another deployment controller applies the reviewed private manifests.
Updates must preserve profile .env separation, installed plugin state and SQLite
volumes. An update is not a backup or a migration of databases into Git.

Managed skills compare previous distribution, candidate distribution and private
projection. Compatible edits are retained; conflicts block the entire candidate.
Markers are never written to active skills. Runtime baselines remain private in
state/versioning/managed-skills. Never solve conflicts during a live agent request
by asking the LLM to choose between two conflicting instructions.
