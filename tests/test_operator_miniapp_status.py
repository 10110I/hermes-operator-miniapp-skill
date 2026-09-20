from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from pathlib import Path

import pytest

from scripts import operator_miniapp_status as app

ROOT = Path(__file__).resolve().parents[1]


def signed_init_data(bot_token: str, user_id: int = 1002597417) -> str:
    params = {
        "auth_date": str(int(time.time())),
        "query_id": "AAEAAAE",
        "user": json.dumps({"id": user_id, "first_name": "Tester"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(params.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(params)


def test_oauth_token_service_reports_scope_access(tmp_path):
    token = tmp_path / "google_token.json"
    token.write_text(json.dumps({"scopes": ["https://www.googleapis.com/auth/drive.metadata.readonly"]}), encoding="utf-8")
    config = {
        "services": [
            {
                "id": "google_drive",
                "label": "Google Drive",
                "type": "oauth_token",
                "enabled": True,
                "token_paths": [str(token)],
                "required_scopes": ["/auth/drive"],
                "access_scopes": {"/auth/drive": "Drive API"},
                "access": ["OAuth"],
            }
        ]
    }

    services = app.collect_services(config)

    assert services == [
        {
            "id": "google_drive",
            "label": "Google Drive",
            "status": "ok",
            "caption": "OAuth token найден",
            "access": ["Drive API"],
        }
    ]


def test_oauth_token_service_reports_missing_and_limited_scope(tmp_path):
    missing_config = {"services": [{"id": "svc", "type": "oauth_token", "token_paths": [str(tmp_path / "missing.json")]}]}
    assert app.collect_services(missing_config)[0]["status"] == "missing"

    token = tmp_path / "token.json"
    token.write_text(json.dumps({"scope": "profile email"}), encoding="utf-8")
    limited_config = {
        "services": [
            {"id": "drive", "type": "oauth_token", "token_paths": [str(token)], "required_scopes": ["/auth/drive"], "access": ["OAuth"]}
        ]
    }
    service = app.collect_services(limited_config)[0]
    assert service["status"] == "limited"
    assert "/auth/drive" in service["caption"]


def test_static_service_reports_configured_gateway():
    services = app.collect_services({"services": [{"id": "gateway_telegram", "label": "Telegram Gateway", "type": "static", "status": "ok", "caption": "configured in Hermes gateway", "access": ["gateway"]}]})

    assert services == [{"id": "gateway_telegram", "label": "Telegram Gateway", "status": "ok", "caption": "configured in Hermes gateway", "access": ["gateway"]}]


def test_discover_service_candidates_from_tokens_cli_and_gateway_config(tmp_path, monkeypatch):
    monkeypatch.setattr(app.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "gh" else None)
    (tmp_path / "google_token.json").write_text(
        json.dumps({"scopes": ["https://www.googleapis.com/auth/drive.metadata.readonly", "https://www.googleapis.com/auth/gmail.readonly"]}),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("platforms:\n  - telegram\n  - slack\n", encoding="utf-8")

    candidates = app.discover_service_candidates(tmp_path, config_path)
    by_id = {candidate["id"]: candidate for candidate in candidates}

    assert "github" in by_id
    assert by_id["github"]["config"]["type"] == "github_cli"
    assert by_id["google_drive"]["config"]["type"] == "oauth_token"
    assert by_id["google_drive"]["config"]["token_paths"] == ["~/.hermes/google_token.json"]
    assert by_id["gmail"]["config"]["label"] == "Gmail"
    assert by_id["gateway_telegram"]["config"]["type"] == "static"
    assert by_id["gateway_slack"]["source"] == "hermes_config"


def test_command_json_tracker_normalizes_windows():
    config = app.load_config(ROOT / "tests" / "fixtures" / "sample-config.yaml")
    tracker = config["subscription_trackers"][0]

    summary = app.collect_command_json_tracker(tracker)

    assert summary["status"] == "ok"
    assert summary["available_accounts"] == 1
    account = summary["accounts"][0]
    assert account["label"] == "JSON Account"
    assert account["plan"] == "Pro"
    assert account["windows"][0] == {
        "key": "five_hour",
        "label": "5 часов",
        "used_percent": 40,
        "remaining_percent": 60,
        "reset_text": "reset in 2h",
    }


def test_subscription_limit_labels_use_compact_mobile_layout():
    html = app.MINIAPP_HTML

    assert "limit-head" in html
    assert "limit-stats" in html
    assert "status-badge" in html
    assert "использовано" in html
    assert "потрачено" not in html
    assert "barlabel" not in html


def test_refresh_button_uses_explicit_click_handler_and_no_store_fetch():
    html = app.MINIAPP_HTML

    assert 'id="refresh" type="button"' in html
    assert "addEventListener('click'" in html
    assert "Обновляю…" in html
    assert "Обновляю данные…" in html
    assert "cache:'no-store'" in html
    assert "'cache-control':'no-cache'" in html
    assert "/api/status?refresh=" in html


def test_command_regex_tracker_parses_text_report():
    config = app.load_config(ROOT / "tests" / "fixtures" / "sample-config.yaml")
    tracker = config["subscription_trackers"][1]

    summary = app.collect_command_regex_tracker(tracker)

    assert summary["status"] == "ok"
    assert summary["accounts"][0]["label"] == "Regex Account"
    assert summary["accounts"][0]["plan"] == "Team"
    assert summary["accounts"][0]["windows"][0]["used_percent"] == 70
    assert summary["accounts"][0]["windows"][1]["remaining_percent"] == 75


def test_cron_schedule_and_next_run_use_configured_timezone():
    zone = app.ZoneInfo("Europe/Moscow")
    text = (ROOT / "tests" / "fixtures" / "cron.txt").read_text(encoding="utf-8")

    jobs = app.parse_cron_list(text, zone)

    assert jobs[0]["schedule_display"] == "каждые 5 дней"
    assert jobs[0]["next_run_display"].endswith("МСК")
    assert jobs[1]["schedule_display"] == "ежедневно в 04:00 МСК"
    assert "04:00" in jobs[1]["next_run_display"]


def test_collect_status_redacts_secret_like_errors(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("print('token=supersecret1234567890')\nraise SystemExit(1)\n", encoding="utf-8")
    config = {
        "time_zone": "Europe/Moscow",
        "services": [],
        "subscription_trackers": [
            {"id": "bad", "label": "Bad", "provider": "bad", "type": "command_json", "command": f"python3 {bad}"}
        ],
        "cron": {"enabled": False},
    }

    status = app.collect_status(config)

    error = status["subscriptions"][0]["error"]
    assert "[REDACTED]" in error
    assert "supersecret" not in error


def test_telegram_init_data_signature_and_allowlist():
    bot_token = "123456:TEST_TOKEN"
    init_data = signed_init_data(bot_token)

    user = app.validate_init_data(init_data, bot_token=bot_token, allowed_users=[1002597417])

    assert user["id"] == 1002597417
    with pytest.raises(PermissionError):
        app.validate_init_data(init_data, bot_token=bot_token, allowed_users=[42])
    with pytest.raises(PermissionError):
        app.validate_init_data(init_data.replace("hash=", "hash=bad"), bot_token=bot_token, allowed_users=[1002597417])
