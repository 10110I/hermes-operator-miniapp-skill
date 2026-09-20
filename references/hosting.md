# Hosting model

The Mini App has two parts:

1. **Public HTML entrypoint** at `/miniapp`.
2. **Private status API** at `/api/status`, protected by Telegram WebApp `initData` validation and an allowlisted Telegram user ID.

Because the status API reads local Hermes state, cron jobs, credential-health probes, and provider-specific quota collectors, the recommended default is **self-hosting on the same machine that runs Hermes**.

## Recommended topology

```text
Telegram client
  ↓ HTTPS
public reverse proxy: Caddy / nginx / Cloudflare Tunnel / Tailscale Funnel
  ↓ only /miniapp /api/status /health
127.0.0.1:9120 operator_miniapp_status.py
  ↓ local-only
~/.hermes/operator-miniapp/config.yaml
~/.hermes/.env or local env vars
hermes cron list / provider quota probe scripts
```

Default local bind:

```bash
python3 ~/.hermes/operator-miniapp-skill/scripts/operator_miniapp_status.py \
  --config ~/.hermes/operator-miniapp/config.yaml \
  serve --host 127.0.0.1 --port 9120
```

The server should not listen on `0.0.0.0` unless the operator knows exactly why. Keep it on loopback and expose it with a narrow HTTPS proxy.

## Hosting choices

### 1. VPS or always-on Linux host — recommended

Use this when Hermes already runs on a VPS/server.

- Run the helper as a user `systemd` service.
- Use Caddy/nginx for HTTPS.
- Proxy only:
  - `/miniapp`
  - `/api/status`
  - `/health`
- Register `https://your-domain.example/miniapp` as the Telegram WebApp URL.

Use templates:

- `templates/hermes-operator-miniapp.service`
- `templates/Caddyfile`

### 2. Home machine behind NAT

Use a tunnel instead of opening random ports:

- Cloudflare Tunnel for a stable public hostname; or
- Tailscale Funnel/Serve if the user's network policy allows it.

The Mini App process still runs locally on `127.0.0.1:9120`; the tunnel forwards only the narrow paths.

### 3. Static hosting / serverless frontend only — not enough

GitHub Pages, Vercel static hosting, or any frontend-only host can serve HTML, but cannot securely provide this Mini App by itself because `/api/status` must:

- validate Telegram `initData` server-side with the bot token;
- read local Hermes cron/config/provider probe state;
- redact errors before returning data.

A serverless deployment is possible only if the user also builds a separate secure collector that pushes sanitized status snapshots to that backend. That is not the default quick-install path.

## Security requirements

- Public entrypoint may be `/miniapp`.
- `/api/status` must reject missing, malformed, stale, wrong-bot, and non-allowlisted Telegram `initData`.
- Never proxy the full Hermes dashboard through this public endpoint.
- Never put bot tokens, OAuth tokens, API keys, cookies, auth.json, or provider raw responses in HTML, URLs, logs, config examples, or API JSON.
- Put secrets in environment variables or local env files referenced by systemd, not in `config.yaml`.

## Verification checklist

1. `curl http://127.0.0.1:9120/health` returns `{"ok": true}` locally.
2. Public `https://<host>/miniapp` returns HTML.
3. Public `https://<host>/api/status` without `initData` returns 403/401, not status data.
4. A signed allowlisted Telegram WebApp request returns status data.
5. Public proxy cannot access the full Hermes dashboard or arbitrary local paths.
6. Telegram menu/button points to the final HTTPS `/miniapp` URL.
