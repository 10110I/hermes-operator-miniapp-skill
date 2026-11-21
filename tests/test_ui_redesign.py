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


def test_action_text_contrast_in_all_button_states():
    html = app.MINIAPP_HTML
    match = re.search(r'--operator-action:(#[0-9a-f]{6})', html)
    assert match, 'Action text needs its own high-contrast token'
    def luminance(color):
        rgb = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
        return sum(v * weight for v, weight in zip(linear, (.2126, .7152, .0722)))
    foreground = luminance(match.group(1))
    for background in ('#eaf0f6', '#dde8f2', '#d4e0ec'):
        assert (luminance(background) + .05) / (foreground + .05) >= 4.5
    for selector in ('#refresh', '.vpn-profile button', '.tab[aria-selected="true"]'):
        rule = re.search(re.escape(selector) + r'\{([^}]+)\}', html)
        assert rule and 'color:var(--operator-action)' in rule.group(1)
