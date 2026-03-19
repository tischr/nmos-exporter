import asyncio
import re
import logging
import httpx
import json
import os
import time

from fastapi import FastAPI, Request, Response, HTTPException
from urllib.parse import urljoin
from prometheus_client import Gauge, Info, generate_latest, CollectorRegistry, CONTENT_TYPE_LATEST
import uvicorn

from is12client import IS12Client, DeviceNavigator

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

LISTEN_HOST = os.getenv('LISTEN_HOST', '0.0.0.0')
LISTEN_PORT = int(os.getenv('LISTEN_PORT', '9080'))
CONNECTION_TTL = int(os.getenv('CONNECTION_TTL', '300')) # 5 minutes default

class ClientCache:
    """Cache for IS12Client connections to avoid repeated handshakes."""
    def __init__(self, ttl: int):
        self.clients = {}
        self.ttl = ttl

    async def get_client(self, ws_url: str) -> IS12Client:
        now = time.time()
        if ws_url in self.clients:
            client, last_used = self.clients[ws_url]
            if client.ws and getattr(client.ws, 'state', None) == 1:
                self.clients[ws_url] = (client, now)
                return client
            else:
                await client.disconnect()
        
        client = IS12Client(ws_url)
        await client.connect()
        self.clients[ws_url] = (client, now)
        return client

    async def cleanup(self):
        """Close connections that haven't been used for TTL."""
        now = time.time()
        to_remove = []
        for ws_url, (client, last_used) in self.clients.items():
            if now - last_used > self.ttl:
                logger.info(f"Closing idle connection to {ws_url}")
                await client.disconnect()
                to_remove.append(ws_url)
        
        for ws_url in to_remove:
            del self.clients[ws_url]

client_cache = ClientCache(CONNECTION_TTL)

def sanitize_metric_name(name):
    # Turns camelCase into snake_case
    s = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s).lower().replace('.', '_')

async def get_is12_ws_endpoint(device_address: str):
    # Gets WS endpoint from node API 
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
                            return ws_url
            
            return None
            
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        logger.error(f"Error getting IS12 endpoint for NMOS device {device_address}: {e}")
        return None


async def update_nmos_metrics(target_ws_url):
    # Fetches and Updates BCP-008 metric
    registry = CollectorRegistry(auto_describe=True)
    gauge_cache = {}

    try:
        client = await client_cache.get_client(target_ws_url)
        navigator = DeviceNavigator(client)
        await navigator.init()
        monitor_blocks = await navigator.get_all_monitors()

        monitor_data_tasks = [client.get_properties(monitor) for monitor in monitor_blocks]
        all_monitor_values = await asyncio.gather(*monitor_data_tasks)

        for monitor, monitor_values in zip(monitor_blocks, all_monitor_values):
            if monitor.class_id == [1, 2, 2, 2]:
                role = 'sender'
            elif monitor.class_id == [1, 2, 2, 1]:
                role = 'receiver'
            else:
                continue

            for key, value in monitor_values.items():
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
                            labelnames=['role', 'monitor_label'],
                            registry=registry
                        )
                        gauge_cache[metric_full_name] = gauge
                    else:
                        gauge = gauge_cache[metric_full_name]
                    
                    gauge_value = (1 if value else 0) if isinstance(value, bool) else value
                    gauge.labels(role=role, monitor_label=f'{monitor.user_label}').set(gauge_value)

        return generate_latest(registry)
    
    except Exception as e:
        logger.error(f"Error updating metrics for {target_ws_url}: {e}")
        raise

app = FastAPI(title="NMOS BCP-008 Exporter")

@app.on_event("startup")
async def startup_event():
    # Start background cleanup task
    async def cleanup_loop():
        while True:
            await asyncio.sleep(60)
            await client_cache.cleanup()
    
    asyncio.create_task(cleanup_loop())

@app.get("/probe")
async def serve_metrics(target: str):
    if not target:
        raise HTTPException(status_code=400, detail="Missing target query parameter.")
    
    target_ws_url = await get_is12_ws_endpoint(target)

    if not target_ws_url:
        raise HTTPException(status_code=400, detail="Could not find IS-12 websocket endpoint in device")
    
    try:
        output = await update_nmos_metrics(target_ws_url)
        return Response(content=output, media_type=CONTENT_TYPE_LATEST)
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
    logger.info(f"Starting NMOS IS-12 Exporter on http://{LISTEN_HOST}:{LISTEN_PORT}")
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
