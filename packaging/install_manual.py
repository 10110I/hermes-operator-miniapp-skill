#!/usr/bin/env python3
"""Manual installer for the Hermes Operator Mini App package.

This installer is intentionally stdlib-only. It copies the audited runtime
skill subtree into the active Hermes home and verifies the copied hashes.
It does not read secrets, run OAuth flows, or start the Mini App backend.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
from typing import Iterable

RUNTIME_PATHS = ("SKILL.md", "references", "templates", "scripts")
DEFAULT_CATEGORY = "autonomous-ai-agents"
DEFAULT_SKILL_NAME = "hermes-operator-miniapp-status"
DEFAULT_HELPER_NAME = "operator-miniapp-skill"
IGNORE_NAMES = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "uv.lock"}


def hermes_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser().resolve()


def repo_commit(source: pathlib.Path) -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001
        return None


def iter_files(root: pathlib.Path, rels: Iterable[str]) -> Iterable[pathlib.Path]:
    for rel in rels:
        path = root / rel
        if path.is_file():
            yield path
        elif path.is_dir():
            for file_path in sorted(child for child in path.rglob("*") if child.is_file()):
                yield file_path


def hash_map(root: pathlib.Path, rels: Iterable[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for file_path in iter_files(root, rels):
        mapping[str(file_path.relative_to(root))] = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return mapping


def backup_existing(path: pathlib.Path) -> pathlib.Path | None:
    if not path.exists():
        return None
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S")
    backup = path.with_name(f"{path.name}.backup-{stamp}")
    counter = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.backup-{stamp}-{counter}")
        counter += 1
    shutil.move(str(path), str(backup))
    return backup


def copy_path(source: pathlib.Path, target: pathlib.Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def ignore_helper_names(_dir: str, names: list[str]) -> set[str]:
    return {name for name in names if name in IGNORE_NAMES}


def copy_runtime_skill(source: pathlib.Path, target: pathlib.Path) -> pathlib.Path | None:
    backup = backup_existing(target)
    target.mkdir(parents=True, exist_ok=True)
    for rel in RUNTIME_PATHS:
        copy_path(source / rel, target / rel)
    return backup


def copy_helper_repo(source: pathlib.Path, target: pathlib.Path) -> pathlib.Path | None:
    if source.resolve() == target.resolve():
        return None
    backup = backup_existing(target)
    shutil.copytree(source, target, ignore=ignore_helper_names)
    return backup


def helper_commit_marker(helper_target: pathlib.Path, fallback_commit: str | None) -> str | None:
    commit = repo_commit(helper_target)
    if commit:
        return commit
    marker = helper_target / ".source-commit"
    if fallback_commit:
        marker.write_text(fallback_commit + "\n", encoding="utf-8")
        return fallback_commit
    if marker.exists():
        return marker.read_text(encoding="utf-8").strip() or None
    return None


def require_source(source: pathlib.Path) -> None:
    missing = [rel for rel in RUNTIME_PATHS if not (source / rel).exists()]
    if missing:
        raise SystemExit(f"Source is not a complete package checkout; missing: {', '.join(missing)}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install the Hermes Operator Mini App runtime skill from an audited checkout.")
    parser.add_argument("--source", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[1], help="Audited repository checkout to install from.")
    parser.add_argument("--hermes-home", type=pathlib.Path, default=hermes_home(), help="Target Hermes home. Defaults to HERMES_HOME or ~/.hermes.")
    parser.add_argument("--category", default=DEFAULT_CATEGORY, help="Skill category under HERMES_HOME/skills.")
    parser.add_argument("--skill-name", default=DEFAULT_SKILL_NAME, help="Runtime skill directory name.")
    parser.add_argument("--helper-name", default=DEFAULT_HELPER_NAME, help="Helper checkout directory name under HERMES_HOME.")
    parser.add_argument("--skip-helper", action="store_true", help="Only install the runtime skill subtree.")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before replacing existing targets.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = args.source.expanduser().resolve()
    home = args.hermes_home.expanduser().resolve()
    require_source(source)

    skill_target = home / "skills" / args.category / args.skill_name
    helper_target = home / args.helper_name

    if not args.yes:
        print("Manual installer will copy audited files into:")
        print(f"  skill:  {skill_target}")
        if not args.skip_helper:
            print(f"  helper: {helper_target}")
        answer = input("Continue? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            print("aborted")
            return 2

    source_hashes = hash_map(source, RUNTIME_PATHS)
    source_commit = repo_commit(source)
    backups: list[str] = []
    skill_backup = copy_runtime_skill(source, skill_target)
    if skill_backup:
        backups.append(str(skill_backup))

    helper_backup: pathlib.Path | None = None
    helper_commit: str | None = None
    if not args.skip_helper:
        helper_backup = copy_helper_repo(source, helper_target)
        if helper_backup:
            backups.append(str(helper_backup))
        helper_commit = helper_commit_marker(helper_target, source_commit)

    installed_hashes = hash_map(skill_target, RUNTIME_PATHS)
    hash_match = source_hashes == installed_hashes
    result = {
        "ok": hash_match,
        "source": str(source),
        "source_commit": source_commit,
        "hermes_home": str(home),
        "skill_target": str(skill_target),
        "helper_target": None if args.skip_helper else str(helper_target),
        "helper_commit": helper_commit,
        "runtime_file_count": len(installed_hashes),
        "hash_match": hash_match,
        "backups": backups,
        "next_steps": [
            "Run /reload-skills or start a fresh Hermes session.",
            "Load the runtime skill: /skill hermes-operator-miniapp-status.",
            "Run the status collector's discover-services command before editing config.",
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if hash_match else 1


if __name__ == "__main__":
    raise SystemExit(main())
