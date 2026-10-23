#!/usr/bin/env python3
"""Standalone Telegram Mini App status panel for Hermes operators.

The script is intentionally self-contained enough to be installed by a Hermes
skill. It does not know about one user's subscriptions: subscriptions are
configured as trackers with command/json/regex adapters.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import html
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from zoneinfo import ZoneInfo

try:  # YAML is nicer for humans; JSON remains available as fallback.
    import yaml  # type: ignore
except Exception:  # pragma: no cover - exercised only on minimal installs
    yaml = None

DEFAULT_CONFIG_PATH = pathlib.Path.home() / ".hermes" / "operator-miniapp" / "config.yaml"
DEFAULT_TIME_ZONE = "Europe/Moscow"
SECRET_PATTERNS = [
    re.compile(r"(?i)(token|secret|password|authorization|api[_-]?key)\s*[:=]\s*[^\s,;}]+"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
]


class ConfigError(RuntimeError):
    """Raised when the Mini App config is invalid."""


def redact(value: Any) -> Any:
    """Redact token-like content before it reaches API responses/logs."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {str(key): redact(item) for key, item in value.items()}
    text = str(value)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1) if m.groups() else 'secret'}=[REDACTED]", text)
    return text[:5000]


def load_config(path: pathlib.Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json" or yaml is None:
        data = json.loads(text)
    else:
        data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ConfigError("config root must be an object")
    return data


def write_config(config: dict[str, Any], path: pathlib.Path = DEFAULT_CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if yaml is None:
        path = path.with_suffix(".json")
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")


def get_path(data: Any, path: str | None, default: Any = None) -> Any:
    """Resolve a simple dotted path like accounts.0.windows."""
    if not path:
        return data
    current = data
    for part in str(path).split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except Exception:
                return default
        elif isinstance(current, dict):
            if part not in current:
                return default
            current = current[part]
        else:
            return default
    return current


def as_command(command: str | list[str]) -> list[str]:
    if isinstance(command, list):
        return [os.path.expandvars(os.path.expanduser(str(part))) for part in command]
    return shlex.split(os.path.expandvars(os.path.expanduser(command)))


def run_command(command: str | list[str], *, timeout: int = 30, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        as_command(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        cwd=os.path.expanduser(cwd) if cwd else None,
    )


def status_from_returncode(code: int, ok_status: str = "ok", fail_status: str = "error") -> str:
    return ok_status if code == 0 else fail_status


# ---------------------------------------------------------------------------
# Telegram WebApp auth


def validate_init_data(init_data: str, *, bot_token: str, allowed_users: list[int] | None, max_age_seconds: int = 86400) -> dict[str, Any]:
    parsed = urllib.parse.parse_qs(init_data, strict_parsing=True)
    flat = {key: values[-1] for key, values in parsed.items()}
    received_hash = flat.pop("hash", None)
    if not received_hash:
        raise PermissionError("missing Telegram hash")
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(flat.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise PermissionError("invalid Telegram signature")

    auth_date = int(flat.get("auth_date", "0") or "0")
    if max_age_seconds > 0 and abs(int(time.time()) - auth_date) > max_age_seconds:
        raise PermissionError("Telegram initData expired")
    user = json.loads(flat.get("user", "{}") or "{}")
    user_id = int(user.get("id") or 0)
    if allowed_users and user_id not in allowed_users:
        raise PermissionError("Telegram user is not allowed")
    return {"id": user_id, "first_name": user.get("first_name"), "username": user.get("username")}


def authenticate_telegram_request(init_data: str, telegram: dict[str, Any], *, allowed_users: list[int] | None = None, require_allowlist: bool = False) -> dict[str, Any]:
    token_env = str(telegram.get("bot_token_env", "TELEGRAM_BOT_TOKEN"))
    bot_token = os.environ.get(token_env, "")
    if not bot_token:
        raise PermissionError(f"bot token env var is not configured: {token_env}")
    allowlist = telegram.get("allowed_users", []) if allowed_users is None else allowed_users
    try:
        allowed = [int(value) for value in allowlist]
    except (TypeError, ValueError):
        raise PermissionError("Telegram user allowlist is invalid") from None
    if not allowed:
        raise PermissionError("Cron edits require an explicit editor allowlist" if require_allowlist else "Telegram user allowlist is empty")
    return validate_init_data(init_data, bot_token=bot_token, allowed_users=allowed, max_age_seconds=int(telegram.get("max_age_seconds", 86400)))


# ---------------------------------------------------------------------------
# Time and cron helpers


def tz(config: dict[str, Any]) -> ZoneInfo:
    name = str(config.get("time_zone") or DEFAULT_TIME_ZONE)
    return ZoneInfo(name)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def zone_label(zone: ZoneInfo) -> str:
    return "МСК" if zone.key == "Europe/Moscow" else zone.key


def format_local(value: str | None, zone: ZoneInfo) -> str | None:
    if not value:
        return None
    text = str(value)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    local = parsed.astimezone(zone)
    return f"{local:%d.%m %H:%M} {zone_label(zone)}"


def plural_ru(value: int, one: str, few: str, many: str) -> str:
    value = abs(int(value))
    last = value % 10
    last2 = value % 100
    if last == 1 and last2 != 11:
        return one
    if 2 <= last <= 4 and not 12 <= last2 <= 14:
        return few
    return many


def humanize_minutes(total_minutes: int) -> str:
    if total_minutes <= 0:
        return "по расписанию"
    if total_minutes % 10080 == 0:
        weeks = total_minutes // 10080
        return "каждую неделю" if weeks == 1 else f"каждые {weeks} {plural_ru(weeks, 'неделю', 'недели', 'недель')}"
    if total_minutes % 1440 == 0:
        days = total_minutes // 1440
        return "каждый день" if days == 1 else f"каждые {days} {plural_ru(days, 'день', 'дня', 'дней')}"
    if total_minutes % 60 == 0:
        hours = total_minutes // 60
        return "каждый час" if hours == 1 else f"каждые {hours} {plural_ru(hours, 'час', 'часа', 'часов')}"
    return "каждую минуту" if total_minutes == 1 else f"каждые {total_minutes} {plural_ru(total_minutes, 'минуту', 'минуты', 'минут')}"


def cron_time_text(minute: str, hour: str, zone: ZoneInfo) -> tuple[str, int]:
    if minute == "*" and hour == "*":
        return "каждую минуту", 0
    if hour == "*" and minute.isdigit():
        return f"каждый час в :{int(minute):02d} {zone.key}", 0
    if minute.isdigit() and hour.isdigit():
        utc_minutes = int(hour) * 60 + int(minute)
        base = dt.datetime(2026, 1, 5, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=utc_minutes)
        local = base.astimezone(zone)
        offset_days = (local.date() - base.date()).days
        label = "МСК" if zone.key == "Europe/Moscow" else zone.key
        return f"в {local:%H:%M} {label}", offset_days
    return "по cron-расписанию", 0


def humanize_schedule(schedule: str | None, zone: ZoneInfo) -> str:
    text = str(schedule or "").strip()
    if not text:
        return "без расписания"
    interval = re.fullmatch(r"every\s+(\d+)\s*([smhdw]?)", text, flags=re.IGNORECASE)
    if interval:
        value = int(interval.group(1))
        unit = (interval.group(2) or "m").lower()
        minutes = value
        if unit == "s":
            minutes = max(1, round(value / 60))
        elif unit == "h":
            minutes = value * 60
        elif unit == "d":
            minutes = value * 1440
        elif unit == "w":
            minutes = value * 10080
        return humanize_minutes(minutes)

    parts = text.split()
    if len(parts) == 5:
        minute, hour, day_of_month, month, day_of_week = parts
        time_text, day_offset = cron_time_text(minute, hour, zone)
        if minute == "*" and hour == "*" and day_of_month == "*" and month == "*" and day_of_week == "*":
            return time_text
        if day_of_month == "*" and month == "*" and day_of_week == "*":
            return f"ежедневно {time_text}"
        weekdays = ["воскресенье", "понедельник", "вторник", "среду", "четверг", "пятницу", "субботу"]
        if day_of_month == "*" and month == "*" and day_of_week in {"0", "1", "2", "3", "4", "5", "6", "7"}:
            idx = 0 if day_of_week in {"0", "7"} else int(day_of_week)
            return f"каждый {weekdays[(idx + day_offset) % 7]} {time_text}"
        if day_of_month.isdigit() and month == "*" and day_of_week == "*":
            return f"ежемесячно {int(day_of_month)}-го числа {time_text}"
    return text


CRON_PERIODICITY_MINUTES = (1, 2, 5, 10, 15, 30, 60, 120, 360, 720, 1440, 2880, 4320, 7200, 10080, 20160, 43200)
CRON_EDITABLE_STATES = {"active", "paused", "scheduled"}
CRON_REASONING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
CRON_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")


def schedule_period_minutes(schedule: Any) -> int | None:
    if isinstance(schedule, dict):
        minutes = schedule.get("minutes")
        if schedule.get("kind") == "interval" and type(minutes) is int and minutes > 0:
            return minutes
        schedule = schedule.get("display") or schedule.get("expr") or ""
    text = str(schedule or "").strip().lower()
    if text.startswith("once "):
        return None
    match = re.fullmatch(r"every\s+(\d+)\s*(m|min|mins|minutes|h|hr|hrs|hours|d|day|days|w|week|weeks)", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        factor = 1 if unit.startswith("m") else (60 if unit.startswith(("h",)) else (1440 if unit.startswith("d") else 10080))
        return max(1, amount * factor)

    fields = text.split()
    if len(fields) != 5:
        return None
    minute, hour, day, month, weekday = fields
    if minute.startswith("*/") and all(value == "*" for value in (hour, day, month, weekday)):
        try:
            return max(1, int(minute[2:]))
        except ValueError:
            return None
    if hour.startswith("*/") and minute.isdigit() and day == month == weekday == "*":
        try:
            return max(1, int(hour[2:]) * 60)
        except ValueError:
            return None
    if minute.isdigit() and hour.isdigit() and day == month == weekday == "*":
        return 1440
    if minute.isdigit() and hour.isdigit() and day == month == "*" and weekday != "*":
        return 10080
    if minute.isdigit() and hour.isdigit() and day.isdigit() and month == "*" and weekday == "*":
        return 43200
    return None


def periodicity_index(minutes: int | None) -> int | None:
    if minutes is None:
        return None
    return min(range(len(CRON_PERIODICITY_MINUTES)), key=lambda index: abs(CRON_PERIODICITY_MINUTES[index] - minutes))


def schedule_kind(schedule: str) -> str:
    text = str(schedule or "").strip().lower()
    if text.startswith("once "):
        return "once"
    if text.startswith("every ") and schedule_period_minutes(text) is not None:
        return "interval"
    return "cron"


def parse_cron_list(output: str, zone: ZoneInfo) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    first = re.compile(r"^\s*([0-9a-f]{12})\s+\[([^\]]+)\]")
    field = re.compile(r"^\s*([A-Za-z ]+):\s+(.*)$")

    def append_current() -> None:
        if not current or current.get("state") not in CRON_EDITABLE_STATES:
            return
        minutes = schedule_period_minutes(current.get("schedule", ""))
        current["schedule_kind"] = schedule_kind(current.get("schedule", ""))
        current["schedule_minutes"] = minutes
        current["periodicity_index"] = periodicity_index(minutes)
        jobs.append(current)

    for raw in output.splitlines():
        first_match = first.match(raw)
        if first_match:
            append_current()
            current = {"id": first_match.group(1), "state": first_match.group(2).strip().lower(), "name": "", "schedule": "", "next_run": None, "script": None}
            continue
        if not current:
            continue
        field_match = field.match(raw)
        if not field_match:
            continue
        key = field_match.group(1).strip().lower().replace(" ", "_")
        value = field_match.group(2).strip()
        if key in current:
            current[key] = value
        if key == "schedule":
            current["schedule_display"] = humanize_schedule(value, zone)
        elif key == "next_run":
            current["next_run_display"] = format_local(value, zone)
    append_current()
    return jobs


# ---------------------------------------------------------------------------
# Services and subscriptions


def hermes_home() -> pathlib.Path:
    return pathlib.Path(os.path.expandvars(os.path.expanduser(os.getenv("HERMES_HOME", "~/.hermes"))))


def _token_paths(item: dict[str, Any]) -> list[pathlib.Path]:
    raw_paths = item.get("token_paths") or item.get("paths") or item.get("path") or []
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    return [pathlib.Path(os.path.expandvars(os.path.expanduser(str(path)))) for path in raw_paths]


def _read_token_scopes(path: pathlib.Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    scopes = data.get("scopes") or data.get("scope") or []
    if isinstance(scopes, str):
        return [scope for scope in scopes.split() if scope]
    if isinstance(scopes, list):
        return [str(scope) for scope in scopes if str(scope).strip()]
    return []


def _service_candidate(candidate_id: str, label: str, source: str, reason: str, config: dict[str, Any]) -> dict[str, Any]:
    return redact({"id": candidate_id, "label": label, "source": source, "reason": reason, "config": config})


def _add_candidate(candidates: list[dict[str, Any]], candidate: dict[str, Any]) -> None:
    if not any(existing.get("id") == candidate.get("id") for existing in candidates):
        candidates.append(candidate)


def _oauth_candidate_from_token(
    *,
    candidates: list[dict[str, Any]],
    token_path: pathlib.Path,
    config_token_path: str,
    scopes: list[str],
    candidate_id: str,
    label: str,
    needles: list[str],
    access_scopes: dict[str, str],
) -> None:
    if not any(any(needle in scope for scope in scopes) for needle in needles):
        return
    config = {
        "id": candidate_id,
        "label": label,
        "type": "oauth_token",
        "enabled": True,
        "token_paths": [config_token_path],
        "required_scopes": needles[:1],
        "access_scopes": access_scopes,
        "access": ["OAuth"],
    }
    _add_candidate(candidates, _service_candidate(candidate_id, label, "oauth_token", f"token file with matching scopes: {token_path.name}", config))


def _gateway_platform_names(config_path: pathlib.Path) -> list[str]:
    if not config_path.exists():
        return []
    try:
        data = load_config(config_path)
    except Exception:
        return []
    gateway_raw = data.get("gateway")
    gateway = gateway_raw if isinstance(gateway_raw, dict) else {}
    raw = data.get("platforms") or gateway.get("platforms")
    names: list[str] = []
    if isinstance(raw, dict):
        names.extend(str(key) for key, value in raw.items() if value is not False)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                name = item.get("id") or item.get("name") or item.get("platform")
                if name and item.get("enabled", True) is not False:
                    names.append(str(name))
            elif item:
                names.append(str(item))
    return sorted(set(names))


def discover_service_candidates(hermes_home_path: pathlib.Path | None = None, hermes_config_path: pathlib.Path | None = None) -> list[dict[str, Any]]:
    """Find safe display candidates without enabling them automatically."""
    home = hermes_home_path or hermes_home()
    config_path = hermes_config_path or (home / "config.yaml")
    candidates: list[dict[str, Any]] = []

    if shutil.which("gh"):
        _add_candidate(candidates, _service_candidate("github", "GitHub", "cli", "gh CLI is installed; runtime probe checks auth", {"id": "github", "label": "GitHub", "type": "github_cli", "enabled": True, "access": ["repo/auth"]}))
    if shutil.which("tailscale"):
        _add_candidate(candidates, _service_candidate("tailscale", "Tailscale", "cli", "tailscale CLI is installed; runtime probe checks status", {"id": "tailscale", "label": "Tailscale", "type": "tailscale", "enabled": True, "access": ["device/network"]}))

    google_token = home / "google_token.json"
    if google_token.exists():
        try:
            scopes = _read_token_scopes(google_token)
        except Exception:
            scopes = []
        google_config_path = "~/.hermes/google_token.json"
        _oauth_candidate_from_token(candidates=candidates, token_path=google_token, config_token_path=google_config_path, scopes=scopes, candidate_id="google_drive", label="Google Drive", needles=["/auth/drive"], access_scopes={"/auth/drive": "Drive API"})
        _oauth_candidate_from_token(candidates=candidates, token_path=google_token, config_token_path=google_config_path, scopes=scopes, candidate_id="gmail", label="Gmail", needles=["gmail", "mail.google.com"], access_scopes={"gmail": "Gmail API", "mail.google.com": "Gmail API"})
        _oauth_candidate_from_token(candidates=candidates, token_path=google_token, config_token_path=google_config_path, scopes=scopes, candidate_id="google_calendar", label="Google Calendar", needles=["/auth/calendar"], access_scopes={"/auth/calendar": "Calendar API"})
        _oauth_candidate_from_token(candidates=candidates, token_path=google_token, config_token_path=google_config_path, scopes=scopes, candidate_id="google_sheets", label="Google Sheets", needles=["/auth/spreadsheets"], access_scopes={"/auth/spreadsheets": "Sheets API"})
        _oauth_candidate_from_token(candidates=candidates, token_path=google_token, config_token_path=google_config_path, scopes=scopes, candidate_id="google_docs", label="Google Docs", needles=["/auth/documents"], access_scopes={"/auth/documents": "Docs API"})
        _oauth_candidate_from_token(candidates=candidates, token_path=google_token, config_token_path=google_config_path, scopes=scopes, candidate_id="google_contacts", label="Google Contacts", needles=["/auth/contacts"], access_scopes={"/auth/contacts": "Contacts API"})

    youtube_paths = [home / "youtube_token.json", home / "youtube_analytics_token.json", home / "youtube_uqi_token.json"]
    for path in youtube_paths:
        if not path.exists():
            continue
        try:
            scopes = _read_token_scopes(path)
        except Exception:
            scopes = []
        _oauth_candidate_from_token(candidates=candidates, token_path=path, config_token_path=f"~/.hermes/{path.name}", scopes=scopes, candidate_id="youtube", label="YouTube", needles=["youtube", "yt-analytics"], access_scopes={"youtube.upload": "upload", "youtube.force-ssl": "manage", "yt-analytics": "analytics"})

    for platform in _gateway_platform_names(config_path):
        platform_id = re.sub(r"[^a-z0-9_]+", "_", platform.lower()).strip("_") or "platform"
        label = f"{platform.title()} Gateway"
        config = {"id": f"gateway_{platform_id}", "label": label, "type": "static", "enabled": True, "status": "ok", "caption": "configured in Hermes gateway", "access": ["gateway"]}
        _add_candidate(candidates, _service_candidate(config["id"], label, "hermes_config", f"platform listed in {config_path.name}", config))

    return candidates


def _collect_oauth_token_service(item: dict[str, Any], result: dict[str, Any]) -> None:
    token_path = next((path for path in _token_paths(item) if path.exists()), None)
    if token_path is None:
        result["status"] = "missing"
        result["caption"] = "OAuth token не найден"
        return

    scopes = _read_token_scopes(token_path)
    required = [str(scope) for scope in item.get("required_scopes", [])]
    missing = [needle for needle in required if not any(needle in scope for scope in scopes)]
    if missing:
        result["status"] = "limited"
        result["caption"] = "OAuth есть, но не хватает scope: " + ", ".join(missing[:3])
    else:
        result["status"] = "ok"
        result["caption"] = "OAuth token найден"

    access_map = item.get("access_scopes") or {}
    access = []
    if isinstance(access_map, dict):
        for needle, label in access_map.items():
            if any(str(needle) in scope for scope in scopes):
                access.append(str(label))
    if access:
        result["access"] = access


def collect_services(config: dict[str, Any]) -> list[dict[str, Any]]:
    services = []
    for item in config.get("services", []):
        if not item.get("enabled", True):
            continue
        service_type = item.get("type", "command")
        result = {"id": item.get("id"), "label": item.get("label") or item.get("id"), "status": "unknown", "caption": item.get("caption", ""), "access": item.get("access", [])}
        try:
            if service_type == "command":
                proc = run_command(item["command"], timeout=int(item.get("timeout_seconds", 20)))
                text = (proc.stdout or "") + "\n" + (proc.stderr or "")
                ok_regex = item.get("success_regex")
                result["status"] = "ok" if proc.returncode == 0 and (not ok_regex or re.search(ok_regex, text, re.I)) else "error"
            elif service_type == "file_exists":
                result["status"] = "ok" if pathlib.Path(os.path.expanduser(str(item["path"]))).exists() else "missing"
            elif service_type == "oauth_token":
                _collect_oauth_token_service(item, result)
            elif service_type == "static":
                result["status"] = str(item.get("status") or "ok")
                result["caption"] = str(item.get("caption") or result.get("caption") or "configured")
            elif service_type == "tailscale":
                proc = run_command(["tailscale", "status", "--json"], timeout=20)
                result["status"] = status_from_returncode(proc.returncode)
                if proc.returncode == 0:
                    data = json.loads(proc.stdout)
                    result["caption"] = data.get("Self", {}).get("DNSName") or "Tailscale connected"
            elif service_type == "github_cli":
                proc = run_command(["gh", "auth", "status"], timeout=20)
                result["status"] = status_from_returncode(proc.returncode)
            else:
                result["status"] = "unknown"
                result["caption"] = f"unknown service type: {service_type}"
        except Exception as exc:  # noqa: BLE001
            result["status"] = "error"
            result["caption"] = str(redact(exc))[:180]
        services.append(redact(result))
    return services


def normalize_account(account: dict[str, Any], fallback_label: str) -> dict[str, Any]:
    status = str(account.get("status") or ("ok" if account.get("available", True) else "limited")).lower()
    windows = account.get("windows") or []
    reset_bank = account.get("reset_bank", account.get("banked_resets", account.get("reset_credits")))
    return redact({
        "label": account.get("label") or account.get("id") or fallback_label,
        "provider": account.get("provider"),
        "plan": account.get("plan"),
        "status": status,
        "available": status in {"ok", "available", "active", "connected"},
        "windows": windows,
        "reset_bank": reset_bank,
        "reset_bank_text": account.get("reset_bank_text"),
        "message": account.get("message"),
    })


def collect_command_json_tracker(tracker: dict[str, Any]) -> dict[str, Any]:
    proc = run_command(tracker["command"], timeout=int(tracker.get("timeout_seconds", 45)), cwd=tracker.get("cwd"))
    if proc.returncode != 0:
        return {"id": tracker["id"], "label": tracker.get("label", tracker["id"]), "status": "error", "accounts": [], "error": redact(proc.stderr or proc.stdout)}
    data = json.loads(proc.stdout)
    account_items = get_path(data, tracker.get("accounts_path", "accounts"), [])
    if isinstance(account_items, dict):
        account_items = list(account_items.values())
    accounts = []
    for raw_account in account_items or []:
        account: dict[str, Any] = {
            "id": get_path(raw_account, tracker.get("account_id_path", "id")),
            "label": get_path(raw_account, tracker.get("account_label_path", "label")),
            "provider": tracker.get("provider"),
            "plan": get_path(raw_account, tracker.get("plan_path", "plan")),
            "status": get_path(raw_account, tracker.get("status_path", "status"), "unknown"),
            "reset_bank": get_path(raw_account, tracker.get("reset_bank_path", "reset_bank")),
            "reset_bank_text": get_path(raw_account, tracker.get("reset_bank_text_path", "reset_bank_text")),
            "windows": [],
        }
        windows_path = tracker.get("windows_path", "windows")
        windows = get_path(raw_account, windows_path, []) if windows_path else []
        for window in windows or []:
            account["windows"].append({
                "key": get_path(window, tracker.get("window_key_path", "key")),
                "label": get_path(window, tracker.get("window_label_path", "label")),
                "used_percent": get_path(window, tracker.get("used_percent_path", "used_percent")),
                "remaining_percent": get_path(window, tracker.get("remaining_percent_path", "remaining_percent")),
                "reset_text": get_path(window, tracker.get("reset_text_path", "reset_text")),
            })
        accounts.append(normalize_account(account, tracker.get("label", tracker["id"])))
    return summarize_tracker(tracker, accounts)


def collect_command_regex_tracker(tracker: dict[str, Any]) -> dict[str, Any]:
    proc = run_command(tracker["command"], timeout=int(tracker.get("timeout_seconds", 45)), cwd=tracker.get("cwd"))
    text = proc.stdout or ""
    if proc.returncode != 0 and tracker.get("require_zero_exit", True):
        return {"id": tracker["id"], "label": tracker.get("label", tracker["id"]), "status": "error", "accounts": [], "error": redact(proc.stderr or proc.stdout)}

    account_re = re.compile(tracker.get("account_start_regex", r"(?m)^Account:\s*(?P<label>.+)$"), re.MULTILINE)
    matches = list(account_re.finditer(text))
    blocks: list[tuple[dict[str, str], str]] = []
    if not matches:
        blocks.append(({"label": tracker.get("label", tracker["id"])}, text))
    else:
        for index, match in enumerate(matches):
            start = match.start()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            blocks.append((match.groupdict(), text[start:end]))

    accounts = []
    for groups, block in blocks:
        status = tracker.get("default_status", "ok")
        if tracker.get("status_regex"):
            status_match = re.search(tracker["status_regex"], block, re.I | re.M)
            if status_match:
                status = status_match.groupdict().get("status") or status_match.group(1)
        plan = None
        if tracker.get("plan_regex"):
            plan_match = re.search(tracker["plan_regex"], block, re.I | re.M)
            if plan_match:
                plan = plan_match.groupdict().get("plan") or plan_match.group(1)
        reset_bank = None
        reset_bank_text = None
        if tracker.get("reset_bank_regex"):
            reset_match = re.search(tracker["reset_bank_regex"], block, re.I | re.M)
            if reset_match:
                gd = reset_match.groupdict()
                reset_bank_text = gd.get("reset_bank_text") or reset_match.group(0).strip()
                raw_bank = gd.get("reset_bank") or gd.get("banked_resets")
                if raw_bank is not None:
                    try:
                        reset_bank = int(raw_bank)
                    except ValueError:
                        reset_bank = raw_bank
        windows = []
        for pattern in tracker.get("window_patterns", []):
            regex = re.compile(pattern["regex"], re.I | re.M)
            window_match = regex.search(block)
            if not window_match:
                continue
            gd = window_match.groupdict()
            windows.append({
                "key": pattern.get("key"),
                "label": pattern.get("label") or gd.get("label"),
                "used_percent": int(gd["used"]) if gd.get("used") else None,
                "remaining_percent": int(gd["remaining"]) if gd.get("remaining") else None,
                "reset_text": gd.get("reset") or pattern.get("reset_text"),
            })
        accounts.append(normalize_account({
            "label": groups.get("label") or groups.get("id") or tracker.get("label", tracker["id"]),
            "provider": tracker.get("provider"),
            "plan": plan,
            "status": status,
            "reset_bank": reset_bank,
            "reset_bank_text": reset_bank_text,
            "windows": windows,
        }, tracker.get("label", tracker["id"])))
    return summarize_tracker(tracker, accounts)


def summarize_tracker(tracker: dict[str, Any], accounts: list[dict[str, Any]]) -> dict[str, Any]:
    available = sum(1 for account in accounts if account.get("available"))
    limited = sum(1 for account in accounts if account.get("status") in {"limited", "reauth", "error"})
    return redact({
        "id": tracker.get("id"),
        "label": tracker.get("label") or tracker.get("id"),
        "provider": tracker.get("provider"),
        "status": "ok" if available else ("limited" if accounts else "unknown"),
        "available_accounts": available,
        "attention_accounts": limited,
        "accounts": accounts,
    })


def collect_subscriptions(config: dict[str, Any]) -> list[dict[str, Any]]:
    trackers = []
    for tracker in config.get("subscription_trackers", []):
        if not tracker.get("enabled", True):
            continue
        try:
            tracker_type = tracker.get("type")
            if tracker_type == "command_json":
                trackers.append(collect_command_json_tracker(tracker))
            elif tracker_type == "command_regex":
                trackers.append(collect_command_regex_tracker(tracker))
            elif tracker_type == "manual":
                trackers.append(summarize_tracker(tracker, [normalize_account(account, tracker.get("label", tracker["id"])) for account in tracker.get("accounts", [])]))
            else:
                trackers.append({"id": tracker.get("id"), "label": tracker.get("label", tracker.get("id")), "status": "error", "accounts": [], "error": f"unknown tracker type: {tracker_type}"})
        except Exception as exc:  # noqa: BLE001
            trackers.append({"id": tracker.get("id"), "label": tracker.get("label", tracker.get("id")), "status": "error", "accounts": [], "error": str(redact(exc))[:240]})
    return trackers


def collect_cron(config: dict[str, Any]) -> list[dict[str, Any]]:
    cron_cfg = config.get("cron", {})
    if not cron_cfg.get("enabled", True):
        return []
    try:
        command = cron_cfg.get("command", "hermes cron list --all")
        proc = run_command(command, timeout=int(cron_cfg.get("timeout_seconds", 60)))
        if proc.returncode != 0:
            return [{"id": "cron-error", "state": "error", "name": "Cron недоступен", "schedule_display": "ошибка", "error": redact(proc.stderr or proc.stdout)}]
        return parse_cron_list(proc.stdout, tz(config))
    except Exception as exc:  # noqa: BLE001
        return [{"id": "cron-error", "state": "error", "name": "Cron недоступен", "schedule_display": "ошибка", "error": str(redact(exc))[:240]}]


_CRON_MODEL_CATALOG_CACHE: dict[str, tuple[float, list[str]]] = {}
_CRON_MODEL_CATALOG_LOCK = threading.Lock()
_CRON_MODEL_CATALOG_TTL_SECONDS = 900


def encode_cron_model_choice(provider: str, model: str) -> str:
    return json.dumps([provider, model], ensure_ascii=False, separators=(",", ":"))


def _model_parts(model: Any, provider: Any = None) -> tuple[str, str]:
    provider_text = str(provider or "").strip()
    model_text = str(model or "").strip()
    if provider_text and model_text.startswith(provider_text + "/"):
        model_text = model_text[len(provider_text) + 1:]
    elif not provider_text and "/" in model_text:
        possible_provider, remainder = model_text.split("/", 1)
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", possible_provider):
            provider_text, model_text = possible_provider, remainder
    return provider_text, model_text


def cron_job_records(*, strict: bool = False) -> list[dict[str, Any]]:
    """Read cron metadata from Hermes storage; never return prompts or raw records."""
    try:
        if strict:
            from cron.jobs import _current_cron_store

            raw = json.loads(_current_cron_store().jobs_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("jobs"), list):
                raise ValueError("Invalid raw cron store schema")
            records = raw["jobs"]
            identifiers = [record.get("id") if isinstance(record, dict) else None for record in records]
            if any(not isinstance(identifier, str) or not identifier for identifier in identifiers):
                raise ValueError("Invalid cron record ID")
            if len(set(identifiers)) != len(identifiers):
                raise ValueError("Duplicate cron record ID")
            return records
        from cron.jobs import load_jobs

        records = load_jobs()
        if isinstance(records, dict):
            records = records.get("jobs", [])
        return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []
    except Exception as exc:
        if strict:
            raise ValueError("Не удалось прочитать хранилище задач.") from exc
        return []


def enrich_cron_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stored = {str(record.get("id")): record for record in cron_job_records() if record.get("id")}
    for job in jobs:
        record = stored.get(str(job.get("id")), {})
        for key in ("model", "provider", "reasoning_effort"):
            if key in record:
                job[key] = record.get(key)
        provider, model = str(job.get("provider") or ""), str(job.get("model") or "")
        job["provider"] = provider or None
        job["model"] = model or None
        job["model_choice"] = encode_cron_model_choice(provider, model) if provider or model else "default"
        reasoning = str(job.get("reasoning_effort") or "").lower()
        job["reasoning_index"] = CRON_REASONING_LEVELS.index(reasoning) + 1 if reasoning in CRON_REASONING_LEVELS else 0
    return jobs


def _provider_model_ids(provider: str) -> list[str]:
    now = time.monotonic()
    with _CRON_MODEL_CATALOG_LOCK:
        cached = _CRON_MODEL_CATALOG_CACHE.get(provider)
        if cached and now - cached[0] < _CRON_MODEL_CATALOG_TTL_SECONDS:
            return list(cached[1])
    models: list[str] = []
    try:
        from hermes_cli.models import curated_models_for_provider

        for item in curated_models_for_provider(provider):
            model = item[0] if isinstance(item, (tuple, list)) and item else item
            if isinstance(model, dict):
                model = model.get("id") or model.get("model")
            if isinstance(model, str) and model.strip() and len(model) <= 300 and not any(ch in model for ch in "\r\n\x00"):
                models.append(model.strip())
    except Exception:
        pass
    models = list(dict.fromkeys(models))[:250]
    with _CRON_MODEL_CATALOG_LOCK:
        _CRON_MODEL_CATALOG_CACHE[provider] = (now, models)
    return models


def cron_model_options(config: dict[str, Any], jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hermes_config: dict[str, Any] = {}
    try:
        hermes_config = load_config(hermes_home() / "config.yaml")
    except Exception:
        pass
    model_cfg = hermes_config.get("model") if isinstance(hermes_config.get("model"), dict) else {}
    default_provider, default_model = _model_parts(model_cfg.get("default"), model_cfg.get("provider"))
    providers = {default_provider} if default_provider else set()
    current_models: list[tuple[str, str]] = []
    for job in jobs:
        provider, model = str(job.get("provider") or ""), str(job.get("model") or "")
        if provider:
            providers.add(provider)
        if provider or model:
            current_models.append((provider, model))
    if default_provider and default_model:
        current_models.append((default_provider, default_model))

    by_pair: dict[tuple[str, str], dict[str, str]] = {}
    for provider in sorted(p for p in providers if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", p)):
        for model in _provider_model_ids(provider):
            by_pair[(provider, model)] = {"value": encode_cron_model_choice(provider, model), "provider": provider, "model": model, "label": f"{provider} · {model}"}
    for provider, model in current_models:
        if len(provider) <= 64 and len(model) <= 300:
            by_pair.setdefault((provider, model), {"value": encode_cron_model_choice(provider, model), "provider": provider, "model": model, "label": f"{provider or 'Провайдер по умолчанию'} · {model or 'Модель по умолчанию'}"})
    default_label = f"По умолчанию · {default_provider}/{default_model}" if default_provider and default_model else "По умолчанию"
    return [{"value": "default", "provider": None, "model": None, "label": default_label}, *[by_pair[key] for key in sorted(by_pair)]]


def edit_cron_job(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if not config.get("cron", {}).get("enabled", True):
        raise ValueError("Cron management is disabled")
    job_id = str(payload.get("job_id") or "")
    if not CRON_ID_PATTERN.fullmatch(job_id):
        raise ValueError("Invalid cron job id")
    listed = run_command(["hermes", "cron", "list", "--all"], timeout=60)
    if listed.returncode != 0:
        raise ValueError(str(redact(listed.stderr or listed.stdout or "Could not read cron jobs"))[:240])
    jobs = enrich_cron_jobs(parse_cron_list(listed.stdout, tz(config)))
    job = next((item for item in jobs if item.get("id") == job_id), None)
    if not job or job.get("state") not in CRON_EDITABLE_STATES:
        raise ValueError("Cron job was not found or is not editable")

    model_choice = payload.get("model_choice")
    options = {item["value"]: item for item in cron_model_options(config, jobs)}
    if not isinstance(model_choice, str) or model_choice not in options:
        raise ValueError("Selected model is not available")
    effort = payload.get("reasoning_effort")
    if not isinstance(effort, str) or effort not in ("", *CRON_REASONING_LEVELS):
        raise ValueError("Invalid reasoning effort")

    command = ["hermes", "cron", "edit", job_id]
    selected = options[model_choice]
    command.extend(["--model", str(selected.get("model") or ""), "--provider", str(selected.get("provider") or "")])
    command.extend(["--reasoning-effort", effort])
    target_index = payload.get("periodicity_index")
    new_schedule = str(job.get("schedule") or "")
    periodicity_changed = False
    if target_index is not None:
        if type(target_index) is not int or not 0 <= target_index < len(CRON_PERIODICITY_MINUTES):
            raise ValueError("Invalid cron periodicity")
        if job.get("schedule_kind") == "once":
            raise ValueError("One-time cron jobs do not have a periodicity")
        if target_index != job.get("periodicity_index"):
            minutes = CRON_PERIODICITY_MINUTES[target_index]
            new_schedule = f"every {minutes}m"
            periodicity_changed = True
            command.extend(["--schedule", new_schedule])

    edited = run_command(command, timeout=60)
    if edited.returncode != 0:
        raise ValueError(str(redact(edited.stderr or edited.stdout or "Could not update cron job"))[:240])

    saved = next((record for record in cron_job_records() if str(record.get("id")) == job_id), None)
    if saved is None:
        raise ValueError("Cron edit returned success but the saved job could not be verified")
    saved_provider, saved_model = str(saved.get("provider") or ""), str(saved.get("model") or "")
    if (saved_provider, saved_model) != (str(selected.get("provider") or ""), str(selected.get("model") or "")):
        raise ValueError("Cron edit returned success but the saved model could not be verified")
    if str(saved.get("reasoning_effort") or "") != effort:
        raise ValueError("Cron edit returned success but the saved reasoning could not be verified")
    if periodicity_changed and schedule_period_minutes(saved.get("schedule")) != CRON_PERIODICITY_MINUTES[target_index]:
        raise ValueError("Cron edit returned success but the saved periodicity could not be verified")
    return {"ok": True, "job_id": job_id, "schedule": new_schedule, "message": "Параметры задачи сохранены"}


def remove_cron_job_by_id(job_id: str) -> bool:
    """Exact-ID transaction using Hermes' atomic writer and shared store lock.

    Do not call the CLI/name resolver. Both process locks must be acquired: the
    runtime's general-purpose lock can degrade to local-only on flock failure.
    Retain output/notepad history; only the scheduling record is removed.
    """
    from cron.jobs import (
        _acquire_flock, _jobs_file_lock, _jobs_lock_file,
        _release_flock, _save_jobs_unlocked,
    )

    if not _jobs_file_lock.acquire(timeout=15):
        raise ValueError("Хранилище задач занято. Обновите список перед повторной попыткой.")
    try:
        with open(_jobs_lock_file(), "a+", encoding="utf-8") as lock_file:
            if _acquire_flock(lock_file, 15) is not True:
                raise ValueError("Не удалось безопасно заблокировать хранилище задач.")
            try:
                records = cron_job_records(strict=True)
                if not any(record["id"] == job_id for record in records):
                    return False
                remaining = [record for record in records if record["id"] != job_id]
                _save_jobs_unlocked(remaining, removed_ids={job_id})
                return True
            finally:
                _release_flock(lock_file)
    finally:
        _jobs_file_lock.release()


def delete_cron_job(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Remove one explicitly confirmed job and verify persisted absence.

    Authorization and serialization are enforced by the HTTP handler. Repeated
    requests for an already absent ID are a verified no-op, never a name lookup.
    """
    if not (config.get("cron", {}) or {}).get("enabled", True):
        raise ValueError("Cron отключён.")
    if set(payload) - {"action", "init_data", "job_id", "confirmed"}:
        raise ValueError("Недопустимые параметры удаления.")
    job_id = payload.get("job_id")
    if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{12}", job_id):
        raise ValueError("Неверный идентификатор задачи.")
    if payload.get("confirmed") is not True:
        raise ValueError("Подтвердите удаление задачи.")
    try:
        removed = remove_cron_job_by_id(job_id)
    except (OSError, TimeoutError, ImportError, AttributeError) as exc:
        raise ValueError("Результат удаления пока неизвестен. Обновите список задач перед повторной попыткой.") from exc
    try:
        remaining = cron_job_records(strict=True)
    except ValueError as exc:
        raise ValueError("Не удалось проверить удаление. Обновите список задач перед повторной попыткой.") from exc
    if any(record["id"] == job_id for record in remaining):
        raise ValueError("Удаление не подтверждено: задача всё ещё в хранилище.")
    if not removed:
        return {"ok": True, "job_id": job_id, "already_removed": True, "message": "Задача уже удалена."}
    return {"ok": True, "job_id": job_id, "message": "Задача удалена."}


def collect_server_status(config: dict[str, Any]) -> dict[str, Any]:
    settings = config.get("server", {}) or {}
    if not settings.get("enabled", True):
        return {"enabled": False}
    try:
        usage = shutil.disk_usage(str(settings.get("disk_path") or "/"))
        usable = usage.used + usage.free
        if usage.total <= 0 or usage.used < 0 or usage.free < 0 or usable <= 0:
            raise ValueError("Invalid filesystem counters")
        percent = round(100 * usage.used / usable, 1) if usable else 0
        return {"enabled": True, "disk": {
            "total_bytes": usage.total, "used_bytes": usage.used,
            "available_bytes": usage.free, "reserved_bytes": max(0, usage.total - usable),
            "used_percent": percent,
            "status": "critical" if percent >= 95 else ("warning" if percent >= 85 else "ok"),
        }}
    except (OSError, ValueError):
        return {"enabled": True, "disk": {"status": "unknown", "message": "Не удалось получить данные диска."}}


def vpn_port_open(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def collect_vpn_status(config: dict[str, Any]) -> dict[str, Any]:
    settings = config.get("vpn", {}) or {}
    if not settings.get("enabled", False):
        return {"enabled": False, "profiles": []}
    profiles = []
    states: dict[str, str] = {}
    for profile in settings.get("profiles", [])[:8]:
        item = {"id": str(profile.get("id") or ""), "label": str(profile.get("label") or "Happ"), "status": "unknown"}
        units, ports = profile.get("services", []), profile.get("probe_ports", [])
        if (not units or len(units) > 8 or any(not isinstance(unit, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", unit) or unit.startswith("-") for unit in units)
                or not ports or len(ports) > 8 or any(type(port) is not int or not 1 <= port <= 65535 for port in ports)):
            profiles.append(item)
            continue
        try:
            for unit in units:
                if unit not in states:
                    result = run_command(["systemctl", "show", "--property=ActiveState,SubState", "--", unit], timeout=3)
                    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
                    states[unit] = values.get("ActiveState", "unknown") if result.returncode == 0 else "unknown"
            service_states = [states[unit] for unit in units]
            active = all(state == "active" for state in service_states)
            listening = all(vpn_port_open(port) for port in ports) if active else False
            item.update({"service_active": active, "listener_ready": listening,
                         "status": "running" if active and listening else ("degraded" if active else ("unknown" if "unknown" in service_states else "stopped"))})
        except (OSError, subprocess.TimeoutExpired):
            pass
        profiles.append(item)
    return {"enabled": True, "profiles": profiles}


def vpn_copy_allowed(config: dict[str, Any], user_id: int) -> bool:
    settings = config.get("vpn", {}) or {}
    allowed = settings.get("link_allowed_users", [])
    return bool(settings.get("enabled", False) and isinstance(allowed, list)
                and str(user_id) in {str(value) for value in allowed if type(value) is int or (isinstance(value, str) and value.isdigit())})


def read_vpn_link(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Read only a configured private import file; callers must authorize first."""
    import stat

    settings = config.get("vpn", {}) or {}
    profile_id = payload.get("profile_id")
    if (not settings.get("enabled", False) or not isinstance(profile_id, str)
            or set(payload) - {"action", "init_data", "profile_id"}):
        raise ValueError("Недопустимый запрос ссылки Happ.")
    profile = next((item for item in settings.get("profiles", [])[:8] if item.get("id") == profile_id), None)
    if not profile:
        raise ValueError("Профиль Happ недоступен.")
    try:
        path = pathlib.Path(profile["link_file"]).expanduser()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 8192 or info.st_mode & 0o077
                    or info.st_uid not in {0, os.geteuid()}):
                raise ValueError("Invalid private file")
            raw = os.read(fd, 8193)
        finally:
            os.close(fd)
        if len(raw) > 8192:
            raise ValueError("Import too large")
        uri = raw.decode("utf-8").strip()
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in uri):
            raise ValueError("Invalid URI characters")
        parsed = urllib.parse.urlsplit(uri)
        if (parsed.scheme != "vless" or not parsed.hostname or not parsed.port or parsed.password is not None
                or not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", parsed.username or "")):
            raise ValueError("Invalid import URI")
        return {"ok": True, "profile_id": profile_id, "uri": uri}
    except Exception:
        raise ValueError("Не удалось безопасно прочитать ссылку Happ.") from None


def collect_status(config: dict[str, Any]) -> dict[str, Any]:
    zone = tz(config)
    checked_at = now_iso()
    cron_jobs = enrich_cron_jobs(collect_cron(config))
    return redact({
        "ok": True,
        "checked_at": checked_at,
        "checked_at_display": format_local(checked_at, zone),
        "time_zone": zone.key,
        "server": collect_server_status(config),
        "vpn": collect_vpn_status(config),
        "services": collect_services(config),
        "subscriptions": collect_subscriptions(config),
        "cron_jobs": cron_jobs,
        "cron_models": cron_model_options(config, cron_jobs) if cron_jobs else [],
    })


# ---------------------------------------------------------------------------
# Mini App UI server

MINIAPP_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Hermes Operator Status</title><style>
:root{color-scheme:dark;font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:#08111f;color:#eef6ff}*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at top,#155e7566,#08111f 46%,#020617)}.wrap{max-width:640px;margin:0 auto;padding:18px 14px 42px}.top{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}.eyebrow{font-size:12px;color:#93c5fd}.title{margin:2px 0 0;font-size:28px;letter-spacing:-.04em}.muted{color:#94a3b8}.small{font-size:12px}.card{border:1px solid #ffffff18;border-radius:24px;background:#0b1220d9;padding:15px;margin-top:12px;box-shadow:0 12px 34px #0005}.card h2{font-size:17px;margin:0 0 12px}.row{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center;padding:11px 0;border-top:1px solid #ffffff10}.row:first-of-type{border-top:0}.name{font-weight:750}.caption{font-size:12px;color:#94a3b8;margin-top:3px}.pill{font-size:11px;text-transform:uppercase;border-radius:999px;border:1px solid #ffffff24;padding:5px 8px;background:#ffffff10;white-space:nowrap}.ok{color:#86efac}.error,.reauth{color:#fca5a5}.limited,.unknown{color:#fde68a}.missing{color:#cbd5e1}.account{display:block;padding:13px 0;border-top:1px solid #ffffff10}.account:first-of-type{border-top:0}.account-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.account-meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:5px}.account-meta .caption{margin-top:0}.status-badge{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.03em;border-radius:999px;border:1px solid #ffffff24;padding:3px 7px;background:#ffffff0d;white-space:nowrap}.bars{display:grid;gap:8px;margin-top:10px}.barline{display:grid;gap:6px;padding:9px;border-radius:14px;background:#ffffff08;border:1px solid #ffffff0d}.limit-head{display:flex;justify-content:space-between;gap:10px;align-items:baseline;flex-wrap:wrap}.limit-title{font-size:12px;font-weight:800;color:#e2e8f0}.limit-stats{font-size:12px;color:#94a3b8}.limit-stats strong{color:#e2e8f0}.limit-note{margin-top:0}.bar{height:9px;background:#ffffff14;border-radius:999px;overflow:hidden}.fill{height:100%;border-radius:999px;background:linear-gradient(90deg,#22c55e,#eab308,#ef4444)}button{border:0;border-radius:999px;padding:10px 14px;background:#38bdf8;color:#04111f;font-weight:800}button:disabled{opacity:.72;cursor:progress}.hidden{display:none}pre{white-space:pre-wrap;word-break:break-word;color:#fecaca}.footer{margin-top:14px;text-align:center;color:#64748b;font-size:11px}</style><style>
.cron-job{border-top:1px solid #ffffff12;padding-top:10px;margin-top:10px}.cron-job:first-of-type{border-top:0;margin-top:0;padding-top:0}.cron-editor{margin-top:6px;border:1px solid #ffffff16;border-radius:14px;background:#ffffff06}.cron-editor summary{padding:10px 12px;cursor:pointer;font-size:13px;font-weight:700;color:#7dd3fc}.cron-fields{display:grid;gap:12px;padding:0 12px 12px}.cron-field{display:grid;gap:6px;font-size:13px}.cron-field select{width:100%;min-height:40px;border-radius:10px;border:1px solid #ffffff22;background:#111c2d;color:#eef6ff;padding:8px}.cron-range{width:100%;accent-color:#22d3ee;touch-action:pan-y}.cron-range:disabled{opacity:.42}.cron-value{color:#a5f3fc;font-weight:750}.cron-hint{font-size:11px;color:#94a3b8;line-height:1.4}.cron-save{justify-self:start;border:0;border-radius:10px;background:#0e7490;color:#ecfeff;font-weight:750;padding:10px 14px}.cron-save:disabled{opacity:.55}.cron-message{font-size:12px;color:#86efac;min-height:16px}.cron-message.error{color:#fca5a5}
.cron-delete{background:#541c27;color:#fecdd3;border:1px solid #f8717166;min-height:44px}.cron-delete:hover{background:#702030}.cron-delete-action{margin-top:10px}.cron-delete-dialog{color:#eef6ff;background:#102137;border:1px solid #ffffff24;border-radius:18px;padding:22px;width:calc(100% - 32px);max-width:420px;max-height:calc(100dvh - 32px);overflow:auto;box-shadow:0 20px 80px #0009}.cron-delete-dialog::backdrop{background:#020617bd;backdrop-filter:blur(3px)}.cron-delete-dialog h2{margin:0 0 14px;font-size:21px;line-height:1.3}.cron-delete-dialog p{overflow-wrap:anywhere;line-height:1.5}.cron-delete-actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:18px}.cron-delete-actions button{flex:1;min-height:44px}.cron-delete-dialog button:disabled{opacity:.55;cursor:wait}
#server-status .bar i{display:block;height:100%;background:#38bdf8;border-radius:inherit}.server-disk-head,.vpn-head{display:flex;justify-content:space-between;align-items:center;gap:10px}.server-disk-head{font-weight:700;margin:8px 0}.server-disk-details{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:9px;font-size:13px}.server-warning{margin-top:10px;padding:9px 11px;border-radius:10px;background:#ef44441a;color:#fca5a5;font-size:13px}.vpn-profile{padding:13px 0;border-bottom:1px solid #ffffff12}.vpn-profile:last-of-type{border:0}.vpn-head .name{overflow-wrap:anywhere}.vpn-profile button{margin-top:10px;min-height:44px}.vpn-dialog{color:#eef6ff;background:#102137;border:1px solid #ffffff24;border-radius:18px;padding:20px;width:calc(100% - 32px);max-width:460px;max-height:calc(100dvh - 32px);overflow:auto;box-shadow:0 20px 80px #0009}.vpn-dialog::backdrop{background:#020617bd;backdrop-filter:blur(3px)}.vpn-dialog h2{margin:0 0 10px;font-size:22px}.vpn-dialog textarea{display:block;width:100%;min-height:116px;resize:vertical;background:#08111f;color:#eef6ff;border:1px solid #ffffff30;border-radius:10px;padding:10px;font:12px/1.5 ui-monospace,monospace;overflow-wrap:anywhere;margin:8px 0}.vpn-dialog textarea[hidden]{display:none}.vpn-dialog-actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}.vpn-dialog-actions button{flex:1;min-height:44px}.vpn-dialog button:disabled{opacity:.5}.vpn-note{line-height:1.5;margin-top:10px}
</style></head>
<body><main class="wrap"><div class="top"><div><div id="verified" class="eyebrow">Проверяю Telegram…</div><h1 class="title">Hermes Operator</h1><div class="muted small">Сервисы, подписки, cron</div></div><button id="refresh" type="button">Обновить</button></div><p id="cron-feedback" class="muted small" role="status" hidden></p><div id="error" class="card hidden"><b>Ошибка доступа</b><pre id="errtext"></pre></div><div id="content"><div class="card muted">Загружаю…</div></div><div class="footer">Данные без токенов и секретов</div></main>
<dialog id="cron-delete-dialog" class="cron-delete-dialog" aria-labelledby="cron-delete-title" aria-describedby="cron-delete-name">
<h2 id="cron-delete-title">Вы уверены что хотите удалить</h2>
<p id="cron-delete-name"></p><p class="muted small">Будущие запуски этой задачи будут отменены. Восстановить её из интерфейса нельзя.</p>
<div id="cron-delete-error" class="cron-message error" role="alert"></div>
<div class="cron-delete-actions"><button id="cron-delete-cancel" type="button" autofocus>Отмена</button><button id="cron-delete-confirm" class="cron-delete" type="button">Удалить</button></div>
</dialog>
<dialog id="vpn-link-dialog" class="vpn-dialog" aria-labelledby="vpn-link-title">
<h2 id="vpn-link-title">Подключение Happ</h2><div id="vpn-link-profile" class="muted small"></div>
<div id="vpn-link-message" class="cron-message" role="status"></div>
<textarea id="vpn-link-value" aria-label="Ссылка подключения Happ" readonly spellcheck="false" autocomplete="off" autocapitalize="off" hidden></textarea>
<p class="small vpn-note">Happ → «+» → импорт из буфера обмена. Ссылка даёт доступ к вашему VPN — не пересылайте её посторонним.</p>
<div class="vpn-dialog-actions"><button id="vpn-link-close" type="button" autofocus>Закрыть</button><button id="vpn-link-copy" type="button" disabled>Скопировать ссылку</button></div>
</dialog>
<script src="https://telegram.org/js/telegram-web-app.js"></script><script>
const tg=window.Telegram&&window.Telegram.WebApp; tg&&tg.ready&&tg.ready(); tg&&tg.expand&&tg.expand();
const $=(id)=>document.getElementById(id); const esc=(v)=>String(v??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
function tone(s){s=String(s||'unknown').toLowerCase();return ['ok','active','available','connected'].includes(s)?'ok':(['error','reauth','failed'].includes(s)?'error':(['limited','unknown'].includes(s)?'limited':s))}
function statusText(s){return ({ok:'доступ есть',available:'доступ есть',active:'active',connected:'connected',limited:'частично',missing:'нет токена',error:'ошибка',reauth:'re-auth',unknown:'unknown'})[s]||s||'unknown'}
function serviceRow(s){return `<div class="row"><div><div class="name">${esc(s.label)}</div><div class="caption">${esc(s.caption||'')}</div></div><span class="pill ${tone(s.status)}">${esc(statusText(s.status))}</span></div>`}
function percent(v){const n=Number(v);return Number.isFinite(n)?`${Math.round(n)}%`:`${esc(v??'?')}%`}
function resetBank(a){if(a.reset_bank_text)return `<span class="caption">${esc(a.reset_bank_text)}</span>`; if(a.reset_bank!==undefined&&a.reset_bank!==null&&a.reset_bank!==''){const bank=Number(a.reset_bank);const label=Number.isFinite(bank)&&bank===0?'нет':esc(a.reset_bank);return `<span class="caption">reset bank: ${label}</span>`} return ''}
function accountCard(a){const windows=(a.windows||[]).map(w=>{const used=Math.max(0,Math.min(100,Number(w.used_percent)||0));const usedText=percent(w.used_percent);const remainingText=percent(w.remaining_percent);const label=esc(w.label||w.key||'лимит');return `<div class="barline"><div class="limit-head"><span class="limit-title">${label}</span><span class="limit-stats"><strong>${remainingText}</strong> осталось · ${usedText} использовано</span></div><div class="bar" aria-label="${label}: использовано ${usedText}, осталось ${remainingText}"><div class="fill" style="width:${used}%"></div></div>${w.reset_text?`<div class="caption limit-note">${esc(w.reset_text)}</div>`:''}</div>`}).join('');return `<div class="account"><div class="account-head"><div><div class="name">${esc(a.label)}</div><div class="account-meta">${a.plan?`<span class="caption">plan ${esc(a.plan)}</span>`:(a.message?`<span class="caption">${esc(a.message)}</span>`:'')}${resetBank(a)}<span class="status-badge ${tone(a.status)}">${esc(statusText(a.status))}</span></div></div></div>${windows?`<div class="bars">${windows}</div>`:''}</div>`}
function subscriptionCard(s){return `<section class="card"><h2>${esc(s.label||s.id)}</h2><div class="caption">${esc(s.provider||'subscription')} · доступно: ${esc(s.available_accounts??0)} · требует внимания: ${esc(s.attention_accounts??0)}</div>${s.error?`<pre>${esc(s.error)}</pre>`:''}${(s.accounts||[]).map(accountCard).join('')||'<div class="muted">Нет данных.</div>'}</section>`}
function bytesLabel(value){const n=Number(value);if(!Number.isFinite(n))return 'нет данных';const gib=n/1073741824;return gib>=1?`${gib.toFixed(1)} ГиБ`:`${(n/1048576).toFixed(0)} МиБ`}
function serverCard(server){
 if(!server||server.enabled===false)return '';
 const disk=server.disk||{};
 if(disk.status==='unknown')return '<section class="card" id="server-status"><h2>Сервер</h2><div class="muted">Данные диска недоступны.</div></section>';
 const used=Math.max(0,Math.min(100,Number(disk.used_percent)||0));
 return `<section class="card" id="server-status"><h2>Сервер</h2><div class="server-disk-head"><span>Заполненность диска</span><span>${used.toFixed(1)}%</span></div><div class="bar" role="progressbar" aria-label="Заполненность диска" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${used}"><i style="width:${used}%;${disk.status==='critical'?'background:#f87171':disk.status==='warning'?'background:#fbbf24':''}"></i></div><div class="server-disk-details"><span>Занято: <b>${bytesLabel(disk.used_bytes)}</b></span><span>Доступно: <b>${bytesLabel(disk.available_bytes)}</b></span><span>Всего: ${bytesLabel(disk.total_bytes)}</span></div>${disk.reserved_bytes>0?`<div class="caption">Резерв файловой системы: ${bytesLabel(disk.reserved_bytes)}; в доступное место не входит.</div>`:''}${['critical','warning'].includes(disk.status)?'<div class="server-warning">Мало свободного места. Очистка автоматически не выполняется.</div>':''}</section>`;
}
function vpnCard(vpn,canCopy){
 if(!vpn||!vpn.enabled)return '';
 const labels={running:'Работает',stopped:'Остановлен',degraded:'Нужна проверка',unknown:'Нет данных'};
 return `<section class="card" id="vpn-status"><h2>VPN для Happ</h2>${(vpn.profiles||[]).map(profile=>`<div class="vpn-profile"><div class="vpn-head"><span class="name">${esc(profile.label)}</span><span class="pill ${profile.status==='running'?'ok':profile.status==='stopped'?'error':'limited'}">${labels[profile.status]||labels.unknown}</span></div>${canCopy?`<button type="button" data-vpn-link data-profile-id="${esc(profile.id)}">Ссылка для Happ</button>`:''}</div>`).join('')||'<div class="muted">Профили не настроены.</div>'}<div class="caption vpn-note">Статус сервиса и локальных портов. Подключение с телефона здесь не проверяется.</div></section>`;
}
let vpnLinkOperation=null;
function closeVpnLink(){
 vpnLinkOperation=null;
 const dialog=$('vpn-link-dialog');if(dialog.open)dialog.close();
 $('vpn-link-value').value='';$('vpn-link-value').hidden=true;$('vpn-link-profile').textContent='';$('vpn-link-message').textContent='';$('vpn-link-copy').disabled=true;
}
async function openVpnLink(button){
 const identity=(tg&&tg.initData)||'';
 const profile=(window.__vpnProfiles||[]).find(item=>item.id===button.dataset.profileId);
 if(!profile||!identity||identity!==statusLoadIdentity){load(true);return}
 closeVpnLink();const operation={identity,profileId:profile.id};vpnLinkOperation=operation;
 const isCurrent=()=>vpnLinkOperation===operation&&identity===((tg&&tg.initData)||'')&&identity===statusLoadIdentity;
 $('vpn-link-profile').textContent=profile.label;$('vpn-link-message').textContent='Получаю защищённую ссылку…';$('vpn-link-dialog').showModal();$('vpn-link-close').focus();
 try{
  const response=await fetch('/api/status',{method:'POST',cache:'no-store',headers:{'content-type':'application/json','cache-control':'no-cache'},body:JSON.stringify({action:'vpn_link',init_data:identity,profile_id:profile.id})});
  const data=await response.json();if(!isCurrent())return;
  if(!response.ok||!data.uri)throw new Error('Ссылка недоступна. Закройте окно и обновите данные.');
  $('vpn-link-value').value=data.uri;$('vpn-link-value').hidden=false;$('vpn-link-copy').disabled=false;$('vpn-link-message').textContent='Нажмите «Скопировать ссылку» и откройте Happ.';
 }catch(error){if(isCurrent())$('vpn-link-message').textContent='Не удалось получить ссылку. Закройте окно и обновите данные.'}
}
async function copyVpnLink(){
 const operation=vpnLinkOperation;
 const isCurrent=()=>operation&&vpnLinkOperation===operation&&operation.identity===((tg&&tg.initData)||'')&&operation.identity===statusLoadIdentity;
 if(!isCurrent()||$('vpn-link-copy').disabled||!$('vpn-link-value').value)return;
 const value=$('vpn-link-value').value;$('vpn-link-copy').disabled=true;
 try{
  let copied=false;
  if(navigator.clipboard&&navigator.clipboard.writeText){try{await navigator.clipboard.writeText(value);copied=true}catch(error){}}
  if(!isCurrent())return;
  if(!copied){$('vpn-link-value').focus();$('vpn-link-value').select();try{copied=document.execCommand('copy')}catch(error){}}
  if(isCurrent())$('vpn-link-message').textContent=copied?'Ссылка скопирована. Теперь импортируйте её в Happ.':'Автокопирование недоступно. Скопируйте выделенный текст вручную.';
 }finally{if(isCurrent())$('vpn-link-copy').disabled=false}
}
$('vpn-link-close').addEventListener('click',closeVpnLink);
$('vpn-link-copy').addEventListener('click',copyVpnLink);
$('vpn-link-dialog').addEventListener('cancel',event=>{event.preventDefault();closeVpnLink()});
$('vpn-link-dialog').addEventListener('close',()=>{if(!$('vpn-link-dialog').open)closeVpnLink()});
document.addEventListener('click',event=>{const button=event.target.closest('[data-vpn-link]');if(button)openVpnLink(button)});
const CRON_REASONING_VALUES=['','none','minimal','low','medium','high','xhigh','max','ultra'];
const CRON_REASONING_LABELS=['По умолчанию','none','minimal','low','medium','high','xhigh','max','ultra'];
const CRON_PERIOD_MINUTES=[1,2,5,10,15,30,60,120,360,720,1440,2880,4320,7200,10080,20160,43200];
function cronPeriodLabel(index){const n=CRON_PERIOD_MINUTES[Math.max(0,Math.min(CRON_PERIOD_MINUTES.length-1,Number(index)||0))];if(n<60)return `${n} мин`;if(n<1440)return `${n/60} ч`;return `${n/1440} дн`}
function cronRow(j,models,canEdit){
 const selected=j.model_choice||'default';
 const modelOptions=(models||[]).map(m=>`<option value="${esc(m.value)}" ${m.value===selected?'selected':''}>${esc(m.label)}</option>`).join('');
 const editable=canEdit&&['active','paused','scheduled'].includes(j.state);
 const deleteAction=canEdit?`<div class="cron-delete-action"><button class="cron-delete" type="button" data-cron-delete data-job-id="${esc(j.id)}">Удалить задачу</button></div>`:'';
 const summary=`<div class="row"><div><div class="name">${esc(j.name||j.id)}</div><div class="caption">${esc(j.schedule_display||j.schedule||'без расписания')}</div>${j.next_run_display?`<div class="caption">следующий запуск: ${esc(j.next_run_display)}</div>`:''}${j.script?`<div class="caption">script: ${esc(j.script)}</div>`:''}</div><span class="pill ${tone(j.state)}">${esc(j.state||'active')}</span></div>`;
 if(!editable)return `<div class="cron-job">${summary}${deleteAction}</div>`;
 const periodIndex=Number.isInteger(j.periodicity_index)?j.periodicity_index:10;
 const reasoningIndex=Number.isInteger(j.reasoning_index)?j.reasoning_index:0;
 const once=j.schedule_kind==='once';
 const warning=j.schedule_kind==='cron'?'Изменение заменит календарное расписание фиксированным интервалом; время суток и календарная привязка изменятся.':'';
 return `<div class="cron-job">${summary}<details class="cron-editor" data-cron-editor data-job-id="${esc(j.id)}"><summary>Настроить модель, рассуждение и периодичность</summary><div class="cron-fields"><label class="cron-field">Модель<select data-model>${modelOptions}</select></label><label class="cron-field"><span>Рассуждение: <strong class="cron-value" data-reasoning-value>${CRON_REASONING_LABELS[reasoningIndex]}</strong></span><input class="cron-range" type="range" min="0" max="8" step="1" value="${reasoningIndex}" data-reasoning aria-label="Уровень рассуждения"><span class="cron-hint">По умолчанию → none → minimal → low → medium → high → xhigh → max → ultra</span></label><label class="cron-field"><span>Периодичность: <strong class="cron-value" data-periodicity-value>${once?'не применяется':(j.schedule_minutes?cronPeriodLabel(periodIndex):'не меняется')}</strong></span><input class="cron-range" type="range" min="0" max="16" step="1" value="${periodIndex}" data-periodicity data-period-initial="${j.periodicity_index??''}" ${once?'disabled':''} aria-label="Периодичность cron-задачи"><span class="cron-hint">От 1 минуты до 30 дней. Текущий график: ${esc(j.schedule_display||j.schedule||'не определён')}. ${warning}</span></label><div class="cron-message" data-cron-message aria-live="polite"></div><button class="cron-save" type="button" data-cron-save>Сохранить</button></div></details>${deleteAction}</div>`;
}
let cronDeletePending=null;
function closeCronDelete(){
 const modal=$('cron-delete-dialog');cronDeletePending=null;
 if(modal.open)modal.close();
 $('cron-delete-name').textContent='';$('cron-delete-error').textContent='';
 $('cron-delete-confirm').disabled=false;$('cron-delete-cancel').disabled=false;$('cron-delete-confirm').textContent='Удалить';
}
function openCronDelete(button){
 if(cronDeletePending&&cronDeletePending.busy)return;
 const identity=(tg&&tg.initData)||'';
 const job=(window.__cronJobs||[]).find(item=>item.id===button.dataset.jobId);
 if(!job||!identity||identity!==statusLoadIdentity){load(true);return}
 closeCronDelete();cronDeletePending={jobId:job.id,identity,busy:false};
 $('cron-delete-name').textContent=job.name||job.id;
 $('cron-delete-dialog').showModal();$('cron-delete-cancel').focus();
}
async function confirmCronDelete(){
 const pending=cronDeletePending;if(!pending||pending.busy)return;
 const isCurrent=()=>cronDeletePending===pending&&pending.identity===((tg&&tg.initData)||'')&&pending.identity===statusLoadIdentity;
 if(!isCurrent()){closeCronDelete();load(true);return}
 pending.busy=true;$('cron-delete-confirm').disabled=true;$('cron-delete-cancel').disabled=true;$('cron-delete-confirm').textContent='Удаляю…';$('cron-delete-error').textContent='';
 try{
  const response=await fetch('/api/status',{method:'POST',cache:'no-store',headers:{'content-type':'application/json','cache-control':'no-cache'},body:JSON.stringify({action:'cron_delete',init_data:pending.identity,job_id:pending.jobId,confirmed:true})});
  const data=await response.json();if(!isCurrent())return;
  if(!response.ok)throw new Error((data.detail&&data.detail.message)||'Не удалось удалить задачу');
  const refreshed=await load(true);if(!isCurrent()||!refreshed)return;
  if((window.__cronJobs||[]).some(job=>job.id===pending.jobId))throw new Error('Обновите список: удалённая задача ещё отображается.');
  closeCronDelete();$('cron-feedback').hidden=false;$('cron-feedback').textContent=data.message||'Задача удалена.';
 }catch(error){if(isCurrent()&&cronDeletePending===pending)$('cron-delete-error').textContent=String(error.message||error)}
 finally{if(cronDeletePending===pending){pending.busy=false;$('cron-delete-confirm').disabled=false;$('cron-delete-cancel').disabled=false;$('cron-delete-confirm').textContent='Удалить'}}
}
$('cron-delete-cancel').addEventListener('click',closeCronDelete);
$('cron-delete-confirm').addEventListener('click',confirmCronDelete);
$('cron-delete-dialog').addEventListener('cancel',event=>{if(cronDeletePending&&cronDeletePending.busy)event.preventDefault()});
$('cron-delete-dialog').addEventListener('close',()=>{if(!$('cron-delete-dialog').open)closeCronDelete()});
function saveCronEditor(button){
 const identity=(tg&&tg.initData)||'';
 const isCurrent=()=>identity===((tg&&tg.initData)||'')&&identity===statusLoadIdentity;
 const editor=button.closest('[data-cron-editor]');const message=editor.querySelector('[data-cron-message]');
 const job=(window.__cronJobs||[]).find(item=>item.id===editor.dataset.jobId);
 if(!job||!isCurrent()){message.classList.add('error');message.textContent='Задача устарела. Обновите страницу.';return}
 const period=editor.querySelector('[data-periodicity]');const reasoning=editor.querySelector('[data-reasoning]');
 const dirty=period&&!period.disabled&&period.dataset.dirty==='1';const initial=period&&period.dataset.periodInitial!==''?Number(period.dataset.periodInitial):null;const nextIndex=dirty?Number(period.value):null;
 if(dirty&&(initial===null||nextIndex!==initial)&&job.schedule_kind==='cron'&&!window.confirm('Заменить календарное расписание фиксированным интервалом?'))return;
 button.disabled=true;button.textContent='Сохраняю…';message.classList.remove('error');message.textContent='';
 fetch('/api/status',{method:'POST',cache:'no-store',headers:{'content-type':'application/json','cache-control':'no-cache'},body:JSON.stringify({action:'cron_update',init_data:identity,job_id:job.id,model_choice:editor.querySelector('[data-model]').value,reasoning_effort:CRON_REASONING_VALUES[Number(reasoning.value)],periodicity_index:nextIndex})})
 .then(async r=>{
  const data=await r.json();if(!isCurrent())return;
  if(!r.ok)throw new Error((data.detail&&data.detail.message)||'Не удалось сохранить задачу');
  const refreshed=await load(true);if(!refreshed||!isCurrent())return;
  const updated=[...document.querySelectorAll('[data-cron-editor]')].find(item=>item.dataset.jobId===job.id);
  if(updated){updated.open=true;updated.querySelector('[data-cron-message]').textContent=data.message||'Параметры задачи сохранены'}
 }).catch(error=>{if(isCurrent()){message.classList.add('error');message.textContent=String(error.message||error)}})
 .finally(()=>{if(button.isConnected&&isCurrent()){button.disabled=false;button.textContent='Сохранить'}})
}
document.addEventListener('input',event=>{if(event.target.matches('[data-reasoning]'))event.target.closest('.cron-field').querySelector('[data-reasoning-value]').textContent=CRON_REASONING_LABELS[Number(event.target.value)];if(event.target.matches('[data-periodicity]')){event.target.dataset.dirty='1';event.target.closest('.cron-field').querySelector('[data-periodicity-value]').textContent=cronPeriodLabel(event.target.value)}});
document.addEventListener('click',event=>{const button=event.target.closest('[data-cron-save]');if(button)saveCronEditor(button);const remove=event.target.closest('[data-cron-delete]');if(remove)openCronDelete(remove)});
function render(data){$('verified').textContent='Telegram verified · '+esc(data.checked_at_display||data.checked_at); const subscriptions=data.subscriptions||[]; const cron=data.cron_jobs||[];window.__cronJobs=cron;window.__vpnProfiles=(data.vpn&&data.vpn.profiles)||[]; $('content').innerHTML=`${serverCard(data.server)}${vpnCard(data.vpn,data.vpn_can_copy===true)}<section class="card"><h2>Подключённые сервисы</h2>${(data.services||[]).map(serviceRow).join('')||'<div class="muted">Сервисы не настроены.</div>'}</section>${subscriptions.length?subscriptions.map(subscriptionCard).join(''):'<section class="card"><h2>Подписки</h2><div class="muted">Трекеры подписок не настроены.</div></section>'}<section class="card"><h2>Cron-задачи</h2>${cron.length?cron.map(j=>cronRow(j,data.cron_models||[],data.cron_can_edit===true)).join(''):'<div class="muted">Активных задач нет.</div>'}</section>`;}
function setRefreshState(active){const b=$('refresh'); if(!b)return; b.disabled=!!active; b.textContent=active?'Обновляю…':'Обновить'; b.setAttribute('aria-busy',active?'true':'false')}
let statusLoadSequence=0;let statusLoadIdentity=null;
async function load(refresh=false){
 const sequence=++statusLoadSequence;
 const identity=(tg&&tg.initData)||'';
 const isCurrent=()=>sequence===statusLoadSequence&&((tg&&tg.initData)||'')===identity;
 const clearPrivate=()=>{if(typeof closeVpnLink==='function')closeVpnLink();window.__vpnProfiles=[];if(typeof closeCronDelete==='function')closeCronDelete();$('cron-feedback').textContent='';$('cron-feedback').hidden=true;window.__cronJobs=[];$('content').innerHTML='';if($('connected-content'))$('connected-content').innerHTML=''};
 if(identity!==statusLoadIdentity){clearPrivate();statusLoadIdentity=identity}
 $('error').classList.add('hidden');
 if(refresh)$('verified').textContent='Обновляю данные…';
 setRefreshState(refresh);
 try{
  if(!identity){clearPrivate();$('verified').textContent='Mini App unavailable';$('content').innerHTML='<div class="card"><b>Открой через кнопку Mini App в Telegram-боте.</b></div>';return false}
  const r=await fetch('/api/status?refresh='+Date.now(),{method:'POST',cache:'no-store',headers:{'content-type':'application/json','cache-control':'no-cache'},body:JSON.stringify({init_data:identity,refresh:!!refresh,requested_at:Date.now()})});
  const data=await r.json();
  if(!isCurrent())return false;
  if(!r.ok)throw new Error((data.detail&&data.detail.message)||JSON.stringify(data));
  if(data.vpn_can_copy!==true)closeVpnLink();
  render(data);return true;
 }catch(e){
  if(!isCurrent())return false;
  clearPrivate();$('verified').textContent='Не авторизовано';$('error').classList.remove('hidden');$('errtext').textContent=String(e);return false;
 }finally{if(isCurrent())setRefreshState(false)}
}
$('refresh').addEventListener('click',(event)=>{event.preventDefault(); load(true)}); load(false);
</script></body></html>"""


@dataclass
class ServerContext:
    config: dict[str, Any]


def make_handler(context: ServerContext) -> type[BaseHTTPRequestHandler]:
    cron_mutation_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "HermesOperatorMiniApp/0.1"

        def _send_json(self, payload: dict[str, Any], status: int = 200, *, private_link: bool = False) -> None:
            data = json.dumps(payload if private_link else redact(payload), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_html(self, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            if path in {"/", "/miniapp", "/miniapp/"}:
                self._send_html(MINIAPP_HTML)
            elif path == "/health":
                self._send_json({"ok": True, "version": "0.1"})
            else:
                self._send_json({"detail": "not found"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            if path != "/api/status":
                self._send_json({"detail": "not found"}, 404)
                return
            try:
                length = int(self.headers.get("content-length", "0"))
                if length <= 0 or length > 16384:
                    self._send_json({"detail": {"message": "Invalid request size"}}, 413)
                    return
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(body, dict):
                    self._send_json({"detail": {"message": "Request body must be an object"}}, 400)
                    return
                action = str(body.get("action") or "status")
                if action not in {"status", "cron_update", "cron_delete", "vpn_link"}:
                    self._send_json({"detail": {"message": "Unknown action"}}, 400)
                    return
                telegram = context.config.get("telegram", {})
                cron_config = context.config.get("cron", {}) or {}
                mutation = action in {"cron_update", "cron_delete"}
                action_allowed_users = cron_config.get("edit_allowed_users") if mutation else None
                user = authenticate_telegram_request(str(body.get("init_data", "")), telegram, allowed_users=action_allowed_users, require_allowlist=mutation or action == "vpn_link")
                if action == "vpn_link":
                    if not vpn_copy_allowed(context.config, int(user["id"])):
                        raise PermissionError("Нет доступа к ссылке Happ.")
                    self._send_json(read_vpn_link(context.config, body), private_link=True)
                    return
                if mutation:
                    with cron_mutation_lock:
                        result = (delete_cron_job if action == "cron_delete" else edit_cron_job)(context.config, body)
                    result["user"] = user
                    self._send_json(result)
                    return
                payload = collect_status(context.config)
                payload["user"] = user
                payload["vpn_can_copy"] = vpn_copy_allowed(context.config, int(user["id"]))
                editors = cron_config.get("edit_allowed_users")
                if editors is None:
                    editors = telegram.get("allowed_users", [])
                payload["cron_can_edit"] = int(user["id"]) in [int(value) for value in editors]
                self._send_json(payload)
            except PermissionError as exc:
                self._send_json({"detail": {"message": str(exc)}}, 403)
            except ValueError as exc:
                self._send_json({"detail": {"message": str(redact(exc))}}, 400)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"detail": {"message": str(redact(exc))}}, 500)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            # Keep access logs small and secret-free.
            sys.stderr.write("miniapp %s - %s\n" % (self.address_string(), format % args))

    return Handler


def serve(config_path: pathlib.Path, host: str, port: int) -> None:
    config = load_config(config_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(ServerContext(config)))
    print(f"Hermes Operator Mini App listening on http://{host}:{port}/miniapp", flush=True)
    httpd.serve_forever()


# ---------------------------------------------------------------------------
# Wizard and CLI


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or (default or "")


def init_config(path: pathlib.Path) -> None:
    print("Hermes Operator Mini App setup")
    time_zone = ask("Timezone for display", DEFAULT_TIME_ZONE)
    bot_token_env = ask("Telegram bot token environment variable", "TELEGRAM_BOT_TOKEN")
    allowed_raw = ask("Allowed Telegram user IDs, comma-separated", "")
    allowed = [int(part.strip()) for part in allowed_raw.split(",") if part.strip().isdigit()]
    cron_edit_raw = ask("Cron editor Telegram user IDs (blank = disable editing)", "")
    cron_edit_allowed = [int(part.strip()) for part in cron_edit_raw.split(",") if part.strip().isdigit()]

    services = []
    if ask("Track GitHub via gh auth status? yes/no", "yes").lower().startswith("y"):
        services.append({"id": "github", "label": "GitHub", "type": "github_cli", "enabled": True, "access": ["repo/auth"]})
    if ask("Track Tailscale via tailscale status? yes/no", "yes").lower().startswith("y"):
        services.append({"id": "tailscale", "label": "Tailscale", "type": "tailscale", "enabled": True, "access": ["device/network"]})
    if ask("Track Google Drive OAuth token/scopes? yes/no", "no").lower().startswith("y"):
        services.append({
            "id": "google_drive",
            "label": "Google Drive",
            "type": "oauth_token",
            "enabled": True,
            "token_paths": ["~/.hermes/google_token.json"],
            "required_scopes": ["/auth/drive"],
            "access_scopes": {"/auth/drive": "Drive API"},
            "access": ["OAuth"],
        })
    if ask("Track YouTube OAuth token/scopes? yes/no", "no").lower().startswith("y"):
        services.append({
            "id": "youtube",
            "label": "YouTube",
            "type": "oauth_token",
            "enabled": True,
            "token_paths": ["~/.hermes/youtube_token.json", "~/.hermes/youtube_analytics_token.json", "~/.hermes/youtube_uqi_token.json"],
            "access_scopes": {"youtube.upload": "upload", "youtube.force-ssl": "manage", "yt-analytics": "analytics"},
            "access": ["OAuth"],
        })

    if ask("Discover additional service candidates from local Hermes config/tokens? yes/no", "yes").lower().startswith("y"):
        existing_ids = {str(item.get("id")) for item in services}
        for candidate in discover_service_candidates():
            config = candidate.get("config") if isinstance(candidate.get("config"), dict) else None
            if not config or str(config.get("id")) in existing_ids:
                continue
            prompt = f"Add discovered service {candidate.get('label')} ({candidate.get('reason')})? yes/no"
            if ask(prompt, "no").lower().startswith("y"):
                services.append(config)
                existing_ids.add(str(config.get("id")))

    trackers: list[dict[str, Any]] = []
    while ask("Add a subscription tracker? yes/no", "no").lower().startswith("y"):
        tracker_id = ask("Tracker id, e.g. openai-codex or qwen-plan")
        label = ask("Display label", tracker_id)
        provider = ask("Provider name", tracker_id.split("-")[0])
        tracker_type = ask("Tracker type: command_json, command_regex, manual", "command_json")
        if tracker_type == "manual":
            trackers.append({"id": tracker_id, "label": label, "provider": provider, "type": "manual", "enabled": True, "accounts": []})
        elif tracker_type == "command_regex":
            print("Regex trackers parse command output. Edit window_patterns in the config after setup.")
            trackers.append({
                "id": tracker_id,
                "label": label,
                "provider": provider,
                "type": "command_regex",
                "enabled": True,
                "command": ask("Command to run"),
                "account_start_regex": r"(?m)^Account:\\s*(?P<label>.+)$",
                "default_status": "ok",
                "window_patterns": [
                    {"key": "primary", "label": "Основной лимит", "regex": r"(?P<label>[^:]+):\\s*(?P<used>\\d+)%\\s*used.*?(?P<remaining>\\d+)%\\s*remaining"}
                ],
            })
        else:
            trackers.append({
                "id": tracker_id,
                "label": label,
                "provider": provider,
                "type": "command_json",
                "enabled": True,
                "command": ask("Command that prints JSON"),
                "accounts_path": "accounts",
                "account_label_path": "label",
                "status_path": "status",
                "plan_path": "plan",
                "windows_path": "windows",
                "window_label_path": "label",
                "used_percent_path": "used_percent",
                "remaining_percent_path": "remaining_percent",
                "reset_text_path": "reset_text",
            })

    config = {
        "time_zone": time_zone,
        "telegram": {"bot_token_env": bot_token_env, "allowed_users": allowed, "max_age_seconds": 86400},
        "services": services,
        "subscription_trackers": trackers,
        "cron": {"enabled": True, "command": "hermes cron list --all", "timeout_seconds": 60, "edit_allowed_users": cron_edit_allowed},
    }
    write_config(config, path)
    print(f"Wrote {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes operator Telegram Mini App status panel")
    parser.add_argument("--config", type=pathlib.Path, default=DEFAULT_CONFIG_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-config")
    discover_parser = sub.add_parser("discover-services")
    discover_parser.add_argument("--pretty", action="store_true")
    discover_parser.add_argument("--hermes-home", type=pathlib.Path, default=None)
    discover_parser.add_argument("--hermes-config", type=pathlib.Path, default=None)
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("--pretty", action="store_true")
    serve_parser = sub.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=9120)
    args = parser.parse_args(argv)

    if args.command == "init-config":
        init_config(args.config)
        return 0
    if args.command == "discover-services":
        payload = {"service_candidates": discover_service_candidates(args.hermes_home, args.hermes_config)}
        print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
        return 0
    if args.command == "collect":
        payload = collect_status(load_config(args.config))
        print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
        return 0
    if args.command == "serve":
        serve(args.config, args.host, args.port)
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
