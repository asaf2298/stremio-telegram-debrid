# CLAUDE.md

> **Status: not yet audited.** This file records only what has been verified from the repository contents and from how the rest of the ecosystem consumes this addon. The internals of `addon.py` (~2 300 lines) have **not** been reviewed. Do not treat the architecture notes below as complete.

## What this is

A Stremio addon that streams video/audio/subtitle files directly out of private Telegram storage channels, acting as an on-the-fly HTTP streaming proxy with full HTTP Range support (so seeking works without downloading first). Python, no database.

Within the wider ecosystem (see `asaf2298/UserManager`'s CLAUDE.md) this is **"the Telegram Addon" — a dynamic, high-speed HTTP proxy for streaming large media files directly from private channels.**

## Provenance — important

This code originates from **`SunilRoy-dev/stremio-telegram-debrid`** (the README still carries that project's badges and banner). GitHub does not record this repo as a fork, so it is a *detached* copy: upstream fixes will **not** merge automatically and have to be applied by hand.

The upstream licence is **MIT-NC (non-commercial)** — GitHub reports `NOASSERTION` because the modified MIT text is not auto-detectable. Keep deployments personal/non-commercial.

Before making a large change here, check whether upstream already fixed it — diverging further makes future upstream merges harder.

## Layout (by size, largest first)

| File | Role |
|---|---|
| `addon.py` (~98 KB) | Main addon + streaming proxy. The entry point. |
| `tg_admin_workflow.py` (~44 KB) | Admin/ingest workflow |
| `tg_client.py` (~21 KB) | Telegram client wrapper |
| `metadata_store.py` | Metadata persistence (no DB — file-backed) |
| `torbox_client.py` | TorBox integration |
| `tmdb_client.py`, `search_utils.py`, `zip_helper.py`, `utils.py`, `config.py` | Supporting modules |
| `host_busy.py` | Host-busy lease — coordinates with the shared media-intelligence worker |
| `tests/` | `pytest` suite (`test_admin_workflow.py`, `test_addon_mapping.py`, `test_metadata_store.py`, `test_tmdb_client.py`) |

Deployment: Docker (`sdk: docker` header in README — Hugging Face Spaces).

## How the aggregator consumes this addon

`asaf2298/UserManager`'s `lib/providerCapabilities.js` registers it as:

```js
family: 'personal_telegram',
matchers: [{ kind: 'exactHost', value: 'advantage-shot-petition-crucial.trycloudflare.com' }],
idSchemes: ['tt', 'tgfile', 'personal'],
transports: [TRANSPORT.DIRECT_OWNER],
markerParser: noMarkerParser,      // cache status can never be claimed via text
vipRule: hostAuthoritativeVip,     // VIP provider
integrityPrior: { mu0: 0.82, kappa0: 8 },
```

Consequences worth knowing:

- **The hostname is matched exactly**, and it is a `trycloudflare.com` quick-tunnel — those URLs change whenever the tunnel restarts. When it changes, the aggregator silently demotes this addon to `generic_known`: VIP status is lost and its integrity prior drops 0.82 → 0.60. **Update `providerCapabilities.js` in `UserManager` whenever the tunnel URL changes.** A stable named tunnel would remove this failure mode entirely.
- **At most ~2 VIP rows reach the user** (`caps.vip = 2`, one row per VIP provider, surplus VIP rows hard-dropped). Returning many rows per request is wasted work.
- **Cache claims cannot be made in text.** `markerParser: noMarkerParser` — "cached"/"⚡" in a title has no effect on the aggregator's availability score.

## Coordination with the shared worker

`host_busy.py` participates in the `personal_host_busy` lease in Supabase project `gihkgnadwxpopvspeskb`. `UserManager`'s media-intelligence worker yields to Telegram when that lease is held (see `worker/media-intelligence/README.md` there). Changing the lease contract affects both repos.

## Before working here

Read `asaf2298/UserManager`'s CLAUDE.md for the ecosystem contract, and the audit epic asaf2298/UserManager#62 for known cross-repo issues.
