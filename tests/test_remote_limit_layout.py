import subprocess
import json
from scripts import operator_miniapp_status as app


def render(profile):
    script = app.MINIAPP_HTML.split('<script>')[1].split('</script>')[0]
    start = script.index('function metric(')
    end = script.index('function render(')
    helpers = script[script.index('function limitWindow('):script.index('function accountCard(')]
    code = "const esc=v=>String(v??'');const tone=v=>v;" + helpers + script[start:end]
    code += 'console.log(remoteCard('+json.dumps({'label':'Друг', 'status':'ok', 'profile':profile})+'));'
    return subprocess.run(['node','-e',code],capture_output=True,text=True,check=True).stdout


def test_limits_share_wide_block_and_reset_is_under_each_bar():
    html = render({'status':'ok','five_hour_used_pct':20,'weekly_used_pct':99,
                   'reset_bank':3,'five_hour_reset_display':'02.10 15:00 MSK',
                   'weekly_reset_display':'03.10 20:40 MSK'})
    assert 'class="remote-limits"' in html
    assert html.count('class="barline"') == 2
    assert '80% осталось' in html and '1% осталось' in html
    assert html.index('width:20%') < html.index('02.10 15:00 MSK') < html.index('width:99%') < html.index('03.10 20:40 MSK')
    assert 'Доступные ресеты: <strong>3</strong>' in html


def test_unknown_windows_do_not_look_like_zero_and_real_zero_is_visible():
    html = render({'status':'usage_unavailable','reset_bank':0})
    assert 'Доступные ресеты: <strong>0</strong>' in html
    assert html.count('Использование: нет данных') == 2
    assert '0% использовано' not in html
    assert 'Сброс · 5 часов: Нет данных' in html
