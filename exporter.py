import asyncio
import re
import logging
import requests
import json
import os

from flask import Flask, request, Response
from urllib.parse import urljoin
from prometheus_client import Gauge, Info, generate_latest, CollectorRegistry, CONTENT_TYPE_LATEST
from waitress import serve

from is12client import IS12Client, DeviceNavigator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LISTEN_HOST = os.getenv('LISTEN_HOST', '0.0.0.0')
LISTEN_PORT = int(os.getenv('LISTEN_PORT', '9080'))


registry = CollectorRegistry(auto_describe=True)
gauge_cache = {}

def sanitize_metric_name(name):
    # Turns camelCase into snake_case
    s = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s).lower().replace('.', '_')

def get_is12_ws_endpoint(device_address: str):
    # Gets WS endpoint from node API
    base_url = f"http://{device_address}"
    
    try:
        node_api_url = urljoin(base_url, "/x-nmos/node/v1.3/")
        
        self_response = requests.get(urljoin(node_api_url, "self"), timeout=5)
        self_response.raise_for_status()
        node_data = self_response.json()
        
        devices_response = requests.get(urljoin(node_api_url, "devices"), timeout=5)
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
        
    except requests.RequestException as e:
        logger.error(f"Error getting IS12 endpoint for NMOS device: {e}")
        return None


async def update_nmos_metrics(target_ws_url):
    # Fetches and Updates BCP-008 metric
    client = IS12Client(target_ws_url)
    await client.connect()

    try:
        navigator = DeviceNavigator(client)
        await navigator.init()
        monitor_blocks = await navigator.get_all_monitors()

        for monitor in monitor_blocks:
            monitor_values = await client.get_properties(monitor)
            
            if monitor.class_id == [1, 2, 2, 2]:
                role = 'sender'
            elif monitor.class_id == [1, 2, 2, 1]:
                role = 'receiver'

            info_labels = {}

            for key, value in monitor_values.items():
                metric_name = sanitize_metric_name(key)
                metric_full_name = f'nmos_{metric_name}'

                if isinstance(value, (int, float, bool)):
                    # Determine description based on type
                    description = f'NMOS IS-12 bool value: {key}' if isinstance(value, bool) else f'NMOS IS-12 metric: {key}'
                    
                    # Get or create gauge
                    if metric_full_name not in gauge_cache:
                        gauge = Gauge(
                            metric_full_name,
                            description,
                            labelnames=['role', 'monitor_label'],
                            registry=registry
                        )
                        gauge_cache[metric_full_name] = gauge
                        logger.info(f"Created metric {metric_name}")
                    else:
                        gauge = gauge_cache[metric_full_name]
                    
                    gauge_value = (1 if value else 0) if isinstance(value, bool) else value
                    gauge.labels(role=role, monitor_label=f'{monitor.user_label}').set(gauge_value)

                elif isinstance(value, str) and value: 
                    info_labels[metric_name] = value

        return generate_latest(registry), None
    
    except KeyboardInterrupt:
        logger.warn("\nStopping...")
    finally:
        await client.disconnect()

app = Flask(__name__)

@app.route('/probe')
def serve_metrics():
    
    nmos_endpoint = request.args.get('target')

    if not nmos_endpoint:
        return Response("Missing target query parameter.", status=400)
    
    target_ws_url = get_is12_ws_endpoint(nmos_endpoint)

    if not target_ws_url:
        return Response("Could not find IS-12 websocket endpoint in device", status=400)
    
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    output, error = loop.run_until_complete(
        update_nmos_metrics(target_ws_url)
    )

    if error:
        return Response(error, status=500, mimetype='text/plain')
    
    return Response(output, mimetype=CONTENT_TYPE_LATEST)

@app.route('/health')
def health_check():
    return Response("OK", status=200)

@app.route('/')
def index():
    return Response("""
    <html>
    <head><title>NMOS Exporter</title></head>
    <body>
    <h1>NMOS BCP-008 Prometheus Exporter</h1>
    <p><a href="/probe?target=127.0.0.1:8080">Probe an NMOS node</a></p>
    </body>
    </html>
    """, status=200)

if __name__ == '__main__':
    logger.info(f"Starting NMOS IS-12 Exporter on http://{LISTEN_HOST}:{LISTEN_PORT}")
    logger.info("Scraping must be done via /metrics?target=<NMOS_WS_URL>")
    logger.info(f"Configure port via LISTEN_PORT environment variable (current: {LISTEN_PORT})")
    logger.info(f"Configure host via LISTEN_HOST environment variable (current: {LISTEN_HOST})")
    
    serve(app, host=LISTEN_HOST, port=LISTEN_PORT, threads=4)
