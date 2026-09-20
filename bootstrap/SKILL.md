---
name: hermes-operator-miniapp-bootstrap
description: Bootstrap the Hermes operator Mini App installer.
version: 0.1.0
author: Konstantin, Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [hermes, telegram, mini-app, bootstrap]
    related_skills: []
---

# Hermes Operator Mini App Bootstrap

This is a small bootstrap skill for the Hermes Operator Mini App package. It contains no helper scripts, local server code, provider probes, or setup hooks. Its purpose is to explain the safe installation path for the full package when the full runtime skill is not yet distributed from a trusted Hermes skill hub.

## When to Use

Use this bootstrap when a user wants to install the Hermes Operator Mini App package from the public repository and the full package is treated as a community source by Hermes skill security scanning.

Do not use it as the runtime panel itself. The runtime package is installed separately through the audited manual fallback described in the repository packaging guide.

## Procedure

1. Resolve the exact repository revision the user wants to trust. Prefer a release tag or immutable commit over a moving branch.
2. Review the package repository before installation. Pay special attention to files that read local Hermes state, run local probes, or serve the Mini App backend.
3. Use the repository's manual installer from that audited checkout. It copies the runtime skill files into the active `HERMES_HOME`, writes no secrets, and verifies the copied runtime hashes.
4. After installation, run `/reload-skills` or start a fresh Hermes session, then load `hermes-operator-miniapp-status`.
5. Configure the panel only after the runtime skill is visible. Keep Telegram bot tokens and provider credentials in local environment or provider stores, never in chat or YAML.

## Expected Result

A successful installation has two parts:

- the runtime skill named `hermes-operator-miniapp-status` is available to Hermes;
- the helper repository exists locally so its config template, status collector, hosting templates, and tests can be used.

## Safety Notes

The full package intentionally includes local status probes and a loopback Mini App backend. Those are expected capabilities for this operator panel, but they are also the reason a generic community-skill scanner may block a direct managed install. Treat that block as a prompt for manual audit, not as something to bypass silently.
