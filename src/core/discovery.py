"""
High-level discovery service.

Wraps UDPDiscovery and the device registry to provide a single call that
finds all P2K devices on the local network and returns ready-to-use driver
objects.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from ..protocol.udp import UDPDiscovery
from ..protocol.packets import DiscoveryResponse
from ..devices.base import BaseDevice
from ..devices.registry import DeviceRegistry, create_device

log = logging.getLogger(__name__)


class DiscoveryService:
    """Discover SiNet2 devices, instantiate drivers, and populate a registry.

    Usage::

        svc = DiscoveryService()
        devices = svc.scan(timeout=5.0)
        for dev in devices:
            print(dev)

    Or keep it running in the background::

        svc = DiscoveryService()
        svc.start_background(interval=30.0, on_found=print)
        # ... later ...
        svc.stop_background()
    """

    def __init__(
        self,
        registry: DeviceRegistry | None = None,
        listen_port: int | None = None,
        target_port: int | None = None,
        broadcast_addr: str = "255.255.255.255",
    ) -> None:
        self.registry = registry or DeviceRegistry()
        self._udp_kwargs: dict = {"broadcast_addr": broadcast_addr}
        if listen_port is not None:
            self._udp_kwargs["listen_port"] = listen_port
        if target_port is not None:
            self._udp_kwargs["target_port"] = target_port

        self._bg_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ── one-shot scan ─────────────────────────────────────────────────────────

    def scan(
        self,
        timeout: float = 5.0,
        on_found: Callable[[BaseDevice], None] | None = None,
    ) -> list[BaseDevice]:
        """Run a single discovery scan and return all found devices.

        New devices are added to :attr:`registry`; devices already present
        (same MAC) are *not* re-instantiated.

        Args:
            timeout: UDP listen window in seconds.
            on_found: Optional callback invoked for each *new* device found.

        Returns:
            List of :class:`~purexs.devices.base.BaseDevice` instances.
        """
        found: list[BaseDevice] = []
        with UDPDiscovery(**self._udp_kwargs) as disc:
            responses: list[DiscoveryResponse] = disc.scan(timeout=timeout)

        for resp in responses:
            existing = self.registry.get(resp.mac)
            if existing is not None:
                log.debug("Device %s already known, skipping", resp.mac)
                found.append(existing)
                continue

            device = create_device(resp)
            self.registry.add(device)
            found.append(device)
            log.info("New device: %r", device)
            if on_found:
                on_found(device)

        log.info(
            "Scan complete: %d response(s), %d unique device(s) in registry",
            len(responses),
            len(self.registry),
        )
        return found

    def scan_iter(
        self,
        timeout: float = 5.0,
        on_found: Callable[[BaseDevice], None] | None = None,
    ):
        """Generator version of :meth:`scan` — yields devices as they arrive."""
        with UDPDiscovery(**self._udp_kwargs) as disc:
            for resp in disc.scan_iter(timeout=timeout):
                existing = self.registry.get(resp.mac)
                if existing is not None:
                    yield existing
                    continue
                device = create_device(resp)
                self.registry.add(device)
                if on_found:
                    on_found(device)
                yield device

    # ── background periodic scanning ──────────────────────────────────────────

    def start_background(
        self,
        interval: float = 30.0,
        timeout: float = 5.0,
        on_found: Callable[[BaseDevice], None] | None = None,
    ) -> None:
        """Start a daemon thread that re-scans every *interval* seconds."""
        if self._bg_thread and self._bg_thread.is_alive():
            return
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.wait(timeout=interval):
                try:
                    self.scan(timeout=timeout, on_found=on_found)
                except Exception:
                    log.exception("Error in background discovery scan")

        self._bg_thread = threading.Thread(
            target=_loop, name="purexs-discovery", daemon=True
        )
        self._bg_thread.start()
        log.info("Background discovery started (interval=%.0fs)", interval)

    def stop_background(self) -> None:
        """Stop the background scan thread."""
        self._stop_event.set()
        if self._bg_thread:
            self._bg_thread.join(timeout=5.0)
            self._bg_thread = None
        log.info("Background discovery stopped")
