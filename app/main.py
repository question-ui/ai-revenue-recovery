"""FastAPI surface for the Revenue Recovery console.

Endpoints:
  GET  /                       -> dashboard
  GET  /api/state              -> one-shot snapshot (JSON)
  GET  /api/stream             -> Server-Sent Events, pushes snapshots on each tick
  POST /api/incident/trigger   -> inject a degradation incident
  POST /api/action/apply       -> apply the recommended payment recovery action
  GET  /api/subscriptions      -> current subscription recovery state
  POST /api/subscriptions/recover -> run the subscription recovery agent
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .engine import Engine

STATIC = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="AI Revenue Recovery")
engine = Engine()


@app.on_event("startup")
async def _startup():
    engine.warmup()
    asyncio.create_task(_sim_loop())


async def _sim_loop():
    while True:
        engine.tick()
        await asyncio.sleep(settings.tick_seconds)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
async def state():
    return JSONResponse(engine.snapshot())


@app.get("/api/stream")
async def stream():
    async def gen():
        while True:
            data = json.dumps(engine.snapshot())
            yield f"data: {data}\n\n"
            await asyncio.sleep(settings.tick_seconds)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/incident/trigger")
async def trigger(kind: str = "gateway"):
    return JSONResponse(engine.trigger_incident(kind))


@app.post("/api/action/apply")
async def apply():
    return JSONResponse(engine.apply_action())


@app.get("/api/subscriptions")
async def subscriptions():
    return JSONResponse(engine.snapshot_subscriptions())


@app.post("/api/subscriptions/recover")
async def recover_subs():
    return JSONResponse(engine.run_subscription_recovery())


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
