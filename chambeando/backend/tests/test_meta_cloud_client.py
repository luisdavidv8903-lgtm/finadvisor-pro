"""
Phase 2D.1 section 7 — real Meta Cloud API outbound client
(MetaCloudWhatsAppClient), fully HTTP-mocked via httpx.MockTransport (no
real network call is ever made, even accidentally). Also covers config-
driven provider selection (messaging/__init__.get_conversation_router) and
confirms SandboxMetaClient still works unmodified.
"""
import httpx
import pytest

from backend.messaging.whatsapp_adapter import MetaApiError, MetaCloudWhatsAppClient, SandboxMetaClient


def _client_with_transport(handler, **kwargs) -> MetaCloudWhatsAppClient:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return MetaCloudWhatsAppClient(
        access_token="synthetic-test-token-never-real",
        phone_number_id="TEST_PHONE_NUMBER_ID_123",
        graph_api_version="v21.0",
        http_client=http_client,
        **kwargs,
    )


def test_requires_non_empty_access_token_and_phone_number_id():
    with pytest.raises(ValueError):
        MetaCloudWhatsAppClient(access_token="", phone_number_id="123")
    with pytest.raises(ValueError):
        MetaCloudWhatsAppClient(access_token="tok", phone_number_id="")


def test_outbound_request_construction():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.read()
        return httpx.Response(200, json={"messages": [{"id": "wamid.sent1"}]})

    client = _client_with_transport(handler)
    client.send_message("15550002222", "hola desde el sandbox")

    assert captured["method"] == "POST"
    assert captured["url"] == "https://graph.facebook.com/v21.0/TEST_PHONE_NUMBER_ID_123/messages"
    assert captured["auth"] == "Bearer synthetic-test-token-never-real"
    import json as _json

    body = _json.loads(captured["body"])
    assert body["messaging_product"] == "whatsapp"
    assert body["to"] == "15550002222"
    assert body["type"] == "text"
    assert body["text"]["body"] == "hola desde el sandbox"


def test_graph_api_version_is_configurable_in_the_endpoint_url():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = MetaCloudWhatsAppClient(
        access_token="tok", phone_number_id="PN1", graph_api_version="v19.0", http_client=http_client
    )
    client.send_message("155500", "hi")
    assert captured["url"].startswith("https://graph.facebook.com/v19.0/")


def test_non_2xx_response_raises_and_is_sanitized(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Invalid OAuth access token", "type": "OAuthException", "code": 190}})

    client = _client_with_transport(handler)
    with caplog.at_level("ERROR"):
        with pytest.raises(MetaApiError):
            client.send_message("155500", "hi")

    # sanitized: status + numeric error code logged, never the raw error body/message
    assert "190" in caplog.text
    assert "Invalid OAuth access token" not in caplog.text


def test_no_token_leakage_in_logs_or_exception_message(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    client = _client_with_transport(handler)
    secret_token = "synthetic-test-token-never-real"
    with caplog.at_level("DEBUG"):
        with pytest.raises(MetaApiError) as excinfo:
            client.send_message("155500", "hi")

    assert secret_token not in caplog.text
    assert secret_token not in str(excinfo.value)


def test_timeout_raises_meta_api_error_not_a_raw_httpx_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout", request=request)

    client = _client_with_transport(handler)
    with pytest.raises(MetaApiError):
        client.send_message("155500", "hi")


def test_network_error_raises_meta_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection refused", request=request)

    client = _client_with_transport(handler)
    with pytest.raises(MetaApiError):
        client.send_message("155500", "hi")


def test_no_automatic_retry_on_failure():
    """Section 7: 'Do not blindly retry non-idempotent operations.' A
    failure must result in exactly ONE outbound HTTP attempt, never a
    silent automatic retry that could double-send."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(500, text="server error")

    client = _client_with_transport(handler)
    with pytest.raises(MetaApiError):
        client.send_message("155500", "hi")
    assert attempts["count"] == 1


def test_success_does_not_raise():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [{"id": "wamid.ok1"}]})

    client = _client_with_transport(handler)
    client.send_message("155500", "hi")  # no exception


def test_malformed_error_body_still_handled_safely():
    """Meta's error response isn't always valid JSON (e.g. an upstream proxy
    error) -- must not crash while trying to extract the error code."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>Bad Gateway</html>")

    client = _client_with_transport(handler)
    with pytest.raises(MetaApiError):
        client.send_message("155500", "hi")


# ---------------------------------------------------------------------------
# Provider selection (config-driven, never inferred from credential presence)
# ---------------------------------------------------------------------------


def test_provider_selection_defaults_to_sandbox(monkeypatch):
    from backend.messaging import _build_default_client
    from backend.config import settings

    monkeypatch.setattr(settings, "WHATSAPP_PROVIDER", "sandbox")
    assert isinstance(_build_default_client(), SandboxMetaClient)


def test_provider_selection_builds_meta_client_only_when_explicitly_configured(monkeypatch):
    from backend.messaging import _build_default_client
    from backend.config import settings

    monkeypatch.setattr(settings, "WHATSAPP_PROVIDER", "meta")
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", "synthetic-token")
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "synthetic-phone-id")
    assert isinstance(_build_default_client(), MetaCloudWhatsAppClient)


def test_setting_credentials_alone_does_not_switch_provider(monkeypatch):
    """Exporting WHATSAPP_ACCESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID for local
    testing must NEVER silently start using the real Meta transport --
    WHATSAPP_PROVIDER is the only switch."""
    from backend.messaging import _build_default_client
    from backend.config import settings

    monkeypatch.setattr(settings, "WHATSAPP_PROVIDER", "sandbox")
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", "synthetic-token")
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "synthetic-phone-id")
    assert isinstance(_build_default_client(), SandboxMetaClient)


def test_sandbox_client_remains_functional():
    client = SandboxMetaClient()
    client.send_message("wa-1", "hola")
    assert client.sent == [("wa-1", "hola")]
