# Packaging and installation paths

The full Hermes Operator Mini App package contains exactly the capabilities the panel needs: local status probes, a loopback Mini App backend, hosting templates, and helper scripts. Generic community-skill security scanning can therefore block a direct managed install until the package is distributed from a trusted Hermes skill hub.

Use one of the paths below.

## Path A — managed bootstrap skill

Install the small bootstrap skill when you want a Hermes-loadable guide without shipping runtime scripts through the managed skill installer:

```bash
hermes skills install https://raw.githubusercontent.com/10110I/hermes-operator-miniapp-skill/main/bootstrap/SKILL.md
```

The bootstrap skill contains no helper scripts, local server code, provider probes, or setup hooks. It exists only to guide the audited manual install of the full runtime package.

## Path B — audited manual fallback

Use this today for the full working package.

1. Clone or update the repository.
2. Check out the exact release tag or commit you reviewed.
3. Run the stdlib installer from that checkout.

```bash
git clone https://github.com/10110I/hermes-operator-miniapp-skill.git ~/.hermes/operator-miniapp-skill
cd ~/.hermes/operator-miniapp-skill
git checkout <audited-tag-or-commit>
python3 packaging/install_manual.py --source . --yes
```

What the installer does:

- copies only the runtime skill subtree into `HERMES_HOME/skills/autonomous-ai-agents/hermes-operator-miniapp-status`;
- keeps the helper directory at `HERMES_HOME/operator-miniapp-skill`;
- backs up existing targets under `HERMES_HOME/.operator-miniapp-backups/` before replacing them;
- verifies SHA-256 hashes of copied runtime files;
- prints a JSON summary with source commit, target paths, file count, and `hash_match`.

What it does not do:

- it does not read OAuth tokens, bot tokens, cookies, provider credentials, or `.env` values;
- it does not start the Mini App backend;
- it does not register a Telegram WebApp URL;
- it does not edit shell startup files or global package managers.

After install:

```text
/reload-skills
/skill hermes-operator-miniapp-status
```

Then follow the runtime skill setup: run discovery, write config, collect status locally, start the loopback backend, and expose only the narrow Mini App paths.

## Path C — future trusted / hub install

Once the package is published through a trusted Hermes skill source, the full runtime skill can be installed through the normal managed path. Until then, treat a direct managed install of the full package as best-effort: if the scanner blocks it, use Path A plus Path B instead of forcing or weakening the scanner.

## Verification checklist

Run these from the repository checkout after manual installation:

```bash
python3 -m py_compile scripts/operator_miniapp_status.py packaging/install_manual.py
python3 scripts/operator_miniapp_status.py discover-services --pretty
python3 scripts/operator_miniapp_status.py --config tests/fixtures/sample-config.yaml collect --pretty
```

A successful package install has:

- runtime skill directory present under `HERMES_HOME/skills`;
- helper checkout present under `HERMES_HOME/operator-miniapp-skill`;
- installer JSON contains `hash_match: true`;
- `discover-services` returns candidates without token values;
- a fresh Hermes session can load `hermes-operator-miniapp-status`.
