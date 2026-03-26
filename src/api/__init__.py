"""
PureXS FastAPI application factory.

Usage::

    from purexs.api import create_app
    app = create_app()

    # uvicorn purexs.api:create_app --factory --reload
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from ..core.discovery import DiscoveryService
from ..devices.registry import DeviceRegistry
from .routes import init_app, router

log = logging.getLogger(__name__)


def create_app(
    title: str = "PureXS",
    version: str = "0.1.0",
    discovery_timeout: float = 5.0,
    background_scan_interval: float | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        title: OpenAPI title shown in /docs.
        version: Application version string.
        discovery_timeout: Default UDP scan window in seconds.
        background_scan_interval: If set, start a background discovery
            thread that rescans every this many seconds.

    Returns:
        Configured :class:`fastapi.FastAPI` instance.
    """
    registry = DeviceRegistry()
    discovery = DiscoveryService(registry=registry)
    init_app(registry, discovery)

    app = FastAPI(
        title=title,
        version=version,
        description=(
            "Open-source reimplementation of the Sirona Sidexis dental "
            "imaging software API, speaking the native SiNet2/P2K protocol."
        ),
    )

    app.include_router(router)

    @app.get("/", tags=["meta"])
    def root():
        return {"name": title, "version": version, "status": "ok"}

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok", "devices": len(registry)}

    @app.on_event("startup")
    async def _startup():
        log.info("PureXS starting up — running initial discovery scan")
        try:
            discovery.scan(timeout=discovery_timeout)
        except Exception:
            log.warning("Initial discovery scan failed (no network?)", exc_info=True)
        if background_scan_interval:
            discovery.start_background(interval=background_scan_interval)

    @app.on_event("shutdown")
    async def _shutdown():
        discovery.stop_background()
        for device in registry.all():
            try:
                device.disconnect()
            except Exception:
                pass

    return app
