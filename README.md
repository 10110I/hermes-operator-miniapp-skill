# Hermes Operator Mini App Skill

Connected-user cards also show available reset credits and separate 5-hour/weekly reset timestamps in the configured timezone. Snapshot producers may supply `codex.reset_bank` (non-negative integer), `codex.five_hour_reset_at` and `codex.weekly_reset_at` (timezone-aware ISO timestamps). Missing or invalid metadata is shown as «Нет данных», not zero. Existing snapshot freshness warnings apply to reset data too. These are read-only fields; no reset is consumed.


Creation date: 2026-09-20
Status: working initial public release

This repository packages a Hermes skill plus helper scripts for a read-only Telegram Mini App that shows:

- connected service/access checks;
- provider-neutral subscription/quota trackers;
- active Hermes cron jobs with human-readable schedules and local timezone display;
- connected users' read-only server and provider snapshots, independently collected.

It is based on a real Hermes operator panel, but the subscription tracking was redesigned so users are not locked to one provider such as `openai-codex`.

## Install paths

There are two supported public install paths today:

1. **Managed bootstrap skill** — small, scanner-friendly guide with no runtime scripts:

```bash
hermes skills install https://raw.githubusercontent.com/10110I/hermes-operator-miniapp-skill/main/bootstrap/SKILL.md
```

2. **Audited manual fallback** — full runtime package with hash verification:

```bash
git clone https://github.com/10110I/hermes-operator-miniapp-skill.git ~/.hermes/operator-miniapp-skill
cd ~/.hermes/operator-miniapp-skill
git checkout <audited-tag-or-commit>
python3 packaging/install_manual.py --source . --yes
```

Why two paths: the full package intentionally contains local probes and a loopback Mini App backend, so community-source scanning may block a direct managed install until the package is published through a trusted Hermes skill source. See [`packaging/INSTALL.md`](packaging/INSTALL.md).

## Quick setup

```bash
python3 -m pip install --user PyYAML
mkdir -p ~/.hermes/operator-miniapp
cp ~/.hermes/operator-miniapp-skill/templates/config.yaml ~/.hermes/operator-miniapp/config.yaml
python3 ~/.hermes/operator-miniapp-skill/scripts/operator_miniapp_status.py discover-services --pretty
python3 ~/.hermes/operator-miniapp-skill/scripts/operator_miniapp_status.py --config ~/.hermes/operator-miniapp/config.yaml init-config
python3 ~/.hermes/operator-miniapp-skill/scripts/operator_miniapp_status.py --config ~/.hermes/operator-miniapp/config.yaml collect --pretty
python3 ~/.hermes/operator-miniapp-skill/scripts/operator_miniapp_status.py --config ~/.hermes/operator-miniapp/config.yaml serve --host 127.0.0.1 --port 9120
```

Local URL:

```text
http://127.0.0.1:9120/miniapp
```

## Hosting model

Default hosting is **self-hosted on the same machine that runs Hermes**:

```text
Telegram → HTTPS reverse proxy → 127.0.0.1:9120 Mini App server → local Hermes state/probes
```

Why: the API must validate Telegram `initData` server-side and read local Hermes cron/provider probe data. Static hosting alone is not enough.

Recommended production setup:

1. run `operator_miniapp_status.py` as a user service bound to `127.0.0.1:9120`;
2. expose only `/miniapp`, `/api/status`, and `/health` through Caddy/nginx/Cloudflare Tunnel/Tailscale Funnel;
3. register the final HTTPS `/miniapp` URL as the Telegram WebApp menu/button.

Templates:

- [`templates/hermes-operator-miniapp.service`](templates/hermes-operator-miniapp.service)
- [`templates/Caddyfile`](templates/Caddyfile)
- [`references/hosting.md`](references/hosting.md)

## Service display design

Services are **explicit probes**, not an automatic dump of every credential in `~/.hermes`. This keeps the Mini App useful and avoids leaking noisy internals.

Run discovery first to get candidate config snippets:

```bash
python3 ~/.hermes/operator-miniapp-skill/scripts/operator_miniapp_status.py discover-services --pretty
```

Discovery inspects safe metadata only: installed CLIs (`gh`, `tailscale`), OAuth token scopes, and Hermes gateway platform names. It never enables a candidate automatically; selected candidates must appear under `services` with `enabled: true`.

A service appears only when it is listed under `services` and `enabled: true`. Built-in service probe types:

- `github_cli` — checks `gh auth status`;
- `tailscale` — checks `tailscale status --json`;
- `oauth_token` — checks that a local OAuth token file exists and, optionally, that it contains required scopes;
- `static` — displays a configured gateway/platform candidate when no safe live probe exists;
- `command` — runs a user-provided command and matches success output;
- `file_exists` — simple local file presence check.

Use `subscription_trackers` for model/API subscription quotas, not the services block.

## Subscription tracking design

Subscriptions are configured under `subscription_trackers`. The Mini App UI renders a normalized data shape; provider-specific collection happens in adapters.

Supported tracker types:

- `command_json` — preferred; runs a local command that prints JSON;
- `command_regex` — parses existing text reports with named regex groups;
- `manual` — temporary static placeholder.

See [`references/service-discovery.md`](references/service-discovery.md), [`references/subscription-tracker-model.md`](references/subscription-tracker-model.md), and [`templates/config.yaml`](templates/config.yaml).

## Security model

### Connected users tab

The **Подключённые** tab shows one card per enabled `connected_users` entry in
configured order (up to 100). This is a read-only operations view, not a remote
agent control surface. Each entry uses `type: snapshot_json`, `snapshot_file`,
`expected_host`, and optional `stale_after_seconds` (capped at one hour). An
independent scheduled collector writes each private local snapshot atomically
with mode `0600`; opening or refreshing the Mini App only reads at most 4097
bytes per file and never triggers SSH. A disconnected collector removes or
stops refreshing its snapshot, so the tab reports unavailable or stale.
Keep real hostnames, addresses, SSH key paths and names in the private runtime
config and collector, **not in this repository**. Adding another peer requires
one isolated collector and one corresponding config entry.

The snapshot must contain only a sanitized JSON object shaped as follows:

```json
{"schema":1,"host":"example-node","gateway":"active","disk_free_pct":82,"mem_available_pct":61,"collected_at":"2026-09-28T12:00:00+00:00","codex":{"status":"not_configured","checked_at":"2026-09-28T12:00:00+00:00","five_hour_used_pct":null,"weekly_used_pct":null}}
```

Use a dedicated Unix monitor account with a forced SSH command that emits only
allowlisted states and metrics. Do **not** allow arbitrary SSH commands, use
the peer's normal Hermes agent/A2A profile, or copy their OAuth credentials.
The app validates schema and expected host, bounds numeric fields, ignores
unexpected fields, and never returns raw snapshots or errors. `not_configured`
means Codex has not been authorized; it does not fabricate usage percentages.
`ok` without both percentage windows is shown as `usage_unavailable`, not as
healthy. The tab requires the same signed Telegram WebApp request and **nonempty**
owner allowlist as the rest of `/api/status`. Refresh reloads the last collector
snapshot, not the remote server. The UI displays its timestamp.

- The HTML entrypoint can be public.
- `/api/status` validates Telegram WebApp `initData` server-side.
- After signature validation it enforces a Telegram user allowlist.
- Config stores env var names and local command paths, not token values.
- Command errors are redacted before API responses.
- Public proxy should expose only `/miniapp`, `/api/status`, and `/health`.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
python3 -m py_compile scripts/operator_miniapp_status.py
python3 scripts/operator_miniapp_status.py --config tests/fixtures/sample-config.yaml collect --pretty
```

## Repository layout

```text
SKILL.md                                  # full runtime skill; use manual fallback until trusted distribution
bootstrap/SKILL.md                        # managed bootstrap skill without runtime scripts
packaging/INSTALL.md                      # install matrix and manual fallback
packaging/install_manual.py               # stdlib manual installer with hash verification
scripts/operator_miniapp_status.py        # standalone Mini App server + collectors
templates/config.yaml                     # user config template
templates/hermes-operator-miniapp.service # user systemd service template
templates/Caddyfile                       # narrow HTTPS reverse proxy template
references/installation.md                # runtime-skill installation model
references/hosting.md                     # recommended hosting topologies
references/service-discovery.md           # discovery candidates and display rules
references/subscription-tracker-model.md  # provider-neutral tracker design
tests/                                    # unit tests + fixtures
```

## Known limitations

- The helper server is intentionally small and dependency-light. For production, run it behind a real HTTPS proxy and systemd/launchd.
- Built-in probes are minimal. Provider-specific quota collection should usually be a small `command_json` script maintained by the user/provider.
- The installer does not create DNS/HTTPS automatically because hosting choices differ.
