# Installation model

The package has two public installation layers.

## Managed bootstrap

Use the bootstrap skill when Hermes should install only a small scanner-friendly guide. It has no runtime scripts or local backend code.

```bash
hermes skills install https://raw.githubusercontent.com/10110I/hermes-operator-miniapp-skill/main/bootstrap/SKILL.md
```

## Manual runtime install

Use the manual fallback for the full working runtime package. It is the expected path while the package is distributed as a community repository rather than a trusted Hermes skill source.

```bash
git clone https://github.com/10110I/hermes-operator-miniapp-skill.git ~/.hermes/operator-miniapp-skill
cd ~/.hermes/operator-miniapp-skill
git checkout <audited-tag-or-commit>
python3 packaging/install_manual.py --source . --yes
```

The manual installer copies the runtime skill subtree into the active `HERMES_HOME`, backs up existing targets, and verifies SHA-256 hashes of copied runtime files. It does not read secrets, start the Mini App backend, register Telegram, or run OAuth.

After installing the runtime skill, reload Hermes skills or start a fresh session, then load:

```text
/skill hermes-operator-miniapp-status
```

For the operator-facing install matrix and verification checklist, see the repository file `packaging/INSTALL.md` in the helper checkout.
