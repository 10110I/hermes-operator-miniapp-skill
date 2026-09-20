# Hermes Operator Mini App Skill

Creation date: 2026-09-20
Status: working initial public release

This repository packages a Hermes skill plus helper scripts for a read-only Telegram Mini App that shows:

- connected service/access checks;
- provider-neutral subscription/quota trackers;
- active Hermes cron jobs with human-readable schedules and local timezone display.

It is based on a real Hermes operator panel, but the subscription tracking was redesigned so users are not locked to one provider such as `openai-codex`.

## Install the skill

```bash
hermes skills install https://raw.githubusercontent.com/10110I/hermes-operator-miniapp-skill/main/SKILL.md
```

Clone helper scripts:

```bash
git clone https://github.com/10110I/hermes-operator-miniapp-skill.git ~/.hermes/operator-miniapp-skill
```

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
SKILL.md                                  # installable Hermes skill
scripts/operator_miniapp_status.py        # standalone Mini App server + collectors
templates/config.yaml                     # user config template
templates/hermes-operator-miniapp.service # user systemd service template
templates/Caddyfile                       # narrow HTTPS reverse proxy template
references/hosting.md                     # recommended hosting topologies
references/service-discovery.md           # discovery candidates and display rules
references/subscription-tracker-model.md  # provider-neutral tracker design
tests/                                    # unit tests + fixtures
```

## Known limitations

- The helper server is intentionally small and dependency-light. For production, run it behind a real HTTPS proxy and systemd/launchd.
- Built-in probes are minimal. Provider-specific quota collection should usually be a small `command_json` script maintained by the user/provider.
- The installer does not create DNS/HTTPS automatically because hosting choices differ.
