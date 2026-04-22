# Backends

Seneschal talks to the LLM through an `ApiBackend` defined in
[`backend.py`](../backend.py). The public repo ships exactly one backend —
this one — which wraps the official Anthropic Messages API.

## Required configuration

| Env var | Required | Default | Notes |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | yes | — | Seneschal refuses to start a review if unset. Get one at <https://console.anthropic.com/>. |
| `ANTHROPIC_BASE_URL` | no | `https://api.anthropic.com` | Override to point at any Anthropic-compatible endpoint. |
| `SENESCHAL_MODEL` | no | `claude-sonnet-4-5-20250929` | Override the model id sent on every review. |

Set these in the systemd unit (`systemd/seneschal.service`) as
`Environment=` lines. See the `Install` section of the top-level
[`README.md`](../README.md) for the full deployment walkthrough.

## Why there is only one backend

Earlier versions of Seneschal shelled out to the `claude` CLI. That path
was removed in favor of the API because:

1. The CLI path coupled Seneschal's public framing to a consumer
   subscription, which is not an appropriate distribution shape for an
   open-source tool.
2. The API backend enables Anthropic **prompt caching** on the system
   prompt (persona + context), which gives repeat reviews on the same
   repo a meaningful cost discount. The CLI path could not.
3. Everything Seneschal does in this public repo — diff review, persona
   fan-out — is plain text round-trips with no tool use. The API handles
   that shape directly.

If you want to re-create a CLI backend for your own personal deployment,
implement the `Backend` protocol (`backend.py`) in a private module and
select it via a factory override. This repo does not ship that path and
does not document it further.

## Prompt caching

`ApiBackend` wraps the system prompt in a single text block with
`cache_control={"type": "ephemeral"}`. No configuration required — if
the SDK and server support it, repeat reviews of the same repo
(same persona + same ADR context) pay a fraction of the input-token
cost on cache hits. You can monitor cache hit rate via the response
headers in the Anthropic SDK if you want to tune it further.

## Pointing at a different Anthropic-compatible endpoint

Some third-party proxies implement the Anthropic wire format. If you
use one, set `ANTHROPIC_BASE_URL` to its endpoint:

```ini
Environment=ANTHROPIC_API_KEY=<provider-key>
Environment=ANTHROPIC_BASE_URL=https://example-proxy/anthropic/v1
```

The `ApiBackend` does nothing provider-specific — it relies on the
`anthropic.Anthropic(base_url=...)` shim built into the SDK. If the
provider serves different underlying models than real Claude, the
review *format* is identical but the review *quality* and reasoning
will reflect whatever the provider actually runs behind the wire.

## Troubleshooting

- **`ANTHROPIC_API_KEY is required to construct ApiBackend`**: the
  systemd unit does not have the key set, or the service was restarted
  without picking up a new `EnvironmentFile`. `systemctl cat
  seneschal.service` to check, then `daemon-reload && restart`.
- **Reviews come back empty**: check `journalctl -u seneschal -n 50` —
  the backend logs the exception from `anthropic.messages.create` on
  failure. Common causes: expired API key, rate limits, or a
  mis-typed `ANTHROPIC_BASE_URL` pointing at a non-Anthropic endpoint.
- **Model ID not recognized at `ANTHROPIC_BASE_URL`**: whatever proxy
  you set may only map a subset of model strings to its underlying
  models. Pick an id it recognizes (check the proxy's docs).
