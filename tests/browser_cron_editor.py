"""Opt-in real-browser/real-Hermes-CLI smoke in a disposable HERMES_HOME.

Run: uv run --with playwright --with PyYAML python tests/browser_cron_editor.py SCRIPT
Requires installed Hermes CLI and `playwright install chromium --only-shell`.
Never reads or edits the owner's cron store. No LLM calls are made.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from playwright.sync_api import sync_playwright, expect

SCRIPT = Path(sys.argv[1]).resolve()
TOKEN = '123456:TEST_TOKEN'
OUT = Path(os.environ.get('OPERATOR_BROWSER_OUTPUT', '/tmp/operator-cron-browser'))
OUT.mkdir(parents=True, exist_ok=True)


def signed(user_id=101):
    fields = {'auth_date': str(int(time.time())), 'user': json.dumps({'id': user_id, 'first_name': 'Тест'})}
    key = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(key, '\n'.join(f'{k}={v}' for k, v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def wait_ready(url, proc):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        assert proc.poll() is None, 'test server exited'
        try:
            with urlopen(url + '/health', timeout=1) as response:
                if json.load(response)['ok']:
                    return
        except OSError:
            time.sleep(0.1)
    raise AssertionError('test server did not become ready')


with tempfile.TemporaryDirectory(prefix='operator-cron-e2e-') as tmp:
    home = Path(tmp)
    # No copied auth, no inherited real HERMES_HOME; cron is never executed.
    env = {**os.environ, 'HERMES_HOME': tmp, 'EDITOR_TEST_TOKEN': TOKEN}
    env.pop('HERMES_PROFILE', None)
    (home / 'config.yaml').write_text('model:\n  provider: openai-codex\n  default: gpt-6.1-sol\n')
    cli = shutil.which('hermes')
    assert cli, 'Hermes CLI is required for this opt-in test'
    for name, schedule, provider in [('Интервальная тестовая задача с длинным названием', 'every 30m', True), ('Календарная', '0 7 * * *', True), ('Однократная', 'in 30m', True), ('Только модель без провайдера', 'every 2h', False)]:
        provider_args = ['--provider', 'openai-codex'] if provider else []
        result = subprocess.run([cli, 'cron', 'create', schedule, 'Test only; never run', '--name', name, '--model', 'gpt-6.1-sol', *provider_args, '--reasoning-effort', 'medium'], env=env, capture_output=True, text=True, timeout=25)
        assert result.returncode == 0, result.stderr[-1000:]
    def records():
        return json.loads((home / 'cron/jobs.json').read_text())['jobs']
    job_id = records()[0]['id']
    config = {'time_zone': 'UTC', 'telegram': {'bot_token_env': 'EDITOR_TEST_TOKEN', 'allowed_users': [101, 202]}, 'cron': {'enabled': True, 'edit_allowed_users': [101]}, 'services': [], 'subscription_trackers': []}
    (home / 'operator.json').write_text(json.dumps(config))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    # Use Hermes' Python so its cron/model modules are available to the script.
    python = str(Path(cli).with_name('python'))
    log = open(home / 'server.log', 'w')
    proc = subprocess.Popen([python, str(SCRIPT), '--config', str(home / 'operator.json'), 'serve', '--host', '127.0.0.1', '--port', str(port)], env=env, stdout=log, stderr=log)
    try:
        wait_ready(url, proc)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
            context = browser.new_context(viewport={'width': 390, 'height': 844}, device_scale_factor=1)
            context.add_init_script('window.Telegram={WebApp:{initData:' + json.dumps(signed()) + ',ready(){},expand(){}}};')
            # The bridge is controlled locally; no request or secret is sent to Telegram.
            context.route('https://telegram.org/js/telegram-web-app.js', lambda route: route.fulfill(body=''))
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url + '/miniapp')
            editor = page.locator(f'[data-cron-editor][data-job-id="{job_id}"]')
            try:
                expect(editor).to_be_attached(timeout=90000)
            except AssertionError:
                print('initial load diagnostics', errors, page.locator('#errtext').inner_text(), (home / 'server.log').read_text()[-3000:], flush=True)
                raise
            print('initial signed browser load passed', flush=True)
            editor.locator('summary').click()
            expect(editor.locator('[data-reasoning-value]')).to_have_text('medium')
            choices = editor.locator('[data-model] option').evaluate_all('(els)=>els.map(el=>el.value)')
            target_choice = next(value for value in choices if value != 'default' and json.loads(value)[1] != 'gpt-6.1-sol')
            target_provider, target_model = json.loads(target_choice)
            editor.locator('[data-model]').select_option(target_choice)
            for selector, value in [('[data-reasoning]', '5'), ('[data-periodicity]', '6')]:
                editor.locator(selector).evaluate('(el,value)=>{el.value=value;el.dispatchEvent(new Event("input",{bubbles:true}))}', value)
            updates = []
            deletions = []
            def track(request):
                if request.method == 'POST' and '/api/status' in request.url:
                    body = request.post_data_json
                    if body.get('action') == 'cron_delete':
                        deletions.append({k: v for k, v in body.items() if k != 'init_data'})
                    if body.get('action') == 'cron_update':
                        # Retain only non-secret fields.
                        updates.append({k: v for k, v in body.items() if k != 'init_data'})
            page.on('request', track)
            editor.locator('[data-cron-save]').click()
            expect(editor.locator('[data-cron-save]')).to_be_disabled()
            try:
                expect(editor.locator('[data-cron-message]')).to_contain_text('сохранены', timeout=90000)
            except AssertionError:
                print('browser errors', errors, 'saved records', [{k:j.get(k) for k in ('id','model','provider','reasoning_effort','schedule')} for j in records()], flush=True)
                print('editor', editor.inner_text(), flush=True)
                raise
            assert len(updates) == 1, updates
            saved = next(j for j in records() if j['id'] == job_id)
            assert saved['model'] == target_model and saved['provider'] == target_provider
            assert saved['reasoning_effort'] == 'high'
            assert saved['schedule']['kind'] == 'interval' and saved['schedule']['minutes'] == 60
            page.reload()
            expect(editor).to_be_attached(timeout=90000)
            editor.locator('summary').click()
            expect(editor.locator('[data-model]')).to_have_value(target_choice)
            expect(editor.locator('[data-reasoning]')).to_have_value('5')
            expect(editor.locator('[data-periodicity]')).to_have_value('6')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), 'mobile horizontal overflow'
            editor.scroll_into_view_if_needed()
            page.screenshot(path=str(OUT / (SCRIPT.parent.parent.name + '-mobile.png')))
            page.set_viewport_size({'width': 1280, 'height': 900})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), 'desktop horizontal overflow'
            page.screenshot(path=str(OUT / (SCRIPT.parent.parent.name + '-desktop.png')))
            # Reset pins; untouched interval must not be rescheduled.
            editor.locator('[data-model]').select_option('default')
            editor.locator('[data-reasoning]').evaluate('(el)=>{el.value="0";el.dispatchEvent(new Event("input",{bubbles:true}))}')
            editor.locator('[data-cron-save]').click()
            try:
                expect(editor.locator('[data-cron-message]')).to_contain_text('сохранены', timeout=90000)
            except AssertionError:
                print('browser errors', errors, 'saved records', [{k:j.get(k) for k in ('id','model','provider','reasoning_effort','schedule')} for j in records()], flush=True)
                print('editor', editor.inner_text(), flush=True)
                raise
            saved = next(j for j in records() if j['id'] == job_id)
            assert not saved.get('model') and not saved.get('provider') and not saved.get('reasoning_effort')
            assert saved['schedule']['minutes'] == 60
            once_id = records()[2]['id']
            once = page.locator(f'[data-cron-editor][data-job-id="{once_id}"]')
            once.locator('summary').click()
            expect(once.locator('[data-periodicity]')).to_be_disabled()
            # Calendar conversion requires confirmation; cancelling causes no write.
            calendar = page.locator(f'[data-cron-editor][data-job-id="{records()[1]["id"]}"]')
            calendar.locator('summary').click()
            calendar.locator('[data-periodicity]').evaluate('(el)=>{el.value="6";el.dispatchEvent(new Event("input",{bubbles:true}))}')
            dialogs = []
            page.on('dialog', lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
            calendar.locator('[data-cron-save]').click()
            assert len(dialogs) == 1 and len(updates) == 2
            # A model-only pin must survive changing reasoning without selecting a model.
            partial_id = records()[3]['id']
            partial = page.locator(f'[data-cron-editor][data-job-id="{partial_id}"]')
            partial.locator('summary').click()
            expect(partial.locator('[data-model]')).to_have_value('["","gpt-6.1-sol"]')
            partial.locator('[data-reasoning]').evaluate('(el)=>{el.value="5";el.dispatchEvent(new Event("input",{bubbles:true}))}')
            partial.locator('[data-cron-save]').click()
            expect(partial.locator('[data-cron-message]')).to_contain_text('сохранены', timeout=90000)
            partial_saved = next(j for j in records() if j['id'] == partial_id)
            assert partial_saved['model'] == 'gpt-6.1-sol' and partial_saved.get('provider') is None
            assert partial_saved['reasoning_effort'] == 'high'
            # API error stays actionable and lets the user retry.
            def fail_update(route):
                body = route.request.post_data_json
                if body.get('action') == 'cron_update':
                    route.fulfill(status=400, content_type='application/json', body=json.dumps({'detail': {'message': 'Тестовая ошибка сохранения'}}))
                else:
                    route.continue_()
            page.route('**/api/status', fail_update)
            editor.locator('summary').click() if not editor.evaluate('(el)=>el.open') else None
            editor.locator('[data-cron-save]').click()
            expect(editor.locator('[data-cron-message]')).to_have_text('Тестовая ошибка сохранения')
            expect(editor.locator('[data-cron-save]')).to_be_enabled()
            assert not errors, errors
            page.unroute('**/api/status', fail_update)
            # Delete confirmation: cancellation is a no-op; failure is recoverable.
            before_delete = records()
            remove = page.locator(f'[data-cron-delete][data-job-id="{job_id}"]')
            popup = page.locator('#cron-delete-dialog')
            page.set_viewport_size({'width': 390, 'height': 844})
            remove.click()
            expect(popup).to_be_visible()
            expect(popup.locator('#cron-delete-title')).to_have_text('Вы уверены что хотите удалить')
            expect(popup.locator('#cron-delete-name')).to_have_text(before_delete[0]['name'])
            expect(popup.locator('#cron-delete-cancel')).to_be_focused()
            for width in (320, 390, 1280):
                page.set_viewport_size({'width': width, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                box = popup.bounding_box()
                assert box and box['x'] >= 0 and box['x'] + box['width'] <= width
                if width != 320:
                    page.screenshot(path=str(OUT / (SCRIPT.parent.parent.name + f'-delete-{width}.png')))
            page.keyboard.press('Escape')
            expect(popup).not_to_be_visible()
            assert not deletions and records() == before_delete
            remove.click()
            popup.locator('#cron-delete-cancel').click()
            expect(popup).not_to_be_visible()
            assert not deletions and records() == before_delete
            def fail_delete(route):
                if route.request.post_data_json.get('action') == 'cron_delete':
                    route.fulfill(status=400, content_type='application/json', body=json.dumps({'detail': {'message': 'Тестовая ошибка удаления'}}))
                else:
                    route.continue_()
            page.route('**/api/status', fail_delete)
            remove.click()
            popup.locator('#cron-delete-confirm').click()
            expect(popup.locator('#cron-delete-error')).to_have_text('Тестовая ошибка удаления')
            expect(popup.locator('#cron-delete-confirm')).to_be_enabled()
            assert records() == before_delete and len(deletions) == 1
            popup.locator('#cron-delete-cancel').click()
            page.unroute('**/api/status', fail_delete)
            remove.click()
            page.evaluate("()=>{const button=document.getElementById('cron-delete-confirm');button.click();button.click()}")
            expect(popup).not_to_be_visible(timeout=90000)
            expect(page.locator('#cron-feedback')).to_have_text('Задача удалена.')
            assert len(deletions) == 2, 'duplicate clicks dispatched multiple deletes'
            assert deletions[-1] == {'action': 'cron_delete', 'job_id': job_id, 'confirmed': True}
            assert records() == [job for job in before_delete if job['id'] != job_id]
            page.reload()
            expect(page.locator('#verified')).to_contain_text('verified', timeout=90000)
            assert page.locator(f'[data-cron-delete][data-job-id="{job_id}"]').count() == 0
            assert all(job['id'] != job_id for job in records())
            assert not errors, errors
            viewer = browser.new_context(viewport={'width': 390, 'height': 844})
            viewer.add_init_script('window.Telegram={WebApp:{initData:' + json.dumps(signed(202)) + ',ready(){},expand(){}}};')
            viewer.route('https://telegram.org/js/telegram-web-app.js', lambda route: route.fulfill(body=''))
            vp = viewer.new_page()
            vp.goto(url + '/miniapp')
            expect(vp.locator('#verified')).to_contain_text('verified', timeout=90000)
            assert vp.locator('[data-cron-editor]').count() == 0
            assert vp.locator('[data-cron-delete]').count() == 0
            denied = viewer.request.post(url + '/api/status', data={'action': 'cron_update', 'init_data': signed(202)})
            assert denied.status == 403
            denied_delete = viewer.request.post(url + '/api/status', data={'action': 'cron_delete', 'init_data': signed(202), 'job_id': once_id, 'confirmed': True})
            assert denied_delete.status == 403
            assert len(records()) == len(before_delete) - 1
            viewer.close()
            # Same WebView, identity revoked/switched: no previous account data survives.
            page.locator(f'[data-cron-delete][data-job-id="{once_id}"]').click()
            expect(popup).to_be_visible()
            page.evaluate('(data)=>{window.Telegram.WebApp.initData=data;load(true)}', signed(303))
            expect(page.locator('#error')).to_be_visible(timeout=90000)
            assert page.locator('[data-cron-editor]').count() == 0
            assert page.evaluate('(window.__cronJobs||[]).length') == 0
            expect(popup).not_to_be_visible()
            expect(page.locator('#cron-delete-name')).to_have_text('')
            page.evaluate('confirmCronDelete()')
            assert len(deletions) == 2 and len(records()) == len(before_delete) - 1
            assert 'Интервальная тестовая задача' not in page.locator('#content').inner_text()
            print('native atomic delete, reload, cancellation, and account switch passed', flush=True)
            # A late deletion reply cannot restore data or feedback after account revocation.
            page.evaluate('(data)=>{window.Telegram.WebApp.initData=data;load(true)}', signed())
            expect(page.locator('#verified')).to_contain_text('verified', timeout=90000)
            held = []
            def hold_delete(route):
                if route.request.post_data_json.get('action') == 'cron_delete':
                    held.append(route)
                else:
                    route.continue_()
            page.route('**/api/status', hold_delete)
            page.locator(f'[data-cron-delete][data-job-id="{once_id}"]').click()
            popup.locator('#cron-delete-confirm').click()
            expect(popup.locator('#cron-delete-confirm')).to_be_disabled()
            page.evaluate('(data)=>{window.Telegram.WebApp.initData=data;load(true)}', signed(303))
            expect(page.locator('#error')).to_be_visible(timeout=90000)
            assert len(held) == 1
            held[0].fulfill(status=200, content_type='application/json', body=json.dumps({'ok': True, 'job_id': once_id, 'message': 'Задача удалена.'}))
            page.wait_for_function('cronDeletePending===null')
            assert page.evaluate('(window.__cronJobs||[]).length') == 0
            expect(popup).not_to_be_visible()
            expect(page.locator('#cron-feedback')).to_be_hidden()
            assert len(records()) == len(before_delete) - 1
            assert not errors, errors
            browser.close()
        print(json.dumps({'script': str(SCRIPT), 'result': 'passed', 'checks': ['mobile+desktop', 'real-cli-save', 'reload', 'default-pins', 'one-shot-disabled', 'calendar-cancel', 'error-recovery', 'viewer-denied', 'account-switch-clears-data', 'partial-model-pin-preserved', 'delete-popup-320-390-1280', 'delete-cancel-noop', 'delete-error-recovery', 'native-atomic-delete-readback', 'delete-double-click-bounded', 'delete-reload', 'delete-viewer-denied', 'delete-account-switch-clears-modal'], 'screenshots': str(OUT)}))
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        log.close()
