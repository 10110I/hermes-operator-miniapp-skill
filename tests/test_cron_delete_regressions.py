"""Review regressions: exact IDs, raw schema and operation-owned UI callbacks."""
import json
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from test_cron_editor_safety import app
from test_cron_delete import JOB_ID, OTHER_ID, confirmed


def runtime_store(monkeypatch, tmp_path, raw):
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(raw))
    saves = []
    lock = threading.RLock()
    runtime = SimpleNamespace(
        _current_cron_store=lambda: SimpleNamespace(jobs_file=path),
        _jobs_lock_file=lambda: tmp_path / ".jobs.lock",
        _jobs_file_lock=lock,
        _acquire_flock=lambda fd, timeout: True,
        _release_flock=lambda fd: None,
        load_jobs=lambda: json.loads(path.read_text()).get("jobs", []),
    )
    def save(records, **kwargs):
        saves.append((records, kwargs))
        path.write_text(json.dumps({"jobs": records}))
    runtime._save_jobs_unlocked = save
    monkeypatch.setitem(sys.modules, "cron.jobs", runtime)
    return runtime, path, saves


def test_exact_delete_cannot_fall_back_to_another_tasks_name(monkeypatch, tmp_path):
    other = {"id": OTHER_ID, "name": JOB_ID, "prompt": "preserve"}
    runtime, path, saves = runtime_store(monkeypatch, tmp_path, {"jobs": [{"id": JOB_ID}, other]})
    def old_cli(command, **kwargs):
        # An independent writer removes A before the CLI resolves its ref.
        path.write_text(json.dumps({"jobs": [other]}))
        target = next((job for job in runtime.load_jobs() if job["id"] == command[-1]), None)
        target = target or next((job for job in runtime.load_jobs() if job.get("name") == command[-1]), None)
        path.write_text(json.dumps({"jobs": [job for job in runtime.load_jobs() if job != target]}))
        return subprocess.CompletedProcess(command, 0, "removed", "")
    monkeypatch.setattr(app, "run_command", old_cli)
    result = app.delete_cron_job({}, confirmed())
    assert result["ok"]
    assert json.loads(path.read_text())["jobs"] == [other]


@pytest.mark.parametrize("raw", [{}, {"jobs": {}}, {"jobs": [{"id": JOB_ID}, {"id": JOB_ID}]}, {"jobs": ["junk"]}, None])
def test_raw_invalid_storage_is_not_normalized_to_absence(monkeypatch, tmp_path, raw):
    runtime, path, saves = runtime_store(monkeypatch, tmp_path, raw)
    # In particular, real Hermes load_jobs() normalizes {} into [].
    runtime.load_jobs = lambda: []
    with pytest.raises(ValueError):
        app.cron_job_records(strict=True)
    assert json.loads(path.read_text()) == raw and not saves


def test_expired_operation_does_not_close_a_new_accounts_dialog():
    import shutil
    if not shutil.which("node"):
        pytest.skip("Node required for deterministic async ordering")
    start = app.MINIAPP_HTML.index("let cronDeletePending=")
    end = app.MINIAPP_HTML.index("function saveCronEditor", start)
    functions = app.MINIAPP_HTML[start:end]
    harness = r'''
const tg={initData:'A'};let statusLoadIdentity='A';
const nodes=new Map();const $=id=>{if(!nodes.has(id))nodes.set(id,{open:false,hidden:true,textContent:'',disabled:false,addEventListener(){},focus(){},showModal(){this.open=true},close(){this.open=false}});return nodes.get(id)};
global.window={__cronJobs:[{id:'aaaaaaaaaaaa',name:'A task'}]};
let resolveFetch;function fetch(){return new Promise(resolve=>{resolveFetch=resolve})}
async function load(){return true}
'''
    assertion = r'''
(async()=>{
openCronDelete({dataset:{jobId:'aaaaaaaaaaaa'}});
const operationA=confirmCronDelete();
tg.initData='B';statusLoadIdentity='B';closeCronDelete();
window.__cronJobs=[{id:'bbbbbbbbbbbb',name:'B task'}];
openCronDelete({dataset:{jobId:'bbbbbbbbbbbb'}});
const operationB=cronDeletePending;
resolveFetch({ok:true,json:async()=>({ok:true})});await operationA;
if(cronDeletePending!==operationB||!$('cron-delete-dialog').open||$('cron-delete-name').textContent!=='B task')throw new Error('late A reply closed B confirmation');
console.log('new account dialog survives old response');
})().catch(error=>{console.error(error);process.exit(1)});
'''
    result = subprocess.run(["node"], input=harness + functions + assertion, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("locked", [False, None])
def test_missing_cross_process_lock_fails_closed(monkeypatch, tmp_path, locked):
    raw = {"jobs": [{"id": JOB_ID}, {"id": OTHER_ID}]}
    runtime, path, saves = runtime_store(monkeypatch, tmp_path, raw)
    runtime._acquire_flock = lambda fd, timeout: locked
    with pytest.raises(ValueError):
        app.delete_cron_job({}, confirmed())
    assert json.loads(path.read_text()) == raw and not saves
    assert runtime._jobs_file_lock.acquire(blocking=False)
    runtime._jobs_file_lock.release()


def test_corrupt_raw_readback_is_not_success(monkeypatch, tmp_path):
    runtime, path, saves = runtime_store(monkeypatch, tmp_path, {"jobs": [{"id": JOB_ID}]})
    runtime._save_jobs_unlocked = lambda *a, **kw: path.write_text("{}")
    with pytest.raises(ValueError, match="проверить"):
        app.delete_cron_job({}, confirmed())


def test_real_hermes_atomic_writer_and_raw_loader_boundary(tmp_path):
    """No real home/credentials; exercise the installed writer, not a fabricated CLI."""
    import os
    import shutil
    from pathlib import Path

    cli = shutil.which("hermes")
    if not cli:
        pytest.skip("Hermes runtime integration is an opt-in local gate")
    python = str(Path(cli).with_name("python"))
    code = r'''
import importlib.util,json,os,pathlib,sys
from cron import jobs
script=sys.argv[1]
spec=importlib.util.spec_from_file_location("operator_native_under_test",script)
app=importlib.util.module_from_spec(spec);sys.modules[spec.name]=app;spec.loader.exec_module(app)
home=pathlib.Path(os.environ["HERMES_HOME"]);store=home/"cron";store.mkdir(exist_ok=True)
path=store/"jobs.json";target="123456789abc";other={"id":"abcdef123456","name":target,"prompt":"must stay"}
path.write_text(json.dumps({"jobs":[{"id":target},other]}))
result=app.delete_cron_job({}, {"job_id":target,"confirmed":True})
assert result["ok"] and json.loads(path.read_text())["jobs"]==[other]
assert app.delete_cron_job({}, {"job_id":target,"confirmed":True})["already_removed"]
assert json.loads(path.read_text())["jobs"]==[other]
path.write_text("{}")
assert jobs.load_jobs()==[],"runtime behavior changed; revisit boundary"
try: app.cron_job_records(strict=True)
except ValueError: pass
else: raise AssertionError("raw malformed store accepted")
print("real Hermes atomic writer and normalized-loader bypass verified in temporary home")
'''
    env = {**os.environ, "HERMES_HOME": str(tmp_path)}
    env.pop("HERMES_PROFILE", None)
    result = subprocess.run([python, "-c", code, str(app.SCRIPT) if hasattr(app, "SCRIPT") else app.__file__], env=env, capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
