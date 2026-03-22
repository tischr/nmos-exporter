import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, AsyncMock

from exporter import app, sanitize_metric_name

@pytest.mark.asyncio
async def test_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "OK"}

@pytest.mark.asyncio
async def test_index():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "NMOS" in resp.text

@pytest.mark.asyncio
async def test_probe_missing_target():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/probe")
    assert resp.status_code == 422