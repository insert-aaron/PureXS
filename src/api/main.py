"""
PureXS FastAPI application entry point.

Starts a uvicorn server on 0.0.0.0:8000.  A background UDP discovery thread
is launched at startup and cleanly stopped at shutdown.  All P2K device
sessions are closed on shutdown.

Usage::

    # via CLI (preferred)
    purexs serve

    # directly
    python -m src.api.main

    # uvicorn factory (dev reload)
    uvicorn src.api.main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..core.discovery import DiscoveryService
from ..devices.registry import DeviceRegistry
from .routes import init_app, router

log = logging.getLogger(__name__)

# ── module-level singletons (one per process) ─────────────────────────────────

_registry = DeviceRegistry()
_discovery = DiscoveryService(registry=_registry)


# ── lifespan (replaces deprecated on_event) ───────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup: initial scan + background polling.  Shutdown: clean teardown."""
    # ── startup ───────────────────────────────────────────────────────────────
    log.info("PureXS starting — running initial UDP discovery scan")
    try:
        _discovery.scan(timeout=5.0)
    except Exception:
        log.warning("Initial discovery scan failed (no network?)", exc_info=True)

    log.info("PureXS starting background discovery (interval=30s)")
    _discovery.start_background(interval=30.0, timeout=5.0)

    yield  # ── server is running ─────────────────────────────────────────────

    # ── shutdown ──────────────────────────────────────────────────────────────
    log.info("PureXS shutting down — stopping background discovery")
    _discovery.stop_background()

    log.info("PureXS shutting down — closing %d device session(s)", len(_registry))
    for device in _registry.all():
        try:
            device.disconnect()
        except Exception:
            log.debug("Error closing session for %s during shutdown", device.ip)


# ── application factory ───────────────────────────────────────────────────────

app = FastAPI(
    title="PureXS",
    version="0.1.0",
    description=(
        "Open-source reimplementation of the Sirona Sidexis dental imaging "
        "software API, speaking the native SiNet2 / P2K (Protocol 2000) wire "
        "protocol.  Discover, connect to, and acquire images from Sirona "
        "ORTHOPHOS XG, GALILEOS, HELIODENT, and related P2K devices."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# Allow browser-based tools (Swagger UI, custom frontends) to call the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Wire DI singletons before the first request arrives.
init_app(_registry, _discovery)

# Mount device routes under /devices.
app.include_router(router)


# ── meta routes ───────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def _root():
    return {"service": "PureXS", "version": "0.1.0", "docs": "/docs"}


@app.get("/health", tags=["meta"], summary="Liveness probe")
def health():
    """Returns 200 when the server is running.

    Reports how many devices are currently tracked in the registry so that
    a monitoring system can confirm discovery is working.
    """
    return {
        "status": "ok",
        "devices_known": len(_registry),
    }


# ── programmatic server entry point ──────────────────────────────────────────

def serve(
    host: str = "0.0.0.0",
    port: int = 8000,
    reload: bool = False,
    log_level: str = "info",
) -> None:
    """Start the uvicorn server.

    Called by ``purexs serve`` and by ``python -m src.api.main``.

    Args:
        host:      Bind address.  Default ``"0.0.0.0"`` (all interfaces).
        port:      HTTP port.  Default 8000.
        reload:    Enable auto-reload on source changes (dev mode only).
        log_level: uvicorn log level string (``"debug"``, ``"info"``, …).
    """
    uvicorn.run(
        "src.api.main:app",
        host=host,
        port=port,
        reload=reload,
        log_level=log_level,
    )


if __name__ == "__main__":
    serve()
