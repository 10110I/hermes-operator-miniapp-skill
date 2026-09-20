from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_skill_is_thin_and_has_no_runtime_payload_triggers():
    text = (ROOT / "bootstrap" / "SKILL.md").read_text(encoding="utf-8")

    assert "name: hermes-operator-miniapp-bootstrap" in text
    assert "contains no helper scripts" in text
    forbidden = ["git clone", "pip install", "subprocess", "127.0.0.1", "operator_miniapp_status.py"]
    for needle in forbidden:
        assert needle not in text


def test_manual_installer_copies_runtime_skill_with_hash_verification(tmp_path):
    hermes_home = tmp_path / "hermes-home"
    command = [
        sys.executable,
        str(ROOT / "packaging" / "install_manual.py"),
        "--source",
        str(ROOT),
        "--hermes-home",
        str(hermes_home),
        "--yes",
    ]
    result = subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(result.stdout)

    assert payload["ok"] is True
    assert payload["hash_match"] is True
    assert payload["helper_commit"] == payload["source_commit"]
    skill_target = Path(payload["skill_target"])
    helper_target = Path(payload["helper_target"])
    assert skill_target.joinpath("SKILL.md").exists()
    assert skill_target.joinpath("references", "installation.md").exists()
    assert skill_target.joinpath("scripts", "operator_miniapp_status.py").exists()
    assert helper_target.joinpath("packaging", "install_manual.py").exists()
    assert helper_target.joinpath("bootstrap", "SKILL.md").exists()
    assert helper_target.joinpath(".source-commit").read_text(encoding="utf-8").strip() == payload["source_commit"]

    second = subprocess.run(command, check=True, text=True, capture_output=True)
    second_payload = json.loads(second.stdout)
    assert second_payload["hash_match"] is True
    assert second_payload["backups"]
    assert all(".operator-miniapp-backups" in item for item in second_payload["backups"])
    assert not list(hermes_home.joinpath("skills").rglob("*.backup-*/SKILL.md"))


def test_packaging_docs_explain_bootstrap_manual_and_trusted_paths():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    install = (ROOT / "packaging" / "INSTALL.md").read_text(encoding="utf-8")
    reference = (ROOT / "references" / "installation.md").read_text(encoding="utf-8")

    assert "Managed bootstrap skill" in readme
    assert "Audited manual fallback" in readme
    assert "trusted Hermes skill source" in install
    assert "hash_match" in install
    assert "Managed bootstrap" in reference
    assert "Manual runtime install" in reference
