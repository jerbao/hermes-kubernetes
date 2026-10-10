# Hermes Kubernetes

This is the reusable public distribution. Personal configurations, profiles,
credentials, network addresses, migration records and private history belong
in independent private instance repositories.

- Preserve native Hermes capabilities and independent .env files per profile.
- Pin the official upstream commit and dependencies. Export core changes as
  focused patches through patches/series.json; never vendor hermes-agent/.
- Container paths under /opt/hermes-personal are a compatibility interface.
- Validate changes using reusable scripts. Do not call models or repeat checks
  on unchanged features. CI may run the complete test suite.
- Run make validate test for distribution changes; selected native tests cover
  patches. Image workflows must never deploy to a personal cluster.
- Publish immutable digests and release manifests. Private consumers update by
  PR, preserving customizations and blocking conflicts with GitHub alerts.
- Never introduce a person's namespaces, endpoints, profiles or credentials as
  defaults. Review publication with scripts/check_publication.py.
