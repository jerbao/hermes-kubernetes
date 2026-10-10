# Development

Use make checkout to prepare the pinned upstream and patches. Read native
hermes-agent/AGENTS.md before editing its source. Export changes using
scripts/export_patch.py and the focused groups in patches/series.json.
make test-hermes selects patch tests; the full upstream suite is manually
available through make test-hermes-full. CI may run all distribution checks.

Development homes and Python environments stay in .local. A core source checkout
is inspectable and editable on the external terminal, but never replaces the
immutable running image without CI and an explicit private deployment update.
Do not reset, clean or overwrite a native home to force an update.
