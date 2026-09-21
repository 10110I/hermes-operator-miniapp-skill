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


def parse_cron_list(output: str, zone: ZoneInfo) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    first = re.compile(r"^\s*([0-9a-f]{12})\s+\[([^\]]+)\]")
    field = re.compile(r"^\s*([A-Za-z ]+):\s+(.*)$")
    for raw in output.splitlines():
        first_match = first.match(raw)
        if first_match:
            if current and current.get("state") == "active":
                jobs.append(current)
            current = {"id": first_match.group(1), "state": first_match.group(2), "name": "", "schedule": "", "next_run": None, "script": None}
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
    if current and current.get("state") == "active":
        jobs.append(current)
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


def collect_status(config: dict[str, Any]) -> dict[str, Any]:
    zone = tz(config)
    checked_at = now_iso()
    return redact({
        "ok": True,
        "checked_at": checked_at,
        "checked_at_display": format_local(checked_at, zone),
        "time_zone": zone.key,
        "services": collect_services(config),
        "subscriptions": collect_subscriptions(config),
        "cron_jobs": collect_cron(config),
    })


# ---------------------------------------------------------------------------
# Mini App UI server

MINIAPP_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Hermes Operator Status</title><style>
:root{color-scheme:dark;font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:#08111f;color:#eef6ff}*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at top,#155e7566,#08111f 46%,#020617)}.wrap{max-width:640px;margin:0 auto;padding:18px 14px 42px}.top{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}.eyebrow{font-size:12px;color:#93c5fd}.title{margin:2px 0 0;font-size:28px;letter-spacing:-.04em}.muted{color:#94a3b8}.small{font-size:12px}.card{border:1px solid #ffffff18;border-radius:24px;background:#0b1220d9;padding:15px;margin-top:12px;box-shadow:0 12px 34px #0005}.card h2{font-size:17px;margin:0 0 12px}.row{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center;padding:11px 0;border-top:1px solid #ffffff10}.row:first-of-type{border-top:0}.name{font-weight:750}.caption{font-size:12px;color:#94a3b8;margin-top:3px}.pill{font-size:11px;text-transform:uppercase;border-radius:999px;border:1px solid #ffffff24;padding:5px 8px;background:#ffffff10;white-space:nowrap}.ok{color:#86efac}.error,.reauth{color:#fca5a5}.limited,.unknown{color:#fde68a}.missing{color:#cbd5e1}.account{display:block;padding:13px 0;border-top:1px solid #ffffff10}.account:first-of-type{border-top:0}.account-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.account-meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:5px}.account-meta .caption{margin-top:0}.status-badge{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.03em;border-radius:999px;border:1px solid #ffffff24;padding:3px 7px;background:#ffffff0d;white-space:nowrap}.bars{display:grid;gap:8px;margin-top:10px}.barline{display:grid;gap:6px;padding:9px;border-radius:14px;background:#ffffff08;border:1px solid #ffffff0d}.limit-head{display:flex;justify-content:space-between;gap:10px;align-items:baseline;flex-wrap:wrap}.limit-title{font-size:12px;font-weight:800;color:#e2e8f0}.limit-stats{font-size:12px;color:#94a3b8}.limit-stats strong{color:#e2e8f0}.limit-note{margin-top:0}.bar{height:9px;background:#ffffff14;border-radius:999px;overflow:hidden}.fill{height:100%;border-radius:999px;background:linear-gradient(90deg,#22c55e,#eab308,#ef4444)}button{border:0;border-radius:999px;padding:10px 14px;background:#38bdf8;color:#04111f;font-weight:800}button:disabled{opacity:.72;cursor:progress}.hidden{display:none}pre{white-space:pre-wrap;word-break:break-word;color:#fecaca}.footer{margin-top:14px;text-align:center;color:#64748b;font-size:11px}</style></head>
<body><main class="wrap"><div class="top"><div><div id="verified" class="eyebrow">Проверяю Telegram…</div><h1 class="title">Hermes Operator</h1><div class="muted small">Сервисы, подписки, cron</div></div><button id="refresh" type="button">Обновить</button></div><div id="error" class="card hidden"><b>Ошибка доступа</b><pre id="errtext"></pre></div><div id="content"><div class="card muted">Загружаю…</div></div><div class="footer">Данные без токенов и секретов</div></main>
<script src="https://telegram.org/js/telegram-web-app.js"></script><script>
const tg=window.Telegram&&window.Telegram.WebApp; tg&&tg.ready&&tg.ready(); tg&&tg.expand&&tg.expand();
const $=(id)=>document.getElementById(id); const esc=(v)=>String(v??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
function tone(s){s=String(s||'unknown').toLowerCase();return ['ok','active','available','connected'].includes(s)?'ok':(['error','reauth','failed'].includes(s)?'error':(['limited','unknown'].includes(s)?'limited':s))}
function statusText(s){return ({ok:'доступ есть',available:'доступ есть',active:'active',connected:'connected',limited:'частично',missing:'нет токена',error:'ошибка',reauth:'re-auth',unknown:'unknown'})[s]||s||'unknown'}
function serviceRow(s){return `<div class="row"><div><div class="name">${esc(s.label)}</div><div class="caption">${esc(s.caption||'')}</div></div><span class="pill ${tone(s.status)}">${esc(statusText(s.status))}</span></div>`}
function percent(v){const n=Number(v);return Number.isFinite(n)?`${Math.round(n)}%`:`${esc(v??'?')}%`}
function resetBank(a){if(a.reset_bank_text)return `<span class="caption">${esc(a.reset_bank_text)}</span>`; if(a.reset_bank!==undefined&&a.reset_bank!==null&&a.reset_bank!=='')return `<span class="caption">reset bank: ${esc(a.reset_bank)}</span>`; return ''}
function accountCard(a){const windows=(a.windows||[]).map(w=>{const used=Math.max(0,Math.min(100,Number(w.used_percent)||0));const usedText=percent(w.used_percent);const remainingText=percent(w.remaining_percent);const label=esc(w.label||w.key||'лимит');return `<div class="barline"><div class="limit-head"><span class="limit-title">${label}</span><span class="limit-stats"><strong>${remainingText}</strong> осталось · ${usedText} использовано</span></div><div class="bar" aria-label="${label}: использовано ${usedText}, осталось ${remainingText}"><div class="fill" style="width:${used}%"></div></div>${w.reset_text?`<div class="caption limit-note">${esc(w.reset_text)}</div>`:''}</div>`}).join('');return `<div class="account"><div class="account-head"><div><div class="name">${esc(a.label)}</div><div class="account-meta">${a.plan?`<span class="caption">plan ${esc(a.plan)}</span>`:(a.message?`<span class="caption">${esc(a.message)}</span>`:'')}${resetBank(a)}<span class="status-badge ${tone(a.status)}">${esc(statusText(a.status))}</span></div></div></div>${windows?`<div class="bars">${windows}</div>`:''}</div>`}
function subscriptionCard(s){return `<section class="card"><h2>${esc(s.label||s.id)}</h2><div class="caption">${esc(s.provider||'subscription')} · доступно: ${esc(s.available_accounts??0)} · требует внимания: ${esc(s.attention_accounts??0)}</div>${s.error?`<pre>${esc(s.error)}</pre>`:''}${(s.accounts||[]).map(accountCard).join('')||'<div class="muted">Нет данных.</div>'}</section>`}
function cronRow(j){return `<div class="row"><div><div class="name">${esc(j.name||j.id)}</div><div class="caption">${esc(j.schedule_display||j.schedule||'без расписания')}</div>${j.next_run_display?`<div class="caption">следующий запуск: ${esc(j.next_run_display)}</div>`:''}${j.script?`<div class="caption">script: ${esc(j.script)}</div>`:''}</div><span class="pill ${tone(j.state)}">${esc(j.state||'active')}</span></div>`}
function render(data){$('verified').textContent='Telegram verified · '+esc(data.checked_at_display||data.checked_at); const subscriptions=data.subscriptions||[]; const cron=data.cron_jobs||[]; $('content').innerHTML=`<section class="card"><h2>Подключённые сервисы</h2>${(data.services||[]).map(serviceRow).join('')||'<div class="muted">Сервисы не настроены.</div>'}</section>${subscriptions.length?subscriptions.map(subscriptionCard).join(''):'<section class="card"><h2>Подписки</h2><div class="muted">Трекеры подписок не настроены.</div></section>'}<section class="card"><h2>Активные cron-задачи</h2>${cron.length?cron.map(cronRow).join(''):'<div class="muted">Активных задач нет.</div>'}</section>`;}
function setRefreshState(active){const b=$('refresh'); if(!b)return; b.disabled=!!active; b.textContent=active?'Обновляю…':'Обновить'; b.setAttribute('aria-busy',active?'true':'false')}
async function load(refresh=false){$('error').classList.add('hidden'); if(refresh){$('verified').textContent='Обновляю данные…'} setRefreshState(refresh); try{if(!tg||!tg.initData){$('verified').textContent='Mini App unavailable';$('content').innerHTML='<div class="card"><b>Открой через кнопку Mini App в Telegram-боте.</b></div>';return} const r=await fetch('/api/status?refresh='+Date.now(),{method:'POST',cache:'no-store',headers:{'content-type':'application/json','cache-control':'no-cache'},body:JSON.stringify({init_data:tg.initData,refresh:!!refresh,requested_at:Date.now()})}); const data=await r.json(); if(!r.ok) throw new Error((data.detail&&data.detail.message)||JSON.stringify(data)); render(data)}catch(e){$('verified').textContent='Не авторизовано';$('error').classList.remove('hidden');$('errtext').textContent=String(e)}finally{setRefreshState(false)}}
$('refresh').addEventListener('click',(event)=>{event.preventDefault(); load(true)}); load(false);
</script></body></html>"""


@dataclass
class ServerContext:
    config: dict[str, Any]


def make_handler(context: ServerContext) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "HermesOperatorMiniApp/0.1"

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            data = json.dumps(redact(payload), ensure_ascii=False).encode("utf-8")
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
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                telegram = context.config.get("telegram", {})
                token_env = telegram.get("bot_token_env", "TELEGRAM_BOT_TOKEN")
                bot_token = os.environ.get(str(token_env), "")
                if not bot_token:
                    raise PermissionError(f"bot token env var is not configured: {token_env}")
                allowed = [int(v) for v in telegram.get("allowed_users", [])]
                user = validate_init_data(str(body.get("init_data", "")), bot_token=bot_token, allowed_users=allowed, max_age_seconds=int(telegram.get("max_age_seconds", 86400)))
                payload = collect_status(context.config)
                payload["user"] = user
                self._send_json(payload)
            except PermissionError as exc:
                self._send_json({"detail": {"message": str(exc)}}, 403)
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
        "cron": {"enabled": True, "command": "hermes cron list --all", "timeout_seconds": 60},
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
