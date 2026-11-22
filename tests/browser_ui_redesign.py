"""Opt-in presentation smoke using synthetic status; no real mutations or secrets.
Run: python tests/browser_ui_redesign.py scripts/operator_miniapp_status.py
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
from playwright.sync_api import sync_playwright, expect

def reveal(locator):
    locator.wait_for(state="attached")
    locator.evaluate("e=>{const chain=[];for(let d=e.parentElement.closest('details');d;d=d.parentElement.closest('details'))chain.unshift(d);chain.forEach(d=>d.open=true)}")

script = Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location('operator_ui_browser', script)
assert spec is not None and spec.loader is not None
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)
out = Path(os.environ.get('OPERATOR_BROWSER_OUTPUT', '/tmp/operator-ui-redesign'))
out.mkdir(parents=True, exist_ok=True)
fixture = {
    'checked_at_display': '21.11 23:00 МСК', 'services': [
        {'label': 'GitHub', 'status': 'ok', 'caption': 'Доступ к репозиториям'},
        {'label': 'Сервис с длинным названием для проверки узкого экрана', 'status': 'limited', 'caption': 'Нужно проверить подключение'}],
    'subscriptions': [{'label': 'Подписка', 'provider': 'Тест', 'available_accounts': 1,
        'accounts': [{'label': 'Основной аккаунт', 'status': 'ok', 'plan': 'Plus', 'reset_bank': 0,
            'windows': [{'label': '5 часов', 'used_percent': 35, 'remaining_percent': 65, 'reset_text': 'Сброс через 2 часа'}]}]}],
    'server': {'disk': {'used_percent': 48, 'used_bytes': 48*2**30, 'available_bytes': 52*2**30, 'total_bytes': 100*2**30}},
    'vpn': {'enabled': True, 'profiles': [{'id': 'test', 'label': 'Happ · основной профиль с длинным названием · 443', 'status': 'running'}]},
    'vpn_can_copy': True, 'cron_jobs': [{'id': 'test', 'name': 'Тестовая задача с длинным названием', 'state': 'active', 'schedule_kind': 'interval', 'schedule_display': 'каждые 30 минут', 'schedule_minutes': 30, 'periodicity_index': 5, 'reasoning_index': 4}],
    'cron_can_edit': True, 'cron_models': [{'value': 'default', 'label': 'По умолчанию'}]
}
def assert_contrast(locator):
    ratio = locator.evaluate('''e=>{
        const s=getComputedStyle(e);
        const L=color=>{const values=color.match(/[\\d.]+/g).slice(0,3).map(Number).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);return values.reduce((n,v,i)=>n+v*[.2126,.7152,.0722][i],0)};
        const a=L(s.color),b=L(s.backgroundColor);return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
    }''')
    assert ratio >= 4.5, ratio


mutations = []
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
    for scheme in ('light', 'dark'):
        context = browser.new_context(viewport={'width': 390, 'height': 844}, color_scheme=scheme)
        context.add_init_script('window.Telegram={WebApp:{initData:"synthetic-only",ready(){},expand(){}}}')
        context.route('https://telegram.org/**', lambda r: r.fulfill(body=''))
        def api(route):
            request = route.request.post_data_json
            if request.get('action'):
                mutations.append(request['action'])
                route.fulfill(status=403, json={'detail': {'message': 'Test does not allow mutations'}})
            else:
                route.fulfill(json=fixture)
        context.route('https://operator.example.invalid/api/status**', api)
        context.route('https://operator.example.invalid/miniapp', lambda r: r.fulfill(content_type='text/html', body=app.MINIAPP_HTML))
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto('https://operator.example.invalid/miniapp')
        reveal(page.locator('#server-status'))
        expect(page.locator('#server-status')).to_be_visible()
        expect(page.locator('#verified')).to_contain_text('verified')
        for width in (320, 390, 1280):
            page.set_viewport_size({'width': width, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert page.evaluate('getComputedStyle(document.body).backgroundColor') == 'rgb(15, 15, 15)'
            reveal(page.locator('#vpn-status'))
            assert page.locator('.vpn-head .pill').evaluate('e=>e.getBoundingClientRect().height') < 35
            assert page.locator('button:visible').evaluate_all('els=>els.every(e=>e.getBoundingClientRect().height>=44)')
            for selector in ('#refresh', '.vpn-profile button', '.tab[aria-selected="true"]'):
                action = page.locator(selector)
                if not action.count():
                    continue
                reveal(action)
                page.mouse.move(0, 0)
                assert_contrast(action)
                action.hover()
                assert_contrast(action)
                page.mouse.down()
                try:
                    assert_contrast(action)
                finally:
                    page.mouse.move(0, 0)
                    page.mouse.up()
            editor = page.locator('[data-cron-editor]')
            reveal(editor)
            editor.locator('summary').click()
            expect(editor.locator('select')).to_be_visible()
            assert editor.locator('select').evaluate('e=>e.getBoundingClientRect().right<=innerWidth')
            editor.locator('summary').click()
            page.locator('[data-cron-delete]').click()
            expect(page.locator('#cron-delete-dialog')).to_be_visible()
            bounds = page.locator('#cron-delete-dialog').bounding_box()
            assert bounds and bounds['x'] >= 0 and bounds['x']+bounds['width']<=width
            page.keyboard.press('Escape')
            expect(page.locator('#cron-delete-dialog')).not_to_be_visible()
            page.screenshot(path=str(out / f'{scheme}-{width}.png'), full_page=True)
        assert not mutations and not errors, (mutations, errors)
        context.close()
    browser.close()
print(json.dumps({'result': 'passed', 'viewports': [320, 390, 1280], 'client_schemes': ['light', 'dark'], 'mutations': mutations, 'screenshots': str(out)}))
