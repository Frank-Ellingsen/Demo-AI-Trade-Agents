"""Saxo OpenAPI WebSocket Streamer for Real-Time Market Data (`saxo_streamer.py`).

Handles:
- Binary WebSocket framing parser (`SaxoBinaryProtocol.parse_frame` & `encode_frame` for testing).
- Real-time price subscription management (`/trade/v1/prices/subscriptions`).
- Control messages (`_heartbeat`, `_resetsubscriptions`, `_disconnect`).
- Stream re-authorization (`PUT /streaming/ws/authorize`) upon OAuth token refresh.
"""

from __future__ import annotations

import json
import logging
import os
import struct
from typing import Any, Callable, Dict, List, Optional

try:
    import requests
except ImportError:
    requests = None

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SaxoStreamer")


class SaxoBinaryProtocol:
    """Parser and encoder for Saxo OpenAPI binary WebSocket frames."""

    @staticmethod
    def encode_frame(msg_id: int, ref_id: str, payload: dict[str, Any] | bytes) -> bytes:
        """Encode a single Saxo binary WebSocket frame (useful for unit tests and local replay)."""
        ref_bytes = ref_id.encode("ascii")
        if isinstance(payload, dict):
            payload_format = 0
            payload_bytes = json.dumps(payload).encode("utf-8")
        else:
            payload_format = 1
            payload_bytes = payload

        header = struct.pack("<QHB", msg_id, 0, len(ref_bytes))
        meta = struct.pack("<BI", payload_format, len(payload_bytes))
        return header + ref_bytes + meta + payload_bytes

    @staticmethod
    def parse_frame(data: bytes) -> List[Dict[str, Any]]:
        """Decode one or more concatenated Saxo binary WebSocket frames."""
        offset = 0
        total_len = len(data)
        messages: List[Dict[str, Any]] = []

        while offset < total_len:
            if total_len - offset < 16:
                break

            msg_id = struct.unpack_from("<Q", data, offset)[0]
            offset += 10

            ref_id_len = struct.unpack_from("B", data, offset)[0]
            offset += 1

            ref_id = data[offset : offset + ref_id_len].decode("ascii", errors="replace")
            offset += ref_id_len

            payload_format = struct.unpack_from("B", data, offset)[0]
            offset += 1
            payload_size = struct.unpack_from("<I", data, offset)[0]
            offset += 4

            payload_bytes = data[offset : offset + payload_size]
            offset += payload_size

            if payload_format == 0:
                try:
                    payload_json = json.loads(payload_bytes.decode("utf-8"))
                    messages.append({"msg_id": msg_id, "ref_id": ref_id, "data": payload_json})
                except Exception as e:
                    logger.error(f"Feil ved parsing av JSON for {ref_id}: {e}")
            else:
                messages.append({"msg_id": msg_id, "ref_id": ref_id, "data": payload_bytes})

        return messages


class SaxoStreamer:
    """WebSocket client for real-time price streaming from Saxo Bank OpenAPI."""

    def __init__(
        self,
        access_token: str,
        context_id: str = "obx_stream_01",
        config_path: str = "config/app_config.json",
        ws_base_url: Optional[str] = None,
        rest_base_url: Optional[str] = None,
        on_price_update: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self.access_token = access_token
        self.context_id = context_id
        self.on_price_update = on_price_update

        cfg: Dict[str, Any] = {}
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)

        default_rest = cfg.get("OpenApiBaseUrl", "https://gateway.saxobank.com/sim/openapi")
        default_ws = cfg.get("StreamingUrl", "wss://sim-streaming.saxobank.com/sim/oapi/streaming/ws/connect")

        self.rest_base_url = (rest_base_url or default_rest).rstrip("/")
        base_ws = (ws_base_url or default_ws).rstrip("/")
        self.ws_connect_url = f"{base_ws}?contextId={context_id}"

        self.active_subscriptions: Dict[str, int] = {}
        self.is_running = False

    def create_price_subscription(self, uic: int, reference_id: str, refresh_rate_ms: int = 1000) -> bool:
        """Create a real-time price subscription for a stock UIC via REST API."""
        if requests is None:
            return False
        url = f"{self.rest_base_url}/trade/v1/prices/subscriptions"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "Arguments": {"AssetType": "Stock", "Uic": uic},
            "ContextId": self.context_id,
            "ReferenceId": reference_id,
            "RefreshRate": refresh_rate_ms,
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            if resp.status_code == 201:
                self.active_subscriptions[reference_id] = uic
                return True
            return False
        except Exception:
            return False

    def remove_subscription(self, reference_id: str) -> bool:
        """Delete an active price subscription."""
        if requests is None:
            return False
        url = f"{self.rest_base_url}/trade/v1/prices/subscriptions/{self.context_id}/{reference_id}"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            resp = requests.delete(url, headers=headers, timeout=10)
            if resp.status_code in (202, 204):
                self.active_subscriptions.pop(reference_id, None)
                return True
        except Exception:
            pass
        return False

    def reauthorize_stream(self, new_access_token: str) -> bool:
        """Send `PUT /streaming/ws/authorize` to refresh session token without dropping WebSocket."""
        self.access_token = new_access_token
        if requests is None:
            return False
        url = f"{self.rest_base_url}/streaming/ws/authorize?contextid={self.context_id}"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            resp = requests.put(url, headers=headers, timeout=10)
            return resp.status_code == 200
        except Exception:
            return False

    async def listen(self) -> None:
        """Listen to incoming binary WebSocket frames via `aiohttp` or `websockets`."""
        headers = {"Authorization": f"Bearer {self.access_token}"}
        self.is_running = True

        if not HAS_AIOHTTP and not HAS_WEBSOCKETS:
            raise ImportError("Verken aiohttp eller websockets er installert. Kjør: pip install aiohttp")

        if HAS_AIOHTTP:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(self.ws_connect_url, headers=headers) as ws:
                    async for msg in ws:
                        if not self.is_running:
                            break
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            self._process_frames(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
        else:
            async with websockets.connect(self.ws_connect_url, additional_headers=headers) as ws:
                async for raw_frame in ws:
                    if not self.is_running:
                        break
                    if isinstance(raw_frame, bytes):
                        self._process_frames(raw_frame)

    def _process_frames(self, raw_bytes: bytes) -> None:
        messages = SaxoBinaryProtocol.parse_frame(raw_bytes)
        for item in messages:
            ref = item["ref_id"]
            data = item["data"]

            if ref in ("_heartbeat", "_resetsubscriptions"):
                continue
            if ref == "_disconnect":
                self.is_running = False
                break

            if self.on_price_update and isinstance(data, dict):
                self.on_price_update(ref, data)

    def stop(self) -> None:
        self.is_running = False


if __name__ == "__main__":
    print("SaxoStreamer initialisert og klar.")
