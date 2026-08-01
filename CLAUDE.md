# CLAUDE.md

> **Status: internals not yet audited.** The ecosystem contract and repo layout below are verified. `addon.py` (~98 KB) and `tg_admin_workflow.py` (~44 KB) have **not** been read line by line — don't treat the behaviour notes as exhaustive.

## What this is

A Stremio addon that streams video/audio/subtitle files directly out of private Telegram storage channels, acting as an on-the-fly HTTP streaming proxy with full Range support (so seeking works without downloading first). Python.

Within the wider ecosystem (see `asaf2298/UserManager`'s CLAUDE.md) this is **"the Telegram Addon" — a dynamic, high-speed HTTP proxy for streaming large media files directly from private channels.**

## ⚠️ The README is inherited and badly out of date

`README.md` came from the upstream project this repo started from and has barely changed (37 KB vs upstream's 38 KB). It documents **none** of what has been built here — zero mentions of TorBox, the admin/bot workflow, TMDB, Supabase, the metadata store, the host-busy lease, or the test suite.

**Do not use the README to understand this repo.** Use this file, then read the source. Rewriting the README is worthwhile work in its own right.

## Relationship to upstream

Started from `SunilRoy-dev/stremio-telegram-debrid`, but has diverged substantially. GitHub does not record it as a fork, and the divergence is one-directional:

```
files:  this repo 44   |   upstream 23   |   shared 23
only in this repo: 21  |   only in upstream: 0
```

Roughly **100 KB of original code** lives here that upstream does not have, and upstream has nothing this repo lacks. Treat upstream as a historical starting point, **not** a live dependency to stay merge-compatible with.

If you do want an upstream fix, cherry-pick it into the five shared core files only — `addon.py`, `tg_client.py`, `utils.py`, `zip_helper.py`, `search_utils.py` — and expect conflicts: `addon.py` is already +16 KB over upstream and `config.py` +5 KB.

Licence is upstream's **MIT-NC (non-commercial)**; GitHub reports `NOASSERTION` because the modified MIT text isn't auto-detectable. Keep deployments personal/non-commercial.

## Layout

**Local additions (not in upstream):**

| File | Role |
|---|---|
| `tg_admin_workflow.py` (44 KB) | Admin / bot workflow — the largest single addition |
| `torbox_client.py` (9 KB) | TorBox integration; gated by `Config.torbox_enabled` (`TORBOX_API_KEY`) |
| `metadata_store.py` (10 KB) | Metadata persistence |
| `tmdb_client.py` (6 KB) | TMDB lookups |
| `host_busy.py` (6 KB) | Host-busy lease shared with the media-intelligence worker |
| `tests/` (8 files, ~53 KB) | pytest suite — `conftest.py`, admin workflow, addon mapping, metadata store, tmdb, torbox, host_busy, tg client concurrency |
| `deployment/vps/` | Caddy + cloudflared docker-compose stacks |
| `deployment/supabase/` | `personal_host_busy.sql`, `torbox_columns.sql` |

**Shared with upstream (modified):** `addon.py` (entry point), `tg_client.py`, `config.py`, `utils.py`, `zip_helper.py`, `search_utils.py`.

## Commands

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest                      # config in pytest.ini
```

Unlike the sibling Node repos in this ecosystem, **this one does have a real test suite** — use it. Deployment: Docker (`docker-compose.yml`; the VPS variants add Caddy or cloudflared).

## How the aggregator consumes this addon

`asaf2298/UserManager`'s `lib/providerCapabilities.js`:

```js
family: 'personal_telegram',
matchers: [{ kind: 'exactHost', value: 'advantage-shot-petition-crucial.trycloudflare.com' }],
idSchemes: ['tt', 'tgfile', 'personal'],
transports: [TRANSPORT.DIRECT_OWNER],
markerParser: noMarkerParser,      // cache status can never be claimed via text
vipRule: hostAuthoritativeVip,     // VIP provider
integrityPrior: { mu0: 0.82, kappa0: 8 },
```

Three consequences worth knowing:

- **The hostname is matched exactly, and it is a `trycloudflare.com` quick tunnel** — those URLs rotate whenever the tunnel restarts. When it changes, the aggregator silently demotes this addon to `generic_known`: VIP status is lost and the integrity prior drops 0.82 → 0.60, with no error anywhere. **Update `providerCapabilities.js` in `UserManager` whenever the tunnel URL changes.** The `deployment/vps/docker-compose.caddy.yml` stack with a stable hostname removes this failure mode entirely — prefer it.
- **At most ~2 VIP rows reach the user** (`caps.vip = 2`, one row per VIP provider, surplus VIP rows hard-dropped even on the 100-row Kodi profile). Returning many rows per request is wasted work.
- **Cache claims cannot be made in text.** `markerParser: noMarkerParser` — "cached"/"⚡" in a title has no effect on the aggregator's availability score. TorBox-backed availability has to be expressed through the stream shape, not the title.

## Coordination with the shared worker

`host_busy.py` + `deployment/supabase/personal_host_busy.sql` implement the `personal_host_busy` lease in Supabase project `gihkgnadwxpopvspeskb`. `UserManager`'s media-intelligence worker yields to Telegram while that lease is held, and its TorBox playback deliberately never sets busy (see `worker/media-intelligence/README.md` there). **Changing the lease contract affects both repos** — check that worker before touching it.

## Before working here

Read `asaf2298/UserManager`'s CLAUDE.md for the ecosystem contract, and the audit epic asaf2298/UserManager#62 for known cross-repo issues.
