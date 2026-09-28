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
    assert account["reset_bank"] == 2
    assert account["reset_bank_text"] == "reset bank: 2 доступны"
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
    assert "resetBank" in html
    assert "reset bank:" in html
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
        app.validate_init_data(init_data, bot_token=bot_token, allowed_users=[])
    with pytest.raises(PermissionError):
        app.validate_init_data(init_data, bot_token=bot_token, allowed_users=None)
    with pytest.raises(PermissionError):
        app.validate_init_data(init_data.replace("hash=", "hash=bad"), bot_token=bot_token, allowed_users=[1002597417])


def test_connected_users_normalizes_multiple_snapshots(tmp_path, monkeypatch):
    snapshots = {
        "friend-one": {"schema": 1, "host": "node-one", "gateway": "active", "disk_free_pct": 82,
                       "mem_available_pct": 60, "load_1m": 0.4,
                       "codex": {"status": "not_configured", "checked_at": app.now_iso()}},
        "friend-two": {"schema": 1, "host": "node-two", "gateway": "active", "disk_free_pct": 36,
                       "mem_available_pct": 49, "load_1m": 0.6,
                       "codex": {"status": "ok", "checked_at": app.now_iso(),
                                 "five_hour_used_pct": 94, "weekly_used_pct": 41}},
    }
    for name, snapshot in snapshots.items():
        (tmp_path / name).write_text(json.dumps(snapshot))
    monkeypatch.setattr(app, "run_command", lambda *_args, **_kw: pytest.fail("never run a command to read a remote user"))
    users = app.collect_connected_users({"connected_users": [
        {"id": "one", "label": "Первый", "type": "snapshot_json", "snapshot_file": str(tmp_path / "friend-one"), "expected_host": "node-one"},
        {"id": "two", "label": "Второй", "type": "snapshot_json", "snapshot_file": str(tmp_path / "friend-two"), "expected_host": "node-two"},
    ]})
    assert len(users) == 2
    assert users[0]["status"] == "ok" and users[0]["profile"]["status"] == "not_configured"
    assert users[1]["status"] == "attention" and users[1]["profile"]["five_hour_used_pct"] == 94
    assert users[1]["server"]["gateway"] == "active"
    assert users[1]["server"]["disk_free_pct"] == 36


def test_connected_user_rejects_invalid_or_mismatched_payload_and_never_echoes_it(tmp_path):
    secret = "token=supersecret-should-not-leak"
    payload = {"schema": 1, "host": "wrong-host", "gateway": secret, "disk_free_pct": 50,
               "mem_available_pct": 50, "codex": {"status": secret, "checked_at": app.now_iso()}}
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(payload))
    config = {"connected_users": [{"id": "friend", "label": "Друг", "type": "snapshot_json",
                                    "snapshot_file": str(snapshot), "expected_host": "expected"}]}
    user = app.collect_connected_users(config)[0]
    assert user["status"] == "unavailable"
    assert "supersecret" not in json.dumps(user)
    config["connected_users"][0]["expected_host"] = "wrong-host"
    user = app.collect_connected_users(config)[0]
    assert user["server"]["gateway"] == "unknown"
    assert user["profile"]["status"] == "usage_unavailable"
    assert "supersecret" not in json.dumps(user)


def test_connected_user_stale_snapshot_and_missing_file(tmp_path):
    old = "2026-01-01T00:00:00+00:00"
    payload = {"schema": 1, "host": "node", "gateway": "active", "disk_free_pct": 82,
               "mem_available_pct": 60, "codex": {"status": "ok", "checked_at": old,
                                                     "five_hour_used_pct": 10}}
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(payload))
    config = {"connected_users": [{"id": "friend", "label": "Друг", "type": "snapshot_json",
                                    "snapshot_file": str(snapshot), "expected_host": "node"}]}
    user = app.collect_connected_users(config)[0]
    assert user["status"] == "attention" and user["profile"]["stale"] is True
    snapshot.unlink()
    user = app.collect_connected_users(config)[0]
    assert user["status"] == "unavailable" and "secret" not in json.dumps(user)

def test_connected_user_missing_limits_and_exact_threshold(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    payload = {"schema": 1, "host": "node", "gateway": "active", "disk_free_pct": 14.9,
               "mem_available_pct": 50, "codex": {"status": "ok", "checked_at": app.now_iso(),
                                                  "five_hour_used_pct": None, "weekly_used_pct": None}}
    snapshot.write_text(json.dumps(payload))
    config = {"connected_users": [{"id": "friend", "type": "snapshot_json", "snapshot_file": str(snapshot), "expected_host": "node"}]}
    user = app.collect_connected_users(config)[0]
    assert user["status"] == "attention"
    assert user["profile"]["status"] == "usage_unavailable"
    assert user["server"]["disk_free_pct"] == 15  # display rounding must not hide 14.9 alert

def test_connected_user_caps_snapshot_file_before_json_parsing(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text('x' * 5000 + 'token=secret')
    user = app.collect_connected_user({"id": "friend", "type": "snapshot_json", "snapshot_file": str(snapshot), "expected_host": "node"})
    assert user["status"] == "unavailable" and "secret" not in json.dumps(user)


def test_connected_users_tab_has_navigation_and_empty_state():
    html = app.MINIAPP_HTML
    assert 'id="connected-tab"' in html
    assert 'id="overview-tab"' in html
    assert 'role="tablist"' in html
    assert 'aria-selected' in html
    assert 'id="connected-panel"' in html
    assert "Подключённых пользователей пока нет" in html
