"""Mobile/desktop disk + private Happ copy integration with synthetic credentials."""
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from collections import namedtuple
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright, expect

SCRIPT = Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location("operator_vpn_browser", SCRIPT)
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)
TOKEN = "123456:TEST_TOKEN"
URI = "vless://00000000-0000-4000-8000-000000000000@vpn.example.invalid:443?type=ws&security=tls&path=%2Ftest-only#Happ-Test"
OUT = Path(os.environ.get("OPERATOR_BROWSER_OUTPUT", "/tmp/operator-server-vpn-browser"))
OUT.mkdir(parents=True, exist_ok=True)


def signed(user_id):
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id})}
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, "\n".join(f"{k}={v}" for k, v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


with tempfile.TemporaryDirectory(prefix="operator-vpn-browser-") as tmp:
    home = Path(tmp)
    link = home / "happ-test.txt"
    link.write_text(URI + "\n")
    link.chmod(0o600)
    os.environ["EDITOR_TEST_TOKEN"] = TOKEN
    config = {"telegram": {"bot_token_env": "EDITOR_TEST_TOKEN", "allowed_users": [101, 202]}, "cron": {"enabled": False, "edit_allowed_users": [101]}, "services": [], "subscription_trackers": [], "vpn": {"enabled": True, "link_allowed_users": [101], "profiles": [{"id": "main", "label": "Happ · основной профиль с длинным названием · 443", "services": ["vpn-test.service"], "probe_ports": [443], "link_file": str(link)}]}}
    app.shutil.disk_usage = lambda path: namedtuple("usage", "total used free")(100 * 2**30, 90 * 2**30, 2 * 2**30)
    app.run_command = lambda command, **kw: subprocess.CompletedProcess(command, 0, "ActiveState=active\nSubState=running\n", "")
    app.vpn_port_open = lambda port: True
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(app.ServerContext(config)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context(viewport={"width": 390, "height": 844}, permissions=["clipboard-read", "clipboard-write"])
            context.add_init_script("window.Telegram={WebApp:{initData:" + json.dumps(signed(101)) + ",ready(){},expand(){}}};")
            context.route("https://telegram.org/js/telegram-web-app.js", lambda route: route.fulfill(body=""))
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url + "/miniapp")
            expect(page.locator("#server-status")).to_be_visible(timeout=15000)
            expect(page.locator("#server-status")).to_contain_text("Мало свободного места")
            expect(page.locator("#vpn-status")).to_contain_text("Работает")
            assert URI not in page.content()
            bar = page.locator("#server-status .bar i").bounding_box()
            assert bar and bar["height"] > 0 and bar["width"] > 0
            response = context.request.post(url + "/api/status", data={"init_data": signed(101)})
            assert response.status == 200 and URI not in response.text()
            assert "link_file" not in response.text()
            modal = page.locator("#vpn-link-dialog")
            for width in (320, 390, 1280):
                page.set_viewport_size({"width": width, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                page.screenshot(path=str(OUT / (SCRIPT.parent.parent.name + f"-cards-{width}.png")))
                page.locator("[data-vpn-link]").click()
                expect(modal).to_be_visible()
                expect(page.locator("#vpn-link-value")).to_have_value(URI)
                expect(page.locator("#vpn-link-close")).to_be_focused()
                bounds = modal.bounding_box()
                assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
                if width == 390:
                    page.screenshot(path=str(OUT / (SCRIPT.parent.parent.name + "-synthetic-copy-dialog.png")))
                page.locator("#vpn-link-close").click()
                expect(modal).not_to_be_visible()
                assert page.locator("#vpn-link-value").input_value() == ""
            # Real Chromium clipboard, initiated by a second user gesture (mobile-safe).
            page.locator("[data-vpn-link]").click()
            expect(page.locator("#vpn-link-copy")).to_be_enabled()
            page.locator("#vpn-link-copy").click()
            expect(page.locator("#vpn-link-message")).to_contain_text("Ссылка скопирована")
            assert page.evaluate("navigator.clipboard.readText()") == URI
            assert page.evaluate("localStorage.length+sessionStorage.length") == 0
            assert "vless" not in page.url
            # Both automated clipboard paths denied: manual fallback, not false success.
            page.evaluate("Object.defineProperty(navigator,'clipboard',{value:{writeText:()=>Promise.reject(new Error('denied'))},configurable:true});document.execCommand=()=>false")
            page.locator("#vpn-link-copy").click()
            expect(page.locator("#vpn-link-message")).to_contain_text("вручную")
            assert page.locator("#vpn-link-value").evaluate("el=>el.selectionEnd-el.selectionStart") == len(URI)
            page.keyboard.press("Escape")
            expect(modal).not_to_be_visible()
            assert page.locator("#vpn-link-value").input_value() == ""
            # Same-identity permission revocation must scrub loaded and in-flight links.
            owner_identity = page.evaluate("window.Telegram.WebApp.initData")
            for delayed in (False, True):
                config["vpn"]["link_allowed_users"] = [101]
                assert page.evaluate("load(true)") is True
                expect(page.locator("[data-vpn-link]")).to_be_visible()
                delayed_routes = []
                def delay_link(route):
                    delayed_routes.append(route)
                if delayed:
                    page.route("**/api/status", delay_link)
                page.evaluate("()=>{window.__vpnRevocationRequest=openVpnLink(document.querySelector('[data-vpn-link]'))}")
                expect(modal).to_be_visible()
                if delayed:
                    expect(page.locator("#vpn-link-copy")).to_be_disabled()
                else:
                    expect(page.locator("#vpn-link-value")).to_have_value(URI)
                config["vpn"]["link_allowed_users"] = []
                assert page.evaluate("load(true)") is True
                assert page.evaluate("window.Telegram.WebApp.initData") == owner_identity
                assert page.locator("[data-vpn-link]").count() == 0
                denied = context.request.post(url + "/api/status", data={"action": "vpn_link", "init_data": owner_identity, "profile_id": "main"})
                assert denied.status == 403
                expect(modal).not_to_be_visible()
                expect(page.locator("#vpn-link-value")).to_have_value("")
                assert page.evaluate("vpnLinkOperation===null")
                if delayed:
                    assert len(delayed_routes) == 1
                    delayed_routes[0].fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "uri": URI}))
                    page.evaluate("()=>window.__vpnRevocationRequest")
                    page.unroute("**/api/status", delay_link)
                    expect(modal).not_to_be_visible()
                    expect(page.locator("#vpn-link-value")).to_have_value("")
                    expect(page.locator("#vpn-link-copy")).to_be_disabled()
            config["vpn"]["link_allowed_users"] = [101]
            # A valid viewer is not automatically entitled to the private link.
            page.evaluate("data=>{window.Telegram.WebApp.initData=data;load(true)}", signed(202))
            expect(page.locator("#verified")).to_contain_text("verified")
            assert page.locator("[data-vpn-link]").count() == 0
            denied = context.request.post(url + "/api/status", data={"action": "vpn_link", "init_data": signed(202), "profile_id": "main"})
            assert denied.status == 403 and URI not in denied.text()
            # A delayed reply cannot repopulate a modal after revocation.
            page.evaluate("data=>{window.Telegram.WebApp.initData=data;load(true)}", signed(101))
            expect(page.locator("[data-vpn-link]")).to_be_visible()
            held = []
            def hold(route):
                if route.request.post_data_json.get("action") == "vpn_link":
                    held.append(route)
                else:
                    route.continue_()
            page.route("**/api/status", hold)
            page.locator("[data-vpn-link]").click()
            expect(modal).to_be_visible()
            expect(page.locator("#vpn-link-copy")).to_be_disabled()
            page.evaluate("data=>{window.Telegram.WebApp.initData=data;load(true)}", signed(303))
            expect(page.locator("#error")).to_be_visible()
            assert len(held) == 1
            held[0].fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "uri": URI}))
            page.wait_for_function("vpnLinkOperation===null")
            expect(modal).not_to_be_visible()
            assert page.locator("#vpn-link-value").input_value() == ""
            assert page.evaluate("window.__vpnProfiles.length") == 0
            assert not errors, errors
            browser.close()
        print(json.dumps({"script": str(SCRIPT), "result": "passed", "checks": ["disk-warning-and-bar", "vpn-local-status", "no-uri-in-normal-status", "320-390-1280-layout", "exact-clipboard-copy", "manual-fallback", "dialog-close-scrubs", "separate-owner-authorization", "no-persistent-browser-storage", "revocation-and-late-response-scrub", "same-identity-permission-revocation-loaded-and-inflight"], "screenshots": str(OUT)}))
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
