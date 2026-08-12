"""HMI server: FastAPI app serving the operator panel and pushing live plant
state to the browser over a websocket.

Data flow: opcua_bridge.py runs one asyncio task per PLC unit plus one for
the DCS, each polling its OPC UA server on a 1 s cycle and writing into a
shared PlantState. Every poll calls state.on_change(), which sets an asyncio
Event; a single broadcaster task wakes on that event, builds one JSON
snapshot, and sends it to every connected websocket. The browser never talks
OPC UA directly, only JSON over ws://.

Run standalone against tools/fake_plc.py:
    python tools/fake_plc.py --units 3 --base-port 4840
    python -m uvicorn hmi.main:app --host 0.0.0.0 --port 8080 --app-dir water_plant_DCS
    # or: cd water_plant_DCS && python -m uvicorn hmi.main:app --port 8080
Then open http://localhost:8080/
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from hmi.opcua_bridge import PlantState, start_polling

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("hmi.main")

STATIC_DIR = Path(__file__).resolve().parent / "static"

# PLC_ENDPOINTS: comma-separated opc.tcp:// URLs, one per unit, in order
# (Unit1 first). Matches the fake_plc / plc convention of one OPC UA server
# per unit. Default targets tools/fake_plc.py started with --units 3
# --base-port 4840 on localhost.
_default_units = "opc.tcp://localhost:4840/,opc.tcp://localhost:4841/,opc.tcp://localhost:4842/"
UNIT_ENDPOINTS = [u.strip() for u in os.environ.get("PLC_ENDPOINTS", _default_units).split(",") if u.strip()]

# DCS_ENDPOINT: the DCS's own OPC UA server exposing the global APC.* tags.
# tags.yaml does not fix a port for this (see OPEN_QUESTIONS.md), 4850 is
# this project's convention, chosen to stay clear of the 4840+ PLC range.
DCS_ENDPOINT = os.environ.get("DCS_ENDPOINT", "opc.tcp://localhost:4850/")

state = PlantState(unit_endpoints=UNIT_ENDPOINTS, dcs_endpoint=DCS_ENDPOINT)

app = FastAPI(title="water_plant_DCS HMI")

_ws_clients: set[WebSocket] = set()
_broadcast_event = asyncio.Event()


def _on_change():
    _broadcast_event.set()


async def _broadcaster():
    while True:
        await _broadcast_event.wait()
        _broadcast_event.clear()
        if not _ws_clients:
            continue
        payload = state.snapshot()
        dead = []
        for ws in _ws_clients:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            _ws_clients.discard(ws)


@app.on_event("startup")
async def on_startup():
    state.on_change = _on_change
    start_polling(state)
    asyncio.create_task(_broadcaster())
    logger.info("HMI polling units=%s dcs=%s", UNIT_ENDPOINTS, DCS_ENDPOINT)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        await websocket.send_json(state.snapshot())
        while True:
            # Panel is push-only from the server side, but we still need to
            # read the socket so disconnects are detected promptly.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)


@app.get("/api/state")
async def get_state():
    """Plain REST snapshot, useful for curl-based smoke tests without a
    websocket client."""
    return JSONResponse(state.snapshot())


class SetpointRequest(BaseModel):
    value: float


@app.post("/api/units/{unit_id}/setpoint")
async def set_unit_setpoint(unit_id: int, req: SetpointRequest):
    """Manual SP override: writes PID.SP directly on the unit. Per
    tags.yaml this is normally the DCS's job (cascade control); this path
    exists for forcing a unit into local/manual test, so the operator uses
    it deliberately, not as the default flow."""
    try:
        await state.write_unit_setpoint(unit_id, req.value)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return {"ok": True}


class ApcEnabledRequest(BaseModel):
    enabled: bool


@app.post("/api/apc/enabled")
async def set_apc_enabled(req: ApcEnabledRequest):
    try:
        await state.write_apc_enabled(req.enabled)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return {"ok": True}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
