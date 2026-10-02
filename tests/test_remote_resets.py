import json
from zoneinfo import ZoneInfo
import pytest
from scripts import operator_miniapp_status as app


def snapshot(tmp_path, fields):
    p = tmp_path / 'snapshot.json'
    p.write_text(json.dumps({'schema': 1, 'host': 'node', 'gateway': 'active',
        'disk_free_pct': 50, 'mem_available_pct': 50,
        'codex': {'status': 'ok', 'checked_at': app.now_iso(), 'five_hour_used_pct': 15,
                  'weekly_used_pct': 92, **fields}}))
    return app.collect_connected_user({'id': 'friend', 'type': 'snapshot_json',
        'snapshot_file': str(p), 'expected_host': 'node'}, ZoneInfo('Europe/Moscow'))['profile']


def test_remote_resets_moscow_and_zero_bank(tmp_path):
    p = snapshot(tmp_path, {'five_hour_reset_at': '2026-10-02T12:00:00Z',
                           'weekly_reset_at': '2026-10-06T10:20:00+00:00', 'reset_bank': 0})
    assert p['reset_bank'] == 0
    assert p['five_hour_reset_display'] == '02.10.2026 15:00 MSK'
    assert p['weekly_reset_display'] == '06.10.2026 13:20 MSK'


@pytest.mark.parametrize('bad', [True, -1, 'secret', float('nan')])
def test_unknown_and_invalid_reset_metadata_not_echoed(tmp_path, bad):
    p = snapshot(tmp_path, {'reset_bank': bad, 'five_hour_reset_at': 'token=secret',
                           'weekly_reset_at': '2026-10-06T00:00:00'})
    assert p['reset_bank'] is None
    assert p['five_hour_reset_display'] is None
    assert p['weekly_reset_display'] is None
    assert 'secret' not in json.dumps(p)


def test_connected_card_shows_reset_labels_and_unknown_state():
    html = app.MINIAPP_HTML
    assert 'Доступные ресеты' in html
    assert 'Сброс · 5 часов' in html
    assert 'Сброс · неделя' in html
    assert "p.reset_bank==null?'Нет данных':p.reset_bank" in html
