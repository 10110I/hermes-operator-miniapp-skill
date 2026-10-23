# Hermes Operator Mini App Skill

Creation date: 2026-09-20
Status: working initial public release

This repository packages a Hermes skill plus helper scripts for an access-controlled Telegram Mini App that shows:

- connected service/access checks;
- provider-neutral subscription/quota trackers;
- active Hermes cron jobs with human-readable schedules and local timezone display;
- narrow cron-task editing: model selection, reasoning slider, and interval slider for repeating jobs;
- explicit task deletion via a separate confirmation dialog: «Вы уверены что хотите удалить». Cancel/Escape make no request; confirmed deletion is verified against Hermes storage.

Cron writes are authenticated separately with `cron.edit_allowed_users` (inheriting `telegram.allowed_users` only when the key is absent; an explicit empty list disables edits); the endpoint only exposes model/reasoning/schedule edits and exact-ID deletion, never arbitrary commands. Deletion requires a signed editor request with boolean `confirmed: true`; duplicate requests for an absent ID are verified no-ops. Deletion uses an exact-ID transaction under fail-closed local/cross-process locks and the Hermes atomic writer; it never falls back to a name. Output/notepad history is retained. An uncertain write result is not automatically retried, and raw malformed storage cannot count as an empty job list. The picker uses actual Hermes model IDs: an unavailable `gpt-6.1-luna` is never synthesized or silently replaced with `gpt-6-luna`.

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

## Server disk and private Happ VPN links

The overview includes local filesystem capacity/used/available bytes, an available-space-based utilization bar and low-space warning. OS-reserved bytes are shown separately; no cleanup runs automatically. `server.disk_path` is configured locally, never supplied by an API request.

Enable `vpn` only for existing profiles. Each profile declares an ID/label, required systemd `services`, loopback `probe_ports`, and a private `link_file`. Status means those daemons and local listeners are ready; it does **not** prove a phone connection, external reachability or a functioning tunnel.

Set explicit `vpn.link_allowed_users`; missing/empty means deny, independently of normal dashboard-viewer access. A signed `vpn_link` action additionally requires the normal viewer allowlist, and accepts only a configured profile ID. Normal status/collect output and public HTML never contain a client URI. Only the separately authorized on-demand response returns the exact sensitive import URI, with `no-store`; do not log/forward that response or open a secret-bearing dialog in report screenshots.

Keep one `vless://` line in a regular owner-controlled `0600` file outside Git; symlinks, public permissions, invalid formats and oversized files fail closed without echoing their content. The UI opens a private dialog, then copies on a second user gesture for mobile clipboard compatibility. If both clipboard methods fail, it selects the text for manual copying. Closing/Escape, identity changes, or a current status response revoking copy permission scrub the field and invalidate pending requests, even with unchanged Telegram identity; late responses cannot restore the URI or fill another operation's dialog. There is no browser persistent storage and no VPN change/restart capability.

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

Optional real-browser/CLI regression (requires an installed Hermes CLI):

```bash
uv run --with playwright playwright install chromium --only-shell
uv run --with playwright --with PyYAML python tests/browser_cron_editor.py scripts/operator_miniapp_status.py
```

This creates a disposable `HERMES_HOME`, never edits the operator's real jobs, and never runs inference. It checks mobile/desktop layout, actual CLI saves and reload, clearing model/reasoning pins, calendar-conversion cancellation, one-shot frequency protection, save failures, and viewer/account-switch denial.

The reasoning slider stores Hermes-supported levels; the inference provider may clamp unsupported levels at execution time. Selecting a catalog model does not prove the account has quota for inference.

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
