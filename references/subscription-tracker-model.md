# Subscription tracker model

This skill does not hardcode `openai-codex`, Qwen, Claude, or any other provider. Every subscription is a `subscription_tracker` configured by the installer.

## Setup questions

During setup the agent or `scripts/operator_miniapp_status.py init-config` should ask one block per subscription:

1. **Provider and label** — human label, e.g. `OpenAI Codex`, `Qwen`, `Claude`, `SuperGrok`, `Custom API`.
2. **Source** — where the current usage comes from:
   - `command_json`: a local command prints JSON;
   - `command_regex`: a local command prints text and regex patterns extract values;
   - `manual`: static/manual placeholder until a source exists.
3. **Accounts/seats** — whether the subscription has one account or a pool.
4. **Windows** — which quota windows to display: e.g. `5 hours`, `weekly`, `monthly`, `credits`, `requests/day`.
5. **Fields** — for each window, map `used_percent`, `remaining_percent`, and optional `reset_text`; for OpenAI-style reset credits, map optional account-level `reset_bank` and `reset_bank_text`.
6. **Refresh** — default Mini App refresh is on open/click; if a provider is slow, wrap the command in a cache script or cron job.
7. **Secrets** — only ask for env var names or existing local command paths. Never store token values in config.

## Normalized output

All adapters normalize to this API shape:

```json
{
  "id": "provider-id",
  "label": "Provider label",
  "provider": "provider-name",
  "status": "ok|limited|unknown|error",
  "available_accounts": 1,
  "attention_accounts": 0,
  "accounts": [
    {
      "label": "Account label",
      "provider": "provider-name",
      "plan": "Pro",
      "reset_bank": 1,
      "reset_bank_text": "reset bank: 1 доступен",
      "status": "ok",
      "available": true,
      "windows": [
        {
          "key": "weekly",
          "label": "Неделя",
          "used_percent": 42,
          "remaining_percent": 58,
          "reset_text": "сброс через 2д"
        }
      ]
    }
  ]
}
```

The Mini App should only render this normalized shape, so provider-specific logic stays in trackers.

## Recommended tracker types

### command_json

Use this for new adapters. It is stable, testable, and avoids fragile text parsing.

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
    reset_bank_path: reset_bank
    reset_bank_text_path: reset_bank_text
    windows_path: windows
    window_label_path: label
    used_percent_path: used_percent
    remaining_percent_path: remaining_percent
    reset_text_path: reset_text
```

### command_regex

Use this when a provider already has a text report but no JSON output. Prefer converting the text report to JSON later.

```yaml
subscription_trackers:
  - id: text-provider
    label: Text Provider
    provider: text-provider
    type: command_regex
    command: ~/.hermes/scripts/text_provider_usage.sh
    account_start_regex: '(?m)^Account:\s*(?P<label>.+)$'
    status_regex: 'status:\s*(?P<status>ok|limited|reauth|error)'
    window_patterns:
      - key: weekly
        label: Неделя
        regex: 'week:\s*(?P<used>\d+)% used / (?P<remaining>\d+)% remaining'
```

### manual

Use this only to unblock UI setup while an adapter is being written. Mark it clearly in the Mini App or config.

## Security rules

- Config stores commands, paths, env var names, regexes, labels, and allowlisted Telegram IDs.
- Config must not store API keys, OAuth refresh tokens, session cookies, or connection strings.
- Commands must print summaries only; if a provider CLI prints tokens in errors, wrap it and redact before stdout.
- The Mini App endpoint validates Telegram `initData` and an allowlist before returning status data.
- Public exposure should proxy only `/miniapp`, `/api/status`, and `/health` — never a full Hermes dashboard.

## Provider adapter checklist

- [ ] Can run non-interactively.
- [ ] Reads secrets from existing local auth/env, not config.
- [ ] Outputs JSON if possible.
- [ ] Includes account label, status, plan, windows.
- [ ] Has a timeout and a cached fallback if the provider is slow.
- [ ] Redacts all token-like strings before printing errors.
- [ ] Has a sample fixture in tests.
