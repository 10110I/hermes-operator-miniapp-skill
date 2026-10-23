from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer
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


def test_calendar_schedule_is_not_misclassified_as_slider_interval():
    assert app.schedule_kind("every Monday at 09:00") == "cron"


def test_schedule_kind_only_marks_slider_backed_repeating_intervals():
    assert app.schedule_kind("every 5 days") == "interval"


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


def test_parse_cron_list_includes_paused_jobs_and_exposes_editor_state():
    text = """  123456789abc [paused]
    Name: Paused task
    Schedule: every 7200m
    Next run: 2026-10-25T12:43:10+00:00
  abcdef123456 [active]
    Name: Daily task
    Schedule: 0 7 * * *
    Next run: 2026-10-23T07:00:00+00:00
"""

    jobs = app.parse_cron_list(text, app.ZoneInfo("UTC"))

    assert [job["state"] for job in jobs] == ["paused", "active"]
    assert jobs[0]["schedule_kind"] == "interval"
    assert jobs[0]["schedule_minutes"] == 7200
    assert jobs[0]["periodicity_index"] == app.CRON_PERIODICITY_MINUTES.index(7200)
    assert jobs[1]["schedule_kind"] == "cron"
    assert jobs[1]["schedule_minutes"] == 1440


def test_edit_cron_job_updates_model_reasoning_and_selected_period(monkeypatch):
    list_output = """  123456789abc [active]
    Name: Daily task
    Schedule: 0 7 * * *
"""
    calls = []

    def fake_run_command(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return app.subprocess.CompletedProcess(command, 0, list_output, "")
        return app.subprocess.CompletedProcess(command, 0, "updated", "")

    monkeypatch.setattr(app, "run_command", fake_run_command)
    monkeypatch.setattr(app, "cron_job_records", lambda: [{"id": "123456789abc", "model": "gpt-6.1-sol", "provider": "openai-codex", "reasoning_effort": "high", "schedule": {"kind": "interval", "minutes": 60, "display": "every 60m"}}])
    monkeypatch.setattr(app, "cron_model_options", lambda config, jobs: [
        {"value": app.encode_cron_model_choice("openai-codex", "gpt-6.1-sol"), "provider": "openai-codex", "model": "gpt-6.1-sol", "label": "openai-codex · gpt-6.1-sol"}
    ])
    selected_model = app.encode_cron_model_choice("openai-codex", "gpt-6.1-sol")

    result = app.edit_cron_job({"cron": {"enabled": True}}, {
        "job_id": "123456789abc",
        "model_choice": selected_model,
        "reasoning_effort": "high",
        "periodicity_index": app.CRON_PERIODICITY_MINUTES.index(60),
    })

    assert result["ok"] is True
    assert calls[1] == [
        "hermes", "cron", "edit", "123456789abc",
        "--model", "gpt-6.1-sol", "--provider", "openai-codex",
        "--reasoning-effort", "high", "--schedule", "every 60m",
    ]


def test_edit_cron_job_checks_the_saved_state(monkeypatch):
    list_output = "  123456789abc [active]\n    Schedule: 0 7 * * *\n"
    calls = []

    def fake_run_command(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return app.subprocess.CompletedProcess(command, 0, list_output, "")
        return app.subprocess.CompletedProcess(command, 0, "updated", "")

    monkeypatch.setattr(app, "run_command", fake_run_command)
    monkeypatch.setattr(app, "cron_job_records", lambda: [{"id": "123456789abc", "model": "gpt-6.1-sol", "provider": "openai-codex", "reasoning_effort": "medium", "schedule": "every 60m"}])
    selected = app.encode_cron_model_choice("openai-codex", "gpt-6.1-sol")
    monkeypatch.setattr(app, "cron_model_options", lambda config, jobs: [
        {"value": selected, "provider": "openai-codex", "model": "gpt-6.1-sol", "label": "model"}
    ])

    with pytest.raises(ValueError, match="verified"):
        app.edit_cron_job({"cron": {"enabled": True}}, {
            "job_id": "123456789abc", "model_choice": selected,
            "reasoning_effort": "high",
            "periodicity_index": app.CRON_PERIODICITY_MINUTES.index(60),
        })


def test_edit_cron_job_rejects_unlisted_model_before_mutation(monkeypatch):
    calls = []
    list_output = "  123456789abc [active]\n    Schedule: every 15m\n"
    monkeypatch.setattr(app, "run_command", lambda command, **kwargs: (calls.append(command) or app.subprocess.CompletedProcess(command, 0, list_output, "")))
    monkeypatch.setattr(app, "cron_model_options", lambda config, jobs: [])

    with pytest.raises(ValueError, match="model"):
        app.edit_cron_job({"cron": {"enabled": True}}, {
            "job_id": "123456789abc",
            "model_choice": app.encode_cron_model_choice("not-configured", "not-a-model"),
            "reasoning_effort": "medium",
        })

    assert len(calls) == 1


def test_cron_editor_controls_are_sliders_and_model_select():
    html = app.MINIAPP_HTML
    assert 'type="range"' in html
    assert 'data-reasoning' in html
    assert 'data-periodicity' in html
    assert 'data-model' in html
    assert "cron_update" in html


def test_cron_update_endpoint_requires_dedicated_editor_allowlist(monkeypatch):
    token = "123456:TEST_TOKEN"
    monkeypatch.setenv("OPERATOR_TEST_BOT_TOKEN", token)
    config = {
        "telegram": {"bot_token_env": "OPERATOR_TEST_BOT_TOKEN", "allowed_users": [1002597417]},
        "cron": {"enabled": True, "edit_allowed_users": []},
    }
    edit_calls = []
    monkeypatch.setattr(app, "edit_cron_job", lambda *args: edit_calls.append(args) or {"ok": True})
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(app.ServerContext(config)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        body = json.dumps({"action": "cron_update", "init_data": signed_init_data(token)})
        conn.request("POST", "/api/status", body=body, headers={"Content-Type": "application/json"})
        denied = conn.getresponse()
        assert denied.status == 403
        assert denied.getheader("Cache-Control") == "no-store"
        denied.read()
        assert edit_calls == []

        config["cron"]["edit_allowed_users"] = [1002597417]
        conn.request("POST", "/api/status", body=body, headers={"Content-Type": "application/json"})
        allowed = conn.getresponse()
        assert allowed.status == 200
        assert json.loads(allowed.read())["ok"] is True
        assert len(edit_calls) == 1
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
