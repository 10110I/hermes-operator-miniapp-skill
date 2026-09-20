from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_hosting_reference_defines_self_hosted_narrow_proxy():
    text = (ROOT / "references" / "hosting.md").read_text(encoding="utf-8")

    assert "same machine that runs Hermes" in text
    assert "127.0.0.1:9120" in text
    assert "/miniapp" in text
    assert "/api/status" in text
    assert "full Hermes dashboard" in text
    assert "Static hosting" in text


def test_hosting_templates_keep_server_local_and_proxy_narrow_paths():
    service = (ROOT / "templates" / "hermes-operator-miniapp.service").read_text(encoding="utf-8")
    caddy = (ROOT / "templates" / "Caddyfile").read_text(encoding="utf-8")

    assert "--host 127.0.0.1 --port 9120" in service
    assert "EnvironmentFile=-%h/.hermes/operator-miniapp/.env" in service
    assert "reverse_proxy @miniapp 127.0.0.1:9120" in caddy
    assert "path /miniapp /miniapp/* /api/status /health" in caddy
    assert "respond \"not found\" 404" in caddy


def test_service_discovery_reference_and_template_are_opt_in():
    reference = (ROOT / "references" / "service-discovery.md").read_text(encoding="utf-8")
    template = (ROOT / "templates" / "config.yaml").read_text(encoding="utf-8")

    assert "Discover candidates" in reference
    assert "does not expose token values" in reference
    assert "does not display every discovered candidate" in reference
    assert "Run `discover-services --pretty`" in template
    assert "enabled: false" in template
