# Private Hermes instance

Shared runtime code is a pinned dependency, not a personal copy to maintain.
Personal skills, plugins, personas and sanitized configuration are versioned in
customizations/<profile>. Every profile has an independent private .env.

Manage the live pod through `python3 hermes.py runtime`, or the native Desktop/API.
Never install production plugins in the terminal's development Python. Use
`python3 hermes.py source` to inspect the pinned core and submit common code
changes to the public distribution. Adopt releases through reviewed lock updates.
Never commit SQLite, conversations, credentials, .env or dependency caches.
Use reusable scripts for checks, without repeated model/API calls.
