"""Server metrics and on-demand private Happ-link authorization."""
import http.client
import json
import os
import stat
import subprocess
import threading
from collections import namedtuple
from http.server import ThreadingHTTPServer

import pytest

from test_cron_editor_safety import app, signed, TOKEN

# Synthetic import profile: never a real VPN credential.
URI = "vless://00000000-0000-4000-8000-000000000000@vpn.example.invalid:443?type=ws&security=tls&path=%2Ftest-only#Happ-Test"


@pytest.fixture
def config(tmp_path):
    link = tmp_path / "happ.txt"
    link.write_text(URI + "\n")
    link.chmod(0o600)
    return {"telegram": {"bot_token_env": "EDITOR_TEST_TOKEN", "allowed_users": [101, 202]}, "cron": {"enabled": False}, "services": [], "subscription_trackers": [], "vpn": {"enabled": True, "link_allowed_users": [101], "profiles": [{"id": "main", "label": "Happ · основной", "services": ["vpn-test.service"], "probe_ports": [443], "link_file": str(link)}]}}


def test_disk_counts_available_space_and_os_reserve(monkeypatch):
    usage = namedtuple("usage", "total used free")(1000, 900, 50)
    monkeypatch.setattr(app.shutil, "disk_usage", lambda path: usage)
    disk = app.collect_server_status({})["disk"]
    assert disk["total_bytes"] == 1000 and disk["used_bytes"] == 900
    assert disk["available_bytes"] == 50 and disk["reserved_bytes"] == 50
    assert disk["used_percent"] == round(900 / 950 * 100, 1)
    assert disk["status"] == "warning"


def test_unavailable_disk_is_not_zero_usage(monkeypatch):
    monkeypatch.setattr(app.shutil, "disk_usage", lambda path: (_ for _ in ()).throw(OSError("private diagnostics")))
    disk = app.collect_server_status({})["disk"]
    assert disk["status"] == "unknown" and "used_percent" not in disk
    assert "private diagnostics" not in json.dumps(disk)


def test_vpn_status_has_no_link_or_local_secret_path(monkeypatch, config):
    monkeypatch.setattr(app, "run_command", lambda command, **kw: subprocess.CompletedProcess(command, 0, "ActiveState=active\nSubState=running\n", ""))
    monkeypatch.setattr(app, "vpn_port_open", lambda port: True)
    data = app.collect_vpn_status(config)
    assert data["profiles"][0]["status"] == "running"
    assert URI not in json.dumps(data) and "link_file" not in json.dumps(data)
    assert "vless://" not in json.dumps(data)
    assert "phone_connected" not in data["profiles"][0]


@pytest.mark.parametrize("service_ok,listener_ok,expected", [(False, True, "stopped"), (True, False, "degraded"), (True, True, "running")])
def test_vpn_requires_both_daemon_and_listener(monkeypatch, config, service_ok, listener_ok, expected):
    monkeypatch.setattr(app, "run_command", lambda command, **kw: subprocess.CompletedProcess(command, 0, f"ActiveState={'active' if service_ok else 'inactive'}\nSubState=running\n", ""))
    monkeypatch.setattr(app, "vpn_port_open", lambda port: listener_ok)
    assert app.collect_vpn_status(config)["profiles"][0]["status"] == expected


def test_private_uri_is_preserved_exactly_and_only_loaded_on_demand(config):
    assert app.read_vpn_link(config, {"profile_id": "main"})["uri"] == URI


@pytest.mark.parametrize("bad", ["not-a-uri", URI + "\n" + URI, "https://example.invalid/token", URI + "\x00", "vless://bad@host.invalid:443"])
def test_invalid_link_is_rejected_without_echo(config, bad):
    path = app.pathlib.Path(config["vpn"]["profiles"][0]["link_file"])
    path.write_text(bad)
    with pytest.raises(ValueError) as error:
        app.read_vpn_link(config, {"profile_id": "main"})
    assert bad not in str(error.value) and str(path) not in str(error.value)


def test_nonprivate_and_symlink_link_files_are_rejected(config, tmp_path):
    path = app.pathlib.Path(config["vpn"]["profiles"][0]["link_file"])
    path.chmod(0o644)
    with pytest.raises(ValueError):
        app.read_vpn_link(config, {"profile_id": "main"})
    path.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    config["vpn"]["profiles"][0]["link_file"] = str(alias)
    with pytest.raises(ValueError):
        app.read_vpn_link(config, {"profile_id": "main"})


def test_profile_id_cannot_select_arbitrary_path(config):
    for payload in ({"profile_id": "/etc/passwd"}, {"profile_id": "main", "link_file": "/etc/passwd"}):
        with pytest.raises(ValueError):
            app.read_vpn_link(config, payload)


@pytest.mark.parametrize("actor,allowlist,expected", [(101, [101], 200), (202, [101], 403), (101, [], 403), (101, None, 403), (303, [101], 403), (303, [303], 403), (None, [101], 403)])
def test_link_endpoint_is_separately_authorized(monkeypatch, config, actor, allowlist, expected):
    monkeypatch.setenv("EDITOR_TEST_TOKEN", TOKEN)
    if allowlist is None:
        config["vpn"].pop("link_allowed_users")
    else:
        config["vpn"]["link_allowed_users"] = allowlist
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(app.ServerContext(config)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("POST", "/api/status", json.dumps({"action": "vpn_link", "init_data": signed(actor) if actor is not None else "auth_date=1&hash=forged", "profile_id": "main"}), {"Content-Type": "application/json"})
        response = conn.getresponse()
        body = response.read().decode()
        assert response.status == expected
        assert response.getheader("Cache-Control") == "no-store"
        if expected == 200:
            assert json.loads(body)["uri"] == URI
        else:
            assert URI not in body and "vless://" not in body
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_normal_status_and_public_html_never_contain_private_uri(monkeypatch, config):
    monkeypatch.setattr(app, "run_command", lambda command, **kw: subprocess.CompletedProcess(command, 0, "ActiveState=active\n", ""))
    monkeypatch.setattr(app, "vpn_port_open", lambda port: True)
    data = app.collect_status(config)
    assert "server" in data and "vpn" in data
    assert URI not in json.dumps(data) and "vless://" not in json.dumps(data)
    assert URI not in app.MINIAPP_HTML and "00000000-0000-4000-8000-000000000000" not in app.MINIAPP_HTML


@pytest.mark.parametrize("values", [(0, 0, 0), (100, -1, 50), (100, 50, -1)])
def test_invalid_disk_counters_remain_unknown(monkeypatch, values):
    monkeypatch.setattr(app.shutil, "disk_usage", lambda path: namedtuple("usage", "total used free")(*values))
    assert app.collect_server_status({})["disk"]["status"] == "unknown"


def test_oversize_import_is_rejected_without_content(config):
    path = app.pathlib.Path(config["vpn"]["profiles"][0]["link_file"])
    path.write_text(URI + "x" * 8192)
    with pytest.raises(ValueError) as error:
        app.read_vpn_link(config, {"profile_id": "main"})
    assert URI not in str(error.value)


def test_late_private_link_cannot_fill_or_close_a_new_operations_dialog():
    import shutil
    if not shutil.which("node"):
        pytest.skip("Node required for deterministic async ordering")
    start = app.MINIAPP_HTML.index("let vpnLinkOperation=")
    end = app.MINIAPP_HTML.index("const CRON_REASONING_VALUES=", start)
    functions = app.MINIAPP_HTML[start:end]
    harness = r'''
const tg={initData:'A'};let statusLoadIdentity='A';
const nodes=new Map();const $=id=>{if(!nodes.has(id))nodes.set(id,{open:false,value:'',textContent:'',disabled:false,hidden:true,addEventListener(){},focus(){},select(){},showModal(){this.open=true},close(){this.open=false}});return nodes.get(id)};
global.document={addEventListener(){},execCommand(){return false}};
global.window={__vpnProfiles:[{id:'main',label:'Test'}]};
const pending=[];function fetch(){return new Promise(resolve=>pending.push(resolve))}
async function load(){return true}
'''
    assertion = r'''
(async()=>{
const operationA=openVpnLink({dataset:{profileId:'main'}});
closeVpnLink();tg.initData='B';statusLoadIdentity='B';
const operationB=openVpnLink({dataset:{profileId:'main'}});const ownedB=vpnLinkOperation;
pending[0]({ok:true,json:async()=>({uri:'old-private-link'})});await operationA;
if(vpnLinkOperation!==ownedB||!$('vpn-link-dialog').open||$('vpn-link-value').value!=='')throw new Error('old private reply affected B');
pending[1]({ok:true,json:async()=>({uri:'current-private-link'})});await operationB;
if($('vpn-link-value').value!=='current-private-link')throw new Error('current reply missing');
closeVpnLink();if($('vpn-link-value').value!==''||vpnLinkOperation!==null)throw new Error('link survived close');
console.log('private link callbacks are operation-owned and scrubbed');
})().catch(error=>{console.error(error);process.exit(1)});
'''
    result = subprocess.run(["node"], input=harness + functions + assertion, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("capability", [False, None, "missing"])
@pytest.mark.parametrize("delayed", [False, True])
def test_same_identity_permission_revocation_scrubs_loaded_and_inflight_links(capability, delayed):
    import shutil
    if not shutil.which("node"):
        pytest.skip("Node required for deterministic async ordering")
    html = app.MINIAPP_HTML
    functions = html[html.index("let vpnLinkOperation="):html.index("const CRON_REASONING_VALUES=")]
    loader = html[html.index("let statusLoadSequence="):html.index("$('refresh').addEventListener")]
    harness = r'''
const tg={initData:'same-owner'};
const nodes=new Map();const $=id=>{if(!nodes.has(id))nodes.set(id,{open:false,value:'',textContent:'',innerHTML:'',disabled:false,hidden:true,classList:{add(){},remove(){}},addEventListener(){},focus(){},select(){},showModal(){this.open=true},close(){this.open=false}});return nodes.get(id)};
global.document={addEventListener(){},execCommand(){return false}};
global.window={__vpnProfiles:[]};
let statusData={vpn_can_copy:true,vpn:{profiles:[{id:'main',label:'Test'}]}};
let release;
function fetch(url,options){if(JSON.parse(options.body).action==='vpn_link')return new Promise(resolve=>release=resolve);return Promise.resolve({ok:true,json:async()=>statusData})}
function setRefreshState(){}
function render(data){window.__vpnProfiles=data.vpn.profiles}
'''
    assertion = r'''
(async()=>{
if(!(await load(true)))throw new Error('initial load failed');
const request=openVpnLink({dataset:{profileId:'main'}});
if(!DELAYED){release({ok:true,json:async()=>({uri:'synthetic-private-link'})});await request;if($('vpn-link-value').value!=='synthetic-private-link')throw new Error('link not initially loaded');}
statusData={vpn:{profiles:[{id:'main',label:'Test'}]}};
if(CAPABILITY!=='missing')statusData.vpn_can_copy=CAPABILITY;
if(!(await load(true)))throw new Error('viewer status should succeed');
if(vpnLinkOperation!==null||$('vpn-link-dialog').open||$('vpn-link-value').value!==''||!$('vpn-link-copy').disabled)throw new Error('revocation did not scrub the private dialog');
if(DELAYED){release({ok:true,json:async()=>({uri:'synthetic-private-link'})});await request;}
if(vpnLinkOperation!==null||$('vpn-link-dialog').open||$('vpn-link-value').value!==''||!$('vpn-link-copy').disabled)throw new Error('late response survived capability revocation');
console.log('same-identity permission revocation scrubs loaded and in-flight URI');
})().catch(error=>{console.error(error);process.exit(1)});
'''
    assertion = assertion.replace("DELAYED", json.dumps(delayed)).replace("CAPABILITY", json.dumps(capability))
    result = subprocess.run(["node"], input=harness + functions + loader + assertion, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
