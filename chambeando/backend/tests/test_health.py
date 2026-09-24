"""
GET /health -- deployment liveness/readiness probe (Docker HEALTHCHECK, load
balancer). Must stay trivially cheap (no DB query) and never require auth or
leak configuration.
"""


def test_health_returns_200(client):
    r = client.get("/health")
    assert r.status_code == 200


def test_health_returns_minimal_status_json(client):
    r = client.get("/health")
    body = r.json()
    assert body["status"] == "ok"
    assert "app" in body


def test_health_requires_no_authentication(client):
    r = client.get("/health")  # no Authorization header
    assert r.status_code == 200


def test_health_response_has_no_secret_or_config_values(client):
    r = client.get("/health")
    text = r.text.lower()
    for leaked in ("secret", "token", "database_url", "app_secret", "postgresql", "sqlite"):
        assert leaked not in text


def test_health_does_not_touch_the_database(client, monkeypatch):
    """A DB outage must never turn /health red for a reason unrelated to the
    process itself being alive -- so the handler must not query the DB at
    all. Poison get_db's SessionLocal to prove nothing here opens a session."""
    from backend import database

    def _boom():
        raise AssertionError("/health must never open a DB session")

    monkeypatch.setattr(database, "SessionLocal", _boom)
    r = client.get("/health")
    assert r.status_code == 200
