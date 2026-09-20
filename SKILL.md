---
name: hermes-operator-miniapp-status
description: Install Telegram status Mini Apps for Hermes.
version: 0.1.1
author: Konstantin, Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [hermes, telegram, mini-app, status, subscriptions]
    related_skills: []
---

# Hermes Operator Mini App Status

Build and install a narrow read-only Telegram Mini App for Hermes operators. It shows connected services, configured subscription/quota trackers, and active cron jobs without exposing provider secrets or the full Hermes dashboard.

This skill is intentionally provider-neutral: it does not assume the user has `openai-codex`. Subscription limits are configured as `subscription_trackers` during setup.

## When to Use

Use when a user wants:

- a Telegram Mini App status panel for Hermes;
- read-only visibility into connected services and cron jobs;
- subscription/quota bars for whatever providers they actually use;
- a setup flow that asks which providers/subscriptions to track.

Do not use for:

- publishing a full Hermes dashboard publicly;
- mutating provider credentials or subscription state;
- storing API keys in the Mini App config.

## Prerequisites

- Hermes installed and available as `hermes`.
- Telegram bot token available in a local env var, usually `TELEGRAM_BOT_TOKEN`.
- Telegram user IDs that are allowed to view the panel.
- Python 3.11+ and `PyYAML` for YAML config parsing.
- HTTPS public URL for Telegram WebApp launch if users need mobile access outside localhost.

Install dependency if needed after choosing the manual runtime path:

```bash
python3 -m pip install --user PyYAML
```

For public distribution, use the scanner-friendly bootstrap skill first:

```bash
hermes skills install https://raw.githubusercontent.com/10110I/hermes-operator-miniapp-skill/main/bootstrap/SKILL.md
```

Then install the full runtime package from an audited checkout:

```bash
git clone https://github.com/10110I/hermes-operator-miniapp-skill.git ~/.hermes/operator-miniapp-skill
cd ~/.hermes/operator-miniapp-skill
git checkout <audited-tag-or-commit>
python3 packaging/install_manual.py --source . --yes
```

Why: the full runtime package includes local probes and a loopback Mini App backend, so a community-source scanner may block a direct managed install until the package is distributed from a trusted Hermes skill source.

## Setup Questions

Before writing config, ask the user these questions in one short batch:

1. Which Telegram bot token env var should the server read?
2. Which Telegram user IDs are allowed?
3. Which discovered service candidates should be displayed? Run `discover-services --pretty` to inspect safe candidates from CLI tools, OAuth scopes, and Hermes gateway platforms.
4. Which service probe style is appropriate: CLI health check, OAuth token/scope check, custom command, file presence, or static configured-platform status? Do not auto-display every credential.
5. Which subscriptions should be tracked?
6. For each subscription: provider label, source type (`command_json`, `command_regex`, or `manual`), account/pool structure, quota windows, and reset text format.
7. Which display timezone should be used? Default to `Europe/Moscow` only when the user has no other preference.
8. Which HTTPS URL will be used for the Mini App, or should setup stop at localhost?
9. Where will it be hosted: same VPS as Hermes, home machine behind a tunnel, or a separate backend with a sanitized snapshot collector? Default to same host as Hermes.

For installation details, read `references/installation.md`. For service discovery details, read `references/service-discovery.md`. For hosting details, read `references/hosting.md`. For the subscription model details, read `references/subscription-tracker-model.md`.

## Quick Start

```bash
mkdir -p ~/.hermes/operator-miniapp
cp ~/.hermes/operator-miniapp-skill/templates/config.yaml ~/.hermes/operator-miniapp/config.yaml
python3 ~/.hermes/operator-miniapp-skill/scripts/operator_miniapp_status.py discover-services --pretty
python3 ~/.hermes/operator-miniapp-skill/scripts/operator_miniapp_status.py --config ~/.hermes/operator-miniapp/config.yaml init-config
python3 ~/.hermes/operator-miniapp-skill/scripts/operator_miniapp_status.py --config ~/.hermes/operator-miniapp/config.yaml collect --pretty
python3 ~/.hermes/operator-miniapp-skill/scripts/operator_miniapp_status.py --config ~/.hermes/operator-miniapp/config.yaml serve --host 127.0.0.1 --port 9120
```

Open locally:

```text
http://127.0.0.1:9120/miniapp
```

The local page will show `Mini App unavailable` unless it is opened through Telegram WebApp or tested with signed Telegram `initData`. Use `collect --pretty` to validate trackers before registering a Telegram menu button.

## Procedure

1. **Install files.** Clone the repo into `~/.hermes/operator-miniapp-skill` and ensure `scripts/operator_miniapp_status.py` exists. Completion: `python3 .../operator_miniapp_status.py --help` exits 0.
2. **Install runtime skill.** Run `packaging/install_manual.py --source . --yes` from the audited checkout. Completion: installer JSON reports `hash_match: true` and the runtime skill directory exists under `HERMES_HOME/skills`.
3. **Discover service candidates.** Run `discover-services --pretty` and review candidate `config` snippets from CLI tools, OAuth scopes, and Hermes gateway platforms. Completion: candidate output contains only labels/reasons/config snippets, no token values.
4. **Create config.** Run `init-config` or copy `templates/config.yaml` and edit it. Completion: config contains no token values, only env var names, token file paths, and command paths.
5. **Configure service display.** Enable only useful candidates under `services`; add custom `command`, `file_exists`, `oauth_token`, or `static` probes for user-specific integrations. Completion: `collect --pretty` shows the selected services with clear `ok`/`limited`/`missing`/`error` statuses.
6. **Configure subscriptions.** For each provider, prefer a `command_json` adapter. Use `command_regex` only for existing text reports. Completion: every tracker returns normalized accounts/windows through `collect --pretty`.
7. **Run locally.** Start `serve --host 127.0.0.1 --port 9120`. Completion: `/health` returns `{"ok": true}`.
8. **Choose hosting.** Default to the same host that runs Hermes, using `templates/hermes-operator-miniapp.service` for the local server. If the host is behind NAT, use Cloudflare Tunnel or Tailscale Funnel/Serve. Completion: the Mini App process is local-only on `127.0.0.1` and has access to local Hermes probes.
9. **Expose narrowly.** Put Caddy/nginx/Cloudflare/Tailscale in front of only `/miniapp`, `/api/status`, and `/health`. Completion: public `/miniapp` returns HTML, public `/api/status` rejects unsigned requests, and the full Hermes dashboard is not reachable through this endpoint.
10. **Register Telegram WebApp.** Set bot menu/button to the HTTPS `/miniapp` URL. Completion: Telegram `getChatMenuButton` returns `type=web_app` with the expected URL.
11. **Verify end to end.** Open the Mini App from Telegram as an allowlisted user. Completion: services, subscriptions, and cron jobs render; unauthorized/signed-invalid requests get 403; no secret appears in page text or API JSON.

## Service Probe Contract

Services live under `services` and are intentionally explicit. They are not auto-discovered from every Hermes credential, because that would add noise and risk exposing internal integration names. `discover-services --pretty` is an onboarding helper: it returns safe candidates, then the user chooses which candidates to enable.

Built-in types:

- `github_cli` checks GitHub CLI auth;
- `tailscale` checks Tailscale device/network status;
- `oauth_token` checks token-file presence and optional OAuth scopes for services such as Google Drive or YouTube;
- `static` displays a configured gateway/platform when no safe live probe exists;
- `command` runs any local probe command;
- `file_exists` checks a local marker file.

Example OAuth service:

```yaml
services:
  - id: google_drive
    label: Google Drive
    type: oauth_token
    enabled: true
    token_paths: [~/.hermes/google_token.json]
    required_scopes: [/auth/drive]
    access_scopes:
      /auth/drive: Drive API
```

Use subscription trackers, not services, for model-provider quota windows.

## Subscription Tracker Contract

Trackers live under `subscription_trackers`:

```yaml
subscription_trackers:
  - id: my-provider
    label: My Provider
    provider: my-provider
    type: command_json
    command: ~/.hermes/scripts/my_provider_usage.py --json
    accounts_path: accounts
    account_label_path: label
    status_path: status
    plan_path: plan
    windows_path: windows
    window_label_path: label
    used_percent_path: used_percent
    remaining_percent_path: remaining_percent
    reset_text_path: reset_text
```

Every tracker normalizes into:

- `available_accounts`;
- `attention_accounts`;
- account `label`, `status`, `plan`;
- window `label`, `used_percent`, `remaining_percent`, `reset_text`.

This lets different providers use different collection scripts while the Mini App UI stays unchanged.

## Security Rules

- Validate Telegram `initData` on the server before returning status.
- Enforce explicit Telegram user allowlist after signature validation.
- Never place bot tokens, API keys, OAuth refresh tokens, cookies, or connection strings in config.
- Keep public reverse proxy narrow; do not proxy the full Hermes dashboard.
- Treat command output as untrusted and redact token-like strings before returning errors.

## Verification

Run from the cloned repo:

```bash
python3 -m py_compile scripts/operator_miniapp_status.py
python3 -m pytest -q
python3 scripts/operator_miniapp_status.py --config tests/fixtures/sample-config.yaml collect --pretty
```

Expected result:

- tests pass;
- sample config collects services/subscriptions/cron without secrets;
- schedule and `next_run_display` use the configured timezone;
- invalid Telegram `initData` is rejected.

## Pitfalls

- A configured credential is not the same as a healthy subscription. Use live provider probes where possible.
- Regex trackers are fragile. Prefer provider scripts that output normalized JSON.
- Telegram WebApp requires HTTPS outside local testing.
- The Mini App page itself may be public, but `/api/status` must require signed Telegram `initData`.
- If a provider CLI is slow or rate-limited, cache its summary with a cron job and point `command_json` at the cached JSON file.
