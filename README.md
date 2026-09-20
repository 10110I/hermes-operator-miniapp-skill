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
python3 ~/.hermes/operator-miniapp-skill/scripts/operator_miniapp_status.py --config ~/.hermes/operator-miniapp/config.yaml init-config
python3 ~/.hermes/operator-miniapp-skill/scripts/operator_miniapp_status.py --config ~/.hermes/operator-miniapp/config.yaml collect --pretty
python3 ~/.hermes/operator-miniapp-skill/scripts/operator_miniapp_status.py --config ~/.hermes/operator-miniapp/config.yaml serve --host 127.0.0.1 --port 9120
```

Local URL:

```text
http://127.0.0.1:9120/miniapp
```

For Telegram users, expose it through a narrow HTTPS reverse proxy and register the `/miniapp` URL as a Telegram WebApp menu/button.

## Subscription tracking design

Subscriptions are configured under `subscription_trackers`. The Mini App UI renders a normalized data shape; provider-specific collection happens in adapters.

Supported tracker types:

- `command_json` — preferred; runs a local command that prints JSON;
- `command_regex` — parses existing text reports with named regex groups;
- `manual` — temporary static placeholder.

See [`references/subscription-tracker-model.md`](references/subscription-tracker-model.md) and [`templates/config.yaml`](templates/config.yaml).

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
references/subscription-tracker-model.md  # provider-neutral tracker design
tests/                                    # unit tests + fixtures
```

## Known limitations

- The helper server is intentionally small and dependency-light. For production, run it behind a real HTTPS proxy and systemd/launchd.
- Built-in probes are minimal. Provider-specific quota collection should usually be a small `command_json` script maintained by the user/provider.
- The installer does not create DNS/HTTPS automatically because hosting choices differ.
