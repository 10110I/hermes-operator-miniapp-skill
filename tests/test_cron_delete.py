"""Confirmed cron removal is authorized, exact-targeted, and read-back verified."""
import subprocess
from types import SimpleNamespace

import pytest

from test_cron_editor_safety import app

JOB_ID = "123456789abc"
OTHER_ID = "abcdef123456"


def setup_jobs(monkeypatch, *, fail=False, retain=False):
    jobs = [{"id": JOB_ID}, {"id": OTHER_ID}]
    calls = []
    monkeypatch.setattr(app, "cron_job_records", lambda **kwargs: [dict(job) for job in jobs])

    def remove(job_id):
        records = app.cron_job_records(strict=True)
        if not any(job["id"] == job_id for job in records):
            return False
        calls.append(job_id)
        if fail:
            raise OSError("writer failed")
        if not retain:
            jobs[:] = [job for job in jobs if job["id"] != job_id]
        return True

    monkeypatch.setattr(app, "remove_cron_job_by_id", remove)
    return jobs, calls


def confirmed(**overrides):
    return {"job_id": JOB_ID, "confirmed": True, **overrides}


def test_remove_exact_target_readback_and_duplicate_is_noop(monkeypatch):
    jobs, calls = setup_jobs(monkeypatch)
    result = app.delete_cron_job({}, confirmed())
    assert result["ok"] and result["job_id"] == JOB_ID
    assert jobs == [{"id": OTHER_ID}]
    assert calls == [JOB_ID]
    again = app.delete_cron_job({}, confirmed())
    assert again["ok"] and again["already_removed"]
    assert len(calls) == 1


@pytest.mark.parametrize("job_id", ["", "123456", "-123456789ab", "123456789abc;id", "123456789abc --all", None])
def test_invalid_target_never_dispatches(monkeypatch, job_id):
    _, calls = setup_jobs(monkeypatch)
    with pytest.raises(ValueError):
        app.delete_cron_job({}, confirmed(job_id=job_id))
    assert calls == []


@pytest.mark.parametrize("confirmation", [None, False, 1, "true", "false"])
def test_confirmation_must_be_explicit_boolean(monkeypatch, confirmation):
    _, calls = setup_jobs(monkeypatch)
    with pytest.raises(ValueError):
        app.delete_cron_job({}, confirmed(confirmed=confirmation))
    assert calls == []


def test_disabled_cron_never_dispatches(monkeypatch):
    _, calls = setup_jobs(monkeypatch)
    with pytest.raises(ValueError):
        app.delete_cron_job({"cron": {"enabled": False}}, confirmed())
    assert calls == []


def test_extra_fields_cannot_smuggle_a_mutation(monkeypatch):
    _, calls = setup_jobs(monkeypatch)
    with pytest.raises(ValueError):
        app.delete_cron_job({}, confirmed(command="remove all"))
    assert calls == []


@pytest.mark.parametrize("retain,fail", [(True, False), (False, True)])
def test_store_write_without_absence_or_failure_never_reports_success(monkeypatch, retain, fail):
    jobs, calls = setup_jobs(monkeypatch, retain=retain, fail=fail)
    with pytest.raises(ValueError):
        app.delete_cron_job({}, confirmed())
    assert len(calls) == 1
    assert any(job["id"] == JOB_ID for job in jobs)


def test_unreadable_store_fails_before_dispatch(monkeypatch):
    _, calls = setup_jobs(monkeypatch)
    def broken(**kwargs):
        raise ValueError("unreadable store")
    monkeypatch.setattr(app, "cron_job_records", broken)
    with pytest.raises(ValueError):
        app.delete_cron_job({}, confirmed())
    assert calls == []


def test_unreadable_readback_is_not_success_and_is_not_retried(monkeypatch):
    _, calls = setup_jobs(monkeypatch)
    reads = []
    def broken_after(**kwargs):
        assert kwargs.get("strict") is True
        reads.append(True)
        if len(reads) == 1:
            return [{"id": JOB_ID}]
        raise ValueError("unreadable readback")
    monkeypatch.setattr(app, "cron_job_records", broken_after)
    with pytest.raises(ValueError):
        app.delete_cron_job({}, confirmed())
    assert len(calls) == 1 and len(reads) == 2


def test_timeout_is_ambiguous_and_not_automatically_retried(monkeypatch):
    _, calls = setup_jobs(monkeypatch)
    def timeout(job_id):
        calls.append(job_id)
        raise TimeoutError("writer timed out")
    monkeypatch.setattr(app, "remove_cron_job_by_id", timeout)
    with pytest.raises(ValueError, match="неизвестен"):
        app.delete_cron_job({}, confirmed())
    assert len(calls) == 1


@pytest.mark.parametrize("data", [None, {}, [{"id": JOB_ID}, "broken"]])
def test_strict_job_reader_never_treats_invalid_storage_as_empty(monkeypatch, tmp_path, data):
    import json
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(data))
    monkeypatch.setitem(app.sys.modules, "cron.jobs", SimpleNamespace(_current_cron_store=lambda: SimpleNamespace(jobs_file=path)))
    with pytest.raises(ValueError):
        app.cron_job_records(strict=True)


def test_missing_model_is_not_synthesized_or_renamed(monkeypatch):
    monkeypatch.setattr(app, "_provider_model_ids", lambda provider: ["gpt-6.1-sol", "gpt-6-luna"])
    monkeypatch.setattr(app, "load_config", lambda path: {"model": {"provider": "openai-codex", "default": "gpt-6.1-sol"}})
    options = app.cron_model_options({}, [])
    names = [option["model"] for option in options]
    assert "gpt-6-luna" in names
    assert "gpt-6.1-luna" not in names


@pytest.mark.parametrize("init_data", ["", "auth_date=1&hash=forged"])
def test_unauthenticated_delete_never_reaches_mutation(monkeypatch, init_data):
    import http.client
    import json
    import threading
    from http.server import ThreadingHTTPServer
    from test_cron_editor_safety import TOKEN

    monkeypatch.setenv("EDITOR_TEST_TOKEN", TOKEN)
    calls = []
    monkeypatch.setattr(app, "delete_cron_job", lambda *a: calls.append(a))
    config = {"telegram": {"bot_token_env": "EDITOR_TEST_TOKEN", "allowed_users": [101]}}
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(app.ServerContext(config)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("POST", "/api/status", json.dumps({"action": "cron_delete", "init_data": init_data, **confirmed()}), {"Content-Type": "application/json"})
        response = conn.getresponse()
        response.read()
        assert response.status == 403 and calls == []
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_concurrent_http_deletions_dispatch_once(monkeypatch):
    import concurrent.futures
    import http.client
    import json
    import threading
    from http.server import ThreadingHTTPServer
    from test_cron_editor_safety import TOKEN, signed

    monkeypatch.setenv("EDITOR_TEST_TOKEN", TOKEN)
    jobs, calls = setup_jobs(monkeypatch)
    gate = threading.Barrier(4)
    authenticate = app.authenticate_telegram_request
    def authenticated(*args, **kwargs):
        result = authenticate(*args, **kwargs)
        gate.wait(timeout=5)
        return result
    monkeypatch.setattr(app, "authenticate_telegram_request", authenticated)
    config = {"telegram": {"bot_token_env": "EDITOR_TEST_TOKEN", "allowed_users": [101]}}
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(app.ServerContext(config)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    identity = signed(101)
    def request():
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        conn.request("POST", "/api/status", json.dumps({"action": "cron_delete", "init_data": identity, **confirmed()}), {"Content-Type": "application/json"})
        response = conn.getresponse()
        body = json.loads(response.read())
        status = response.status
        conn.close()
        return status, body
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: request(), range(4)))
        assert all(status == 200 and body["ok"] for status, body in results)
        assert sum(bool(body.get("already_removed")) for _, body in results) == 3
        assert calls == [JOB_ID]
        assert jobs == [{"id": OTHER_ID}]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
