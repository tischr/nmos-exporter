import asyncio
import json
import pytest
from unittest.mock import AsyncMock

from is12client import IS12Client, MessageType, nmos_touchpoint


def test_nmos_touchpoint_extracts_nmos_resource():
    tp = [{"contextNamespace": "x-nmos",
           "resource": {"resourceType": "receiver", "id": "rx-uuid-1"}}]
    assert nmos_touchpoint(tp) == {"resourceType": "receiver", "id": "rx-uuid-1"}


def test_nmos_touchpoint_ignores_non_nmos_entries():
    tp = [
        {"contextNamespace": "x-vendor", "resource": {"id": "other"}},
        {"contextNamespace": "x-nmos",
         "resource": {"resourceType": "sender", "id": "tx-uuid-1"}},
    ]
    assert nmos_touchpoint(tp) == {"resourceType": "sender", "id": "tx-uuid-1"}


@pytest.mark.parametrize("value", [None, [], [{"contextNamespace": "x-vendor"}],
                                   [{"contextNamespace": "x-nmos", "resource": {}}]])
def test_nmos_touchpoint_returns_none_without_nmos_resource(value):
    assert nmos_touchpoint(value) is None


def make_notification(oid, property_id, value, event_id=None, change_type=0):
    return {
        "messageType": MessageType.NOTIFICATION,
        "notifications": [{
            "oid": oid,
            "eventId": event_id or {"level": 1, "index": 1},
            "eventData": {
                "propertyId": property_id,
                "changeType": change_type,
                "value": value,
                "sequenceItemIndex": None
            }
        }]
    }


@pytest.mark.asyncio
async def test_notification_dispatched_to_callback():
    client = IS12Client("ws://test")
    received = []
    client.on_notification = lambda oid, prop_id, change_type, value: received.append(
        (oid, prop_id, change_type, value)
    )

    await client._handle_response(make_notification(10, {"level": 3, "index": 1}, "healthy"))

    assert received == [(10, {"level": 3, "index": 1}, 0, "healthy")]


@pytest.mark.asyncio
async def test_notification_wrong_event_id_ignored():
    client = IS12Client("ws://test")
    received = []
    client.on_notification = lambda *args: received.append(args)

    await client._handle_response(
        make_notification(10, {"level": 3, "index": 1}, "x", event_id={"level": 2, "index": 1})
    )

    assert received == []


@pytest.mark.asyncio
async def test_notification_sequence_change_ignored():
    client = IS12Client("ws://test")
    received = []
    client.on_notification = lambda *args: received.append(args)

    await client._handle_response(
        make_notification(10, {"level": 3, "index": 1}, "x", change_type=1)
    )

    assert received == []


@pytest.mark.asyncio
async def test_notification_callback_exception_does_not_propagate():
    client = IS12Client("ws://test")

    def broken_callback(*args):
        raise RuntimeError("boom")

    client.on_notification = broken_callback

    await client._handle_response(make_notification(10, {"level": 3, "index": 1}, "x"))


@pytest.mark.asyncio
async def test_subscribe_sends_full_accumulated_set():
    client = IS12Client("ws://test")
    client.ws = AsyncMock()

    async def subscribe_with_response(oids):
        client.ws.send.reset_mock()

        async def respond():
            while client.ws.send.call_args is None:
                await asyncio.sleep(0)
            sent = json.loads(client.ws.send.call_args[0][0])
            await client._handle_response({
                "messageType": MessageType.SUBSCRIPTION_RESPONSE,
                "subscriptions": sent["subscriptions"]
            })

        responder = asyncio.create_task(respond())
        confirmed = await client.subscribe(oids)
        await responder
        return json.loads(client.ws.send.call_args[0][0]), confirmed

    sent, confirmed = await subscribe_with_response([10, 11])
    assert sent == {"messageType": MessageType.SUBSCRIPTION, "subscriptions": [10, 11]}
    assert confirmed == [10, 11]

    sent, confirmed = await subscribe_with_response([12])
    assert sent["subscriptions"] == [10, 11, 12]
    assert confirmed == [10, 11, 12]


@pytest.mark.asyncio
async def test_command_responses_still_correlate():
    client = IS12Client("ws://test")
    future = asyncio.Future()
    client._pending_requests[1] = future

    await client._handle_response({
        "messageType": MessageType.COMMAND_RESPONSE,
        "responses": [{"handle": 1, "result": {"value": 42}}]
    })

    assert future.result() == {"handle": 1, "result": {"value": 42}}
