import asyncio
import functools
import re
import logging
import httpx
import json
import os
import time

from dataclasses import dataclass
from fastapi import FastAPI, Response, HTTPException
from contextlib import asynccontextmanager
from urllib.parse import urljoin
from prometheus_client import Gauge, generate_latest, CollectorRegistry, CONTENT_TYPE_LATEST
import uvicorn

from is12client import IS12Client, DeviceNavigator, monitor_role, monitor_nmos_resource

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

CONNECTION_TTL = int(os.getenv('CONNECTION_TTL', '300')) # 5 minutes default
REDISCOVERY_INTERVAL = int(os.getenv('REDISCOVERY_INTERVAL', '600')) # 10 minutes default, 0 disables


class DiscoveryError(Exception):
    """Raised when the IS-12 endpoint could not be discovered via the Node API."""


@dataclass
class TargetState:
    """Subscription-fed state for one probe target."""
    client: IS12Client
    ws_url: str
    monitors: list
    monitors_by_oid: dict
    subscribed_oids: list
    created_at: float


def handle_notification(state: TargetState, oid: int, property_id: dict, change_type: int, value):
    """Update the cached property value for a PropertyChanged notification."""
    monitor = state.monitors_by_oid.get(oid)
    if not monitor:
        logger.debug(f"Notification for unknown oid {oid}")
        return

    for prop in monitor.properties:
        if prop.id == property_id:
            prop.value = value
            logger.info(f"Property update from {state.ws_url}: {monitor.user_label} {prop.name} = {value}")
            return

    logger.debug(f"Notification for unknown property {property_id} on oid {oid}")


class ClientCache:
    """Cache for per-target subscription state to avoid repeated handshakes and polling."""
    def __init__(self, ttl: int, rediscovery_interval: int = REDISCOVERY_INTERVAL):
        self.states = {}
        self.ttl = ttl
        self.rediscovery_interval = rediscovery_interval
        self._locks = {}

    async def get_state(self, target: str) -> TargetState:
        if target not in self._locks:
            self._locks[target] = asyncio.Lock()

        async with self._locks[target]:
            now = time.time()
            if target in self.states:
                state, last_used = self.states[target]
                rediscovery_due = (
                    self.rediscovery_interval > 0
                    and now - state.created_at > self.rediscovery_interval
                )
                if state.client.is_connected() and not rediscovery_due:
                    self.states[target] = (state, now)
                    return state
                else:
                    await state.client.disconnect()
                    del self.states[target]

            state = await self._build_state(target)
            self.states[target] = (state, now)
            return state

    async def _build_state(self, target: str) -> TargetState:
        ws_url, error = await get_is12_ws_endpoint(target)
        if not ws_url:
            raise DiscoveryError(error)

        client = IS12Client(ws_url)
        await client.connect()

        try:
            navigator = DeviceNavigator(client)
            await navigator.init()
            monitors = await navigator.get_all_monitors()

            monitor_data_tasks = [client.get_properties(monitor) for monitor in monitors]
            await asyncio.gather(*monitor_data_tasks)

            state = TargetState(
                client=client,
                ws_url=ws_url,
                monitors=monitors,
                monitors_by_oid={monitor.oid: monitor for monitor in monitors},
                subscribed_oids=[],
                created_at=time.time()
            )

            client.on_notification = functools.partial(handle_notification, state)
            state.subscribed_oids = await client.subscribe([monitor.oid for monitor in monitors])
            return state
        except Exception:
            await client.disconnect()
            raise

    async def cleanup(self):
        """Close connections that haven't been used for TTL."""
        now = time.time()
        to_remove = []
        for target, (state, last_used) in self.states.items():
            if now - last_used > self.ttl:
                logger.info(f"Closing idle connection to {state.ws_url}")
                await state.client.disconnect()
                to_remove.append(target)

        for target in to_remove:
            del self.states[target]
            self._locks.pop(target, None)

client_cache = ClientCache(CONNECTION_TTL)

def sanitize_metric_name(name):
    # Turns camelCase into snake_case
    s = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s).lower().replace('.', '_')

async def get_is12_ws_endpoint(device_address: str) -> tuple[str | None, str | None]:
    # Gets WS endpoint from node API
    # Returns (ws_url, error_reason) — one of the two will be None
    base_url = f"http://{device_address}"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            node_api_url = urljoin(base_url, "/x-nmos/node/v1.3/")

            self_response = await client.get(urljoin(node_api_url, "self"))
            self_response.raise_for_status()

            devices_response = await client.get(urljoin(node_api_url, "devices"))
            devices_response.raise_for_status()
            devices = devices_response.json()

            for device in devices:
                if "controls" in device:
                    for control in device["controls"]:
                        if control.get("type") in [
                            "urn:x-nmos:control:ncp/v1.0",
                            "urn:x-nmos:control:ncp/v1.1"
                        ]:
                            ws_url = control.get("href")
                            return ws_url, None

            return None, f"No IS-12 control endpoint found on {device_address} (device reachable, but no NCP control advertised)"

    except httpx.ConnectError as e:
        logger.error(f"Could not connect to {device_address}: {e}")
        return None, f"Could not connect to {device_address}: {e}"
    except httpx.TimeoutException as e:
        logger.error(f"Timeout connecting to {device_address}: {e}")
        return None, f"Timeout connecting to {device_address}"
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error from {device_address}: {e.response.status_code}")
        return None, f"HTTP error from {device_address}: {e.response.status_code}"
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        logger.error(f"Failed to query Node API on {device_address}: {e}")
        return None, f"Failed to query Node API on {device_address}: {e}"


def render_metrics(state: TargetState):
    registry = CollectorRegistry(auto_describe=True)
    gauge_cache = {}

    subscription_active = Gauge(
        'nmos_exporter_subscription_active',
        'Whether the exporter holds an active IS-12 subscription covering all monitors',
        registry=registry
    )
    all_subscribed = set(state.subscribed_oids) >= {monitor.oid for monitor in state.monitors}
    subscription_active.set(1 if state.client.is_connected() and all_subscribed else 0)

    monitors_discovered = Gauge(
        'nmos_exporter_monitors_discovered',
        'Number of BCP-008 status monitors discovered on the target',
        registry=registry
    )
    monitors_discovered.set(len(state.monitors))

    for monitor in state.monitors:
        role = monitor_role(monitor.class_id)
        if role is None:
            continue

        resource_id = monitor_nmos_resource(monitor).get("id", "")

        for prop in monitor.properties:
            key = prop.name
            value = prop.value

            if isinstance(value, dict) and "error" in value:
                continue

            metric_name = sanitize_metric_name(key)
            metric_full_name = f'nmos_{metric_name}'

            if isinstance(value, (int, float, bool)):
                description = f'NMOS IS-12 bool value: {key}' if isinstance(value, bool) else f'NMOS IS-12 metric: {key}'

                if metric_full_name not in gauge_cache:
                    gauge = Gauge(
                        metric_full_name,
                        description,
                        labelnames=['role', 'monitor_label', 'nmos_resource_id'],
                        registry=registry
                    )
                    gauge_cache[metric_full_name] = gauge
                else:
                    gauge = gauge_cache[metric_full_name]

                gauge_value = (1 if value else 0) if isinstance(value, bool) else value
                gauge.labels(
                    role=role,
                    monitor_label=f'{monitor.user_label}',
                    nmos_resource_id=resource_id
                ).set(gauge_value)

            if isinstance(value, str):
                description = f'NMOS IS-12 string value: {key}'

                if metric_full_name not in gauge_cache:
                    gauge = Gauge(
                        metric_full_name,
                        description,
                        labelnames=['role', 'monitor_label', 'nmos_resource_id', 'value'],
                        registry=registry
                    )
                    gauge_cache[metric_full_name] = gauge
                else:
                    gauge = gauge_cache[metric_full_name]

                gauge.labels(
                    role=role,
                    monitor_label=monitor.user_label,
                    nmos_resource_id=resource_id,
                    value=value
                ).set(1)

    return generate_latest(registry)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    async def cleanup_loop():
        while True:
            await asyncio.sleep(60)
            await client_cache.cleanup()
    asyncio.create_task(cleanup_loop())
    yield

app = FastAPI(title="NMOS BCP-008 Exporter", lifespan=lifespan)

@app.get("/probe")
async def serve_metrics(target: str):
    if not target:
        raise HTTPException(status_code=400, detail="Missing target query parameter.")

    try:
        state = await client_cache.get_state(target)
        output = render_metrics(state)
        return Response(content=output, media_type=CONTENT_TYPE_LATEST)
    except DiscoveryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (ConnectionRefusedError, asyncio.TimeoutError, OSError) as e:
        logger.error(f"Could not connect to IS-12 endpoint on {target}: {e}")
        raise HTTPException(status_code=502, detail=f"Could not connect to IS-12 endpoint on {target}: {e}")
    except Exception as e:
        logger.exception(f"Internal error probing {target}")
        return Response(content=f"Error probing target: {e}", status_code=500, media_type="text/plain")

@app.get("/health")
async def health_check():
    return {"status": "OK"}

@app.get("/", response_class=Response)
async def index():
    return Response(content="""
    <html>
    <head><title>NMOS Exporter</title></head>
    <body>
    <h1>NMOS BCP-008 Prometheus Exporter</h1>
    <p><a href="/probe?target=127.0.0.1:8080">Probe an NMOS node</a></p>
    </body>
    </html>
    """, media_type="text/html")

if __name__ == '__main__':
    uvicorn.run(app, host="0.0.0.0", port=9080, log_level="info")
