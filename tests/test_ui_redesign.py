"""Presentation contract: no backend or Telegram capability changes."""
import importlib.util
from pathlib import Path
import sys

SCRIPT = Path(__file__).parents[1] / 'scripts/operator_miniapp_status.py'
spec = importlib.util.spec_from_file_location('operator_redesign_contract', SCRIPT)
assert spec is not None and spec.loader is not None
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)


def test_reference_light_palette_and_native_system_typography():
    html = app.MINIAPP_HTML
    assert '--operator-bg:#f7f9fc' in html
    assert '--operator-text:#142238' in html
    assert '--operator-accent:#1971b9' in html
    assert 'font:14px/1.45 system-ui' in html
    assert 'radial-gradient' not in html
    assert 'linear-gradient' not in html
    assert 'color-scheme:light' in html


def test_safe_area_focus_and_mobile_controls():
    html = app.MINIAPP_HTML
    assert 'viewport-fit=cover' in html
    assert 'env(safe-area-inset-bottom' in html
    assert ':focus-visible' in html
    assert 'min-height:44px' in html
    assert '[hidden],.hidden{display:none!important}' in html
    assert '<div class="brand">H / OPERATOR</div>' in html
    for marker in ('id="refresh"', 'data-cron-save', 'data-cron-delete', 'id="vpn-link-dialog"'):
        assert marker in html
