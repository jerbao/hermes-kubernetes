# Subscription quotas through CLIProxyAPI

The optional quota adapter displays upstream subscription usage in the native
Hermes model picker. OpenAI/Codex reports its actual time windows, Claude reports
session and weekly windows (including model-specific windows), and Gemini CLI
reports model quota buckets and reset times. It does not infer plan limits or
substitute the proxy's local request/token counters for subscription quotas.
API-key accounts without a compatible subscription endpoint remain unavailable.

Use the official CLIProxyAPI image; no fork or OAuth credential duplication is
required. The adapter uses the v8.0.13 management `auth-files` and `api-call`
interfaces. OAuth refresh remains owned by the proxy. The `quota/fetch` interface
requires a registered quota plugin or declarative probe and is not assumed to
support these accounts automatically.

## Deploy beside an existing proxy

Add `deploy/optional/proxy-quota` as a Kustomize component to the proxy overlay,
using the distribution's immutable Hermes image for `hermes-personal:initial`.
The component expects a deployment/container named `cli-proxy-api`, a `config`
volume containing `config.yaml`, and the pod label
`app.kubernetes.io/name: cli-proxy-api`. It preserves inference, routing and the
existing OAuth volume. Mount a Secret named `cli-proxy-quota` containing a random
management `key` of at least 32 characters. Use your existing secret manager or
SealedSecrets; never commit its plaintext.

An init container generates the proxy configuration in memory, enabling management
with `allow-remote: false`. Do not set `MANAGEMENT_PASSWORD`: in v8.0.13 that variable
also enables remote management. Only the adjacent quota container receives the key.
The Hermes pod never receives it or OAuth tokens. No additional image family is
needed; the adapter reuses the pinned distribution Python image.

Allow ingress to TCP 8318 **only from the intended Hermes pods** using a
NetworkPolicy, and allow matching Hermes egress. Do not expose the quota Service
through an ingress or Tailscale: its read-only endpoint intentionally relies on
this cluster access boundary. It contains opaque account IDs, not account emails.
The only functional route is `GET /v1/usage`; it accepts no upstream URL, account,
mutation or credential arguments. `/healthz` reports process health separately
from upstream availability.

Add this optional block to Hermes config.yaml or its managed policy. Provider
slugs are instance-defined; map only the desired custom routes:

```yaml
proxy_usage:
  url: http://cli-proxy-quota.proxy.svc.cluster.local:8318
  providers:
    my-openai: codex
    my-google: gemini
    my-anthropic: claude
```

Each account has its own usage windows. Opening the picker starts a background
read and remains nonblocking; reopen after its first refresh. The adapter queries
the upstream endpoints every two minutes, including while Hermes is idle. No
model requests are made. Readings older than five minutes become unknown; failed
or empty responses never become a misleading zero. Reset times and window labels
come from provider data. Telemetry never changes model routing or benches accounts.
Gemini's reported buckets may omit other enforced limits; a displayed remaining
quota does not guarantee that a generation request will be accepted.

## Verification

`python -m unittest tests.quota.test_usage` tests provider normalization, real HTTP
route restrictions, stale/error handling, secret isolation and picker integration
with mocked provider responses. Native bridge coverage is listed in
`patches/series.json`. Use a deployment's automated acceptance script to inspect
the broker and the native picker without calling models. Native clients and
provider APIs can change independently; keep unavailable states visible rather
than synthesizing usage when an endpoint rejects the query.
