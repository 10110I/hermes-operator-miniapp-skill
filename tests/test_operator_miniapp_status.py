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
