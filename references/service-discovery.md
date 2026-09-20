# Service discovery and display

The Mini App should not guess that every credential or config entry is useful to show. Different Hermes users connect different platforms and external services, so setup uses a two-step model:

1. **Discover candidates** from safe local signals.
2. **Enable only selected probes** in `services`.

This keeps the panel portable without turning it into a noisy debug dump.

## Discovery sources

`discover-services` reads only safe metadata and returns candidate config snippets. It does not expose token values or enable candidates automatically.

Current discovery sources:

- installed CLI tools:
  - `gh` → GitHub candidate;
  - `tailscale` → Tailscale candidate;
- local OAuth token metadata:
  - `~/.hermes/google_token.json` scopes → Google Drive, Gmail, Calendar, Sheets, Docs, Contacts candidates;
  - `~/.hermes/youtube_token.json`, `youtube_analytics_token.json`, `youtube_uqi_token.json` scopes → YouTube candidate;
- Hermes config:
  - `platforms:` / `gateway.platforms:` → gateway platform candidates such as Telegram, Slack, Discord, Email.

Run:

```bash
python3 ~/.hermes/operator-miniapp-skill/scripts/operator_miniapp_status.py discover-services --pretty
```

A candidate looks like:

```json
{
  "id": "google_drive",
  "label": "Google Drive",
  "source": "oauth_token",
  "reason": "token file with matching scopes: google_token.json",
  "config": {
    "id": "google_drive",
    "label": "Google Drive",
    "type": "oauth_token",
    "enabled": true,
    "token_paths": ["~/.hermes/google_token.json"],
    "required_scopes": ["/auth/drive"]
  }
}
```

Copy selected `config` objects into `services`, or use `init-config` and accept the discovered candidates interactively.

## Display rules

The status API renders only entries under:

```yaml
services:
  - id: ...
    enabled: true
```

It does not display every discovered candidate. Discovery is an onboarding helper; `services` remains the explicit display contract.

Service statuses:

- `ok` — selected probe works or the platform is configured;
- `limited` — something exists, but required scope/access is missing;
- `missing` — expected local token/file is absent;
- `error` — probe command failed;
- `unknown` — unsupported probe type.

## Extending discovery

For a new service class, prefer one of the generic probe types before writing provider-specific code:

- `command` for a CLI or script that can prove health;
- `oauth_token` for token-file presence and scopes;
- `file_exists` for a simple local marker;
- `static` for a configured gateway/platform that cannot be live-probed safely.

If a provider has quotas or subscription windows, put that in `subscription_trackers`, not in `services`.

## Privacy constraints

Discovery must never return:

- API keys or OAuth tokens;
- raw token JSON;
- connection strings;
- full provider API responses;
- personal account identifiers unless the user explicitly configures a display label.

Return only service label, source, reason, and a redacted config snippet.
