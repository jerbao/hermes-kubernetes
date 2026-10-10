# Private Hermes instance

Copy configuration into an ignored installer `.env`, then deploy:

```sh
python3 hermes.py setup init
# Fill .env: model, terminal, namespace and optional Proxmox/Drive settings.
python3 hermes.py setup plan
python3 hermes.py setup configure
# Optional: create this private GitHub repository and configure its deploy key.
python3 hermes.py setup install
python3 hermes.py setup connect
```

Shared code and images are pinned by `platform.lock.json`. Setup prepares its
own ignored Python environment when needed; native profile environments remain
independent and private. The generated files are non-secret desired state;
setup commits them without committing `.env`, keys, SQLite, OAuth or caches.
Use `setup drive-auth` for a one-time encrypted Google Drive authorization.

The complete settings reference and infrastructure prerequisites are in the
public distribution's [installation guide](https://github.com/jerbao/hermes-kubernetes/blob/main/docs/installation/environment.md).

Use `python3 hermes.py runtime` or Desktop/API to manage the live runtime.
`python3 hermes.py source` prepares the exact official core and distribution
patches for inspection. Accept common updates through reviewed release PRs;
preserve private skills/personas/plugins under `customizations/<profile>`.
