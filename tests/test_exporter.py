import time

import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, AsyncMock, MagicMock

from exporter import app, sanitize_metric_name, TargetState, render_metrics, handle_notification
from is12client import BlockMember, Property


def make_property(name, level, index, value):
    return Property(
        description="", id={"level": level, "index": index}, name=name,
        typeName="", isReadOnly=True, isNullable=False, isSequence=False,
        constraints=None, isDeprecated=False, value=value
    )


def make_state(monitors, connected=True, subscribed_oids=None):
    client = MagicMock()
    client.is_connected.return_value = connected
    if subscribed_oids is None:
        subscribed_oids = [m.oid for m in monitors]
    return TargetState(
        client=client,
        ws_url="ws://test",
        monitors=monitors,
        monitors_by_oid={m.oid: m for m in monitors},
        subscribed_oids=subscribed_oids,
        created_at=time.time()
    )


def make_receiver_monitor():
    return BlockMember(
        oid=10, role="mon1", class_id=[1, 2, 2, 1], user_label="rx 1",
        properties=[
            make_property("overallStatus", 3, 1, 2),
            make_property("packetsLost", 4, 2, 17.5),
            make_property("signalPresent", 4, 3, True),
            make_property("overallStatusMessage", 3, 2, "PacketsLost"),
            make_property("brokenProp", 5, 1, {"error": "NotImplemented"}),
            make_property("emptyProp", 5, 2, None),
        ]
    )

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


def test_render_metrics_from_cached_values():
    receiver = make_receiver_monitor()
    sender = BlockMember(
        oid=20, role="mon2", class_id=[1, 2, 2, 2], user_label="tx 1",
        properties=[make_property("overallStatus", 3, 1, 1)]
    )
    other = BlockMember(
        oid=30, role="block", class_id=[1, 2], user_label="ignored",
        properties=[make_property("overallStatus", 3, 1, 1)]
    )
    output = render_metrics(make_state([receiver, sender, other])).decode()

    assert 'nmos_overall_status{monitor_label="rx 1",role="receiver"} 2.0' in output
    assert 'nmos_packets_lost{monitor_label="rx 1",role="receiver"} 17.5' in output
    assert 'nmos_signal_present{monitor_label="rx 1",role="receiver"} 1.0' in output
    assert 'nmos_overall_status_message{monitor_label="rx 1",role="receiver",value="PacketsLost"} 1.0' in output
    assert 'nmos_overall_status{monitor_label="tx 1",role="sender"} 1.0' in output
    assert "broken_prop" not in output
    assert "empty_prop" not in output
    assert "ignored" not in output
    assert "nmos_exporter_subscription_active 1.0" in output
    assert "nmos_exporter_monitors_discovered 3.0" in output


def test_render_metrics_reports_inactive_subscription():
    receiver = make_receiver_monitor()

    output = render_metrics(make_state([receiver], connected=False)).decode()
    assert "nmos_exporter_subscription_active 0.0" in output

    output = render_metrics(make_state([receiver], subscribed_oids=[])).decode()
    assert "nmos_exporter_subscription_active 0.0" in output


@pytest.mark.asyncio
async def test_probe_reflects_notification_update():
    state = make_state([make_receiver_monitor()])

    with patch("exporter.client_cache.get_state", new=AsyncMock(return_value=state)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/probe?target=example:80")
            assert resp.status_code == 200
            assert 'nmos_overall_status{monitor_label="rx 1",role="receiver"} 2.0' in resp.text

            handle_notification(state, 10, {"level": 3, "index": 1}, 0, 0)
            handle_notification(state, 10, {"level": 3, "index": 2}, 0, "Healthy")

            resp = await client.get("/probe?target=example:80")
            assert resp.status_code == 200
            assert 'nmos_overall_status{monitor_label="rx 1",role="receiver"} 0.0' in resp.text
            assert 'value="Healthy"' in resp.text
            assert 'value="PacketsLost"' not in resp.text

    state.client._send_command.assert_not_called()


def test_notification_for_unknown_oid_or_property_is_noop():
    state = make_state([make_receiver_monitor()])
    before = [(p.name, p.value) for p in state.monitors[0].properties]

    handle_notification(state, 99, {"level": 3, "index": 1}, 0, "x")
    handle_notification(state, 10, {"level": 99, "index": 99}, 0, "x")

    assert [(p.name, p.value) for p in state.monitors[0].properties] == before