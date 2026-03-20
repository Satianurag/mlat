#!/usr/bin/env python3
"""Layer 6: Live Map — FastAPI WebSocket backend.

Reads Layer 5 JSONL track updates from stdin and broadcasts them
to connected WebSocket clients for real-time map visualization.

Architecture (from MLAT_Verified_Combined_Reference.md Part 14):
    Sensor Data -> FastAPI Backend -> WebSocket -> Browser (MapLibre + Deck.gl)

The server maintains the latest state of all active aircraft tracks
and pushes updates to connected clients at configurable intervals.

Usage:
    Full pipeline (Layer 1 → 2 → 3 → 4 → 5 → 6):
    ./data-pipe/mlat-pipe | python3 modes-decoder/main.py | \\
        python3 correlation-engine/main.py | python3 mlat-solver/main.py | \\
        python3 track-builder/main.py | python3 live-map/server.py

    Or replay from saved Layer 5 output:
    cat track_data.jsonl | python3 live-map/server.py

References:
  - MLAT_Verified_Combined_Reference.md Part 3.3 (Layer 6 spec)
  - MLAT_Verified_Combined_Reference.md Part 5.2 (Stack: FastAPI + MapLibre + Deck.gl)
  - MLAT_Verified_Combined_Reference.md Part 14 (Deck.gl guidance)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

# Add parent directory to sys.path to access mlat-solver modules
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mlat-solver"))
from gdop import compute_gdop
from geo import lla_to_ecef

# Cache for GDOP grid
_gdop_grid_cache = None

# Server configuration
HOST = os.environ.get("MLAT_MAP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MLAT_MAP_PORT", "8080"))
UPDATE_INTERVAL_S = float(os.environ.get("MLAT_UPDATE_INTERVAL", "1.0"))

PRUNE_INTERVAL_S = 60.0
MAX_TRACK_AGE_S = 300.0


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Manage background tasks for the application lifecycle."""
    task = asyncio.create_task(_periodic_prune())
    yield
    task.cancel()


async def _periodic_prune() -> None:
    """Prune stale aircraft from the store every PRUNE_INTERVAL_S seconds."""
    while True:
        await asyncio.sleep(PRUNE_INTERVAL_S)
        pruned = store.prune_stale(MAX_TRACK_AGE_S)
        if pruned > 0:
            log(f"Pruned {pruned} stale aircraft from store")


app = FastAPI(title="MLAT Live Map", version="1.0.0", lifespan=lifespan)

# Mount static files for the frontend
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── Shared state ───────────────────────────────────────────────────────

class AircraftStore:
    """Thread-safe store for current aircraft track states.

    Updated by the stdin reader thread, read by the WebSocket broadcaster.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Current state of each aircraft keyed by ICAO
        self._aircraft: dict[str, dict] = {}
        # Track position history keyed by ICAO
        self._trails: dict[str, list[list[float]]] = {}
        self._last_update = 0.0
        self.messages_received = 0

    def update(self, track: dict) -> None:
        """Update or insert an aircraft track."""
        icao = track.get("icao", "")
        if not icao:
            return

        with self._lock:
            self._aircraft[icao] = track
            self._last_update = time.time()
            self.messages_received += 1

            # Maintain trail history (last 50 positions)
            lat = track.get("lat")
            lon = track.get("lon")
            if lat is not None and lon is not None:
                if icao not in self._trails:
                    self._trails[icao] = []
                trail = self._trails[icao]
                trail.append([lon, lat])
                if len(trail) > 50:
                    self._trails[icao] = trail[-50:]

    def get_snapshot(self) -> dict:
        """Get current state of all aircraft for WebSocket broadcast."""
        with self._lock:
            aircraft_list = []
            for icao, data in self._aircraft.items():
                entry = dict(data)
                entry["trail"] = self._trails.get(icao, [])
                aircraft_list.append(entry)

            return {
                "type": "update",
                "timestamp": time.time(),
                "aircraft": aircraft_list,
                "count": len(aircraft_list),
            }

    def prune_stale(self, max_age_s: float = 300.0) -> int:
        """Remove aircraft not updated recently."""
        now = time.time()
        with self._lock:
            stale = [
                icao for icao, data in self._aircraft.items()
                if now - data.get("_update_time", 0) > max_age_s
            ]
            for icao in stale:
                del self._aircraft[icao]
                self._trails.pop(icao, None)
            return len(stale)


store = AircraftStore()


# ── WebSocket management ──────────────────────────────────────────────

class ConnectionManager:
    """Manages active WebSocket connections."""

    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: str) -> None:
        disconnected: list[WebSocket] = []
        for ws in self.active:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            self.disconnect(ws)


manager = ConnectionManager()


# ── Routes ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the main map page."""
    index_path = static_dir / "index.html"
    return HTMLResponse(content=index_path.read_text())


@app.get("/api/aircraft")
async def get_aircraft() -> dict:
    """REST endpoint for current aircraft state (polling fallback)."""
    return store.get_snapshot()


@app.get("/api/gdop_grid")
async def get_gdop_grid() -> dict:
    """Return an evaluated GDOP grid overlay for Cornwall."""
    global _gdop_grid_cache
    if _gdop_grid_cache is not None:
        return _gdop_grid_cache
        
    sensor_ecef = np.array([
        lla_to_ecef(s["lat"], s["lon"], 100.0) 
        for s in [
            {"lon": -5.7, "lat": 50.1},
            {"lon": -5.6, "lat": 50.2},
            {"lon": -5.5, "lat": 50.3},
            {"lon": -5.4, "lat": 50.15},
            {"lon": -5.3, "lat": 50.25},
            {"lon": -5.1, "lat": 50.35},
            {"lon": -5.0, "lat": 50.1},
            {"lon": -6.3, "lat": 49.92},
            {"lon": -6.35, "lat": 49.95},
        ]
    ])
    
    grid = []
    # 20x20 grid over Cornwall bounds
    lat_min, lat_max = 49.8, 50.8
    lon_min, lon_max = -6.5, -4.5
    steps = 20
    
    alt_m = 30000 * 0.3048  # evaluate GDOP at 30k feet
    
    for i in range(steps):
        lat = lat_min + (lat_max - lat_min) * i / (steps - 1)
        for j in range(steps):
            lon = lon_min + (lon_max - lon_min) * j / (steps - 1)
            pos_ecef = lla_to_ecef(lat, lon, alt_m)
            gdop = compute_gdop(pos_ecef, sensor_ecef)
            grid.append({
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "gdop": round(float(gdop), 2)
            })
            
    _gdop_grid_cache = {"grid": grid}
    return _gdop_grid_cache


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """WebSocket endpoint for real-time aircraft updates.

    Sends the current aircraft snapshot to the client at regular
    intervals (default 1 second, configurable via MLAT_UPDATE_INTERVAL).
    """
    await manager.connect(ws)
    try:
        # Send initial snapshot immediately
        snapshot = store.get_snapshot()
        await ws.send_text(json.dumps(snapshot))

        # Keep connection alive and send periodic updates
        while True:
            await asyncio.sleep(UPDATE_INTERVAL_S)
            snapshot = store.get_snapshot()
            await ws.send_text(json.dumps(snapshot))
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


# ── Stdin reader thread ───────────────────────────────────────────────

def stdin_reader() -> None:
    """Read JSONL track updates from stdin in a background thread.

    This runs in a separate thread so the FastAPI event loop
    remains responsive for WebSocket connections.
    """
    log("Stdin reader started — waiting for Layer 5 JSONL input")
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                track = json.loads(line)
                track["_update_time"] = time.time()
                store.update(track)
            except (json.JSONDecodeError, TypeError):
                continue
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    log("Stdin reader finished")


# ── Logging ───────────────────────────────────────────────────────────

def log(msg: str) -> None:
    """Log to stderr."""
    print(msg, file=sys.stderr, flush=True)


# ── Main ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="MLAT Live Map Server (Layer 6)")
    parser.add_argument(
        "--host", default=HOST,
        help=f"Server host (default: {HOST})",
    )
    parser.add_argument(
        "--port", type=int, default=PORT,
        help=f"Server port (default: {PORT})",
    )
    args = parser.parse_args()

    log("=== MLAT Live Map (Layer 6) ===")
    log(f"Backend: FastAPI v0.135.1 + WebSocket")
    log(f"Frontend: MapLibre GL JS v5.20.2 + Deck.gl v9.2.11")
    log(f"Update interval: {UPDATE_INTERVAL_S}s")
    log(f"Server: http://{args.host}:{args.port}")

    # Start stdin reader in background thread
    reader_thread = threading.Thread(target=stdin_reader, daemon=True)
    reader_thread.start()

    # Start FastAPI server
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
