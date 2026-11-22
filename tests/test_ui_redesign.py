"""Presentation contract: no backend or Telegram capability changes."""
import importlib.util
from pathlib import Path
import sys
import re

SCRIPT = Path(__file__).parents[1] / 'scripts/operator_miniapp_status.py'
spec = importlib.util.spec_from_file_location('operator_redesign_contract', SCRIPT)
assert spec is not None and spec.loader is not None
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)


def test_stavkaposting_dark_palette_and_native_system_typography():
    html = app.MINIAPP_HTML
    assert '--operator-bg:#0f0f0f' in html
    assert '--operator-text:#fff' in html
    assert '--operator-accent:#6ab3f3' in html
    assert 'font:14px/1.35 system-ui' in html
    assert 'radial-gradient' not in html
    assert 'linear-gradient' not in html
    assert 'color-scheme:dark' in html
    assert '--operator-surface:#212121' in html
    assert 'padding:14px;margin-top:7px' in html
    assert '.bar{height:5px' in html
    assert '<header class="top"><div class="brand">H / OPERATOR</div><button id="refresh"' in html
    assert '<div class="intro"><h1 class="title">Hermes Operator</h1>' in html


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


def test_action_text_contrast_in_all_button_states():
    html = app.MINIAPP_HTML
    match = re.search(r'--operator-action:(#[0-9a-f]{6})', html)
    assert match, 'Action text needs its own high-contrast token'
    def luminance(color):
        rgb = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
        return sum(v * weight for v, weight in zip(linear, (.2126, .7152, .0722)))
    foreground = luminance(match.group(1))
    for background in ('#212121', '#292929', '#303030', '#383838'):
        assert (foreground + .05) / (luminance(background) + .05) >= 4.5
    for selector in ('.vpn-profile button', '.tab[aria-selected="true"]'):
        rule = re.search(re.escape(selector) + r'\{([^}]+)\}', html)
        assert rule and 'color:var(--operator-action)' in rule.group(1)
