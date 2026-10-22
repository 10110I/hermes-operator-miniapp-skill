"""Behavioral cron security tests; also runnable against an installed script."""
import hashlib
import hmac
import http.client
import importlib.util
import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

import pytest

SCRIPT = Path(os.environ.get("OPERATOR_TEST_SCRIPT", Path(__file__).resolve().parents[1] / "scripts/operator_miniapp_status.py"))
spec = importlib.util.spec_from_file_location("cron_editor_under_test", SCRIPT)
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)
TOKEN = "123456:TEST_TOKEN"


def signed(user_id):
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id})}
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.mark.parametrize("editor_config,user_id,expected", [
    ({}, 101, 200),
    ({}, 202, 403),
    ({"edit_allowed_users": []}, 101, 403),
    ({"edit_allowed_users": [101]}, 101, 200),
    ({"edit_allowed_users": [202]}, 101, 403),
    ({"edit_allowed_users": [202]}, 202, 200),
])
def test_editor_allowlist_policy(monkeypatch, editor_config, user_id, expected):
    monkeypatch.setenv("EDITOR_TEST_TOKEN", TOKEN)
    config = {"telegram": {"bot_token_env": "EDITOR_TEST_TOKEN", "allowed_users": [101]}, "cron": editor_config}
    calls = []
    monkeypatch.setattr(app, "edit_cron_job", lambda *args: calls.append(args) or {"ok": True})
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(app.ServerContext(config)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("POST", "/api/status", json.dumps({"action": "cron_update", "init_data": signed(user_id)}), {"Content-Type": "application/json"})
        response = conn.getresponse()
        response.read()
        assert response.status == expected
        assert response.getheader("Cache-Control") == "no-store"
        assert len(calls) == (1 if expected == 200 else 0)
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_all_interval_slider_values_match_runtime_storage():
    for minutes in app.CRON_PERIODICITY_MINUTES:
        assert app.schedule_period_minutes({"kind": "interval", "minutes": minutes, "display": f"every {minutes}m"}) == minutes


@pytest.mark.parametrize("schedule", ["once in 30m", "once at 2026-12-01 09:00"])
def test_one_time_jobs_never_get_an_interval(schedule):
    assert app.schedule_kind(schedule) == "once"
    assert app.schedule_period_minutes(schedule) is None


@pytest.mark.parametrize("entry", ["catalog-model", {"id": "catalog-model"}, ("catalog-model", "Description")])
def test_model_picker_accepts_runtime_catalog_records(monkeypatch, entry):
    from types import SimpleNamespace
    monkeypatch.setitem(sys.modules, "hermes_cli.models", SimpleNamespace(curated_models_for_provider=lambda provider: [entry]))
    monkeypatch.setattr(app, "_CRON_MODEL_CATALOG_CACHE", {})
    monkeypatch.setattr(app, "load_config", lambda path: {"model": {"provider": "openai-codex", "default": "default-model"}})
    options = app.cron_model_options({}, [])
    assert any(item["model"] == "catalog-model" for item in options)


@pytest.mark.parametrize("model,provider", [("model-only", None), (None, "provider-only"), ("vendor/model", None)])
def test_reasoning_edit_preserves_partial_model_pin(monkeypatch, model, provider):
    job_id = "123456789abc"
    record = {"id": job_id, "model": model, "provider": provider, "reasoning_effort": "high"}
    monkeypatch.setattr(app, "cron_job_records", lambda: [record])
    jobs = app.enrich_cron_jobs([{"id": job_id, "state": "active", "schedule_kind": "interval"}])
    choice = app.encode_cron_model_choice(provider or "", model or "")
    assert jobs[0]["model_choice"] == choice
    monkeypatch.setattr(app, "collect_cron", lambda config: jobs)
    monkeypatch.setattr(app, "load_config", lambda path: {})
    monkeypatch.setattr(app, "_provider_model_ids", lambda provider: [])
    assert any(option["value"] == choice for option in app.cron_model_options({}, jobs))
    import subprocess
    calls = []
    listing = f"  {job_id} [active] Тестовая задача\n    Schedule: every 30m\n"
    monkeypatch.setattr(app, "run_command", lambda command, **kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0, listing if "list" in command else "ok", ""))
    result = app.edit_cron_job({}, {"job_id": job_id, "model_choice": choice, "reasoning_effort": "high"})
    assert result["ok"]
    edited = calls[-1]
    assert edited[edited.index("--model") + 1] == (model or "")
    assert edited[edited.index("--provider") + 1] == (provider or "")


def test_late_status_response_cannot_restore_an_old_identity():
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("Node required for deterministic JS race test")
    html = app.MINIAPP_HTML
    start = html.index("let statusLoadSequence") if "let statusLoadSequence" in html else html.index("async function load(")
    end = html.index("$('refresh').addEventListener", start)
    loader = html[start:end]
    harness = r"""
const tg={initData:'signed-A'};
const elements=new Map();
const $=id=>{if(!elements.has(id))elements.set(id,{innerHTML:'private',textContent:'',classList:{add(){},remove(){}},addEventListener(){}});return elements.get(id)};
global.window={__cronJobs:[{id:'private'}]};
let renders=0;
function render(data){renders++;window.__cronJobs=data.cron_jobs}
function setRefreshState(){}
const pending=[];
function fetch(){return new Promise(resolve=>pending.push(resolve))}
"""
    ending = r"""
(async()=>{
const a=load(true);tg.initData='signed-B';const b=load(true);
pending[1]({ok:false,json:async()=>({detail:{message:'denied'}})});await b;
pending[0]({ok:true,json:async()=>({cron_jobs:[{id:'private'}]})});await a;
if(renders!==0||window.__cronJobs.length!==0)throw new Error('late authorized response restored private data');
console.log('stale identity response ignored');
})().catch(e=>{console.error(e);process.exit(1)});
"""
    result = subprocess.run(["node"], input=harness + loader + ending, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_javascript_reasoning_labels_are_defined():
    assert "const CRON_REASONING_LABELS=" in app.MINIAPP_HTML


@pytest.mark.parametrize("invalid_index", [True, -1, 17, "6"])
def test_invalid_interval_never_mutates(monkeypatch, invalid_index):
    calls = []
    monkeypatch.setattr(app, "run_command", lambda command, **kw: calls.append(command) or app.subprocess.CompletedProcess(command, 0, "123456789abc [active]\n    Schedule: every 30m\n", ""))
    monkeypatch.setattr(app, "cron_job_records", lambda: [])
    monkeypatch.setattr(app, "cron_model_options", lambda *a: [{"value": "default", "provider": None, "model": None}])
    with pytest.raises(ValueError, match="periodicity"):
        app.edit_cron_job({"cron": {"enabled": True}}, {"job_id": "123456789abc", "model_choice": "default", "reasoning_effort": "", "periodicity_index": invalid_index})
    assert len(calls) == 1
