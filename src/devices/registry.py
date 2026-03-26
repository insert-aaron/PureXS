"""
Device registry: maps P2K device-type codes to driver classes and tracks
all discovered / connected devices in a running PureXS session.
"""

from __future__ import annotations

import logging
from typing import Type

from .base import SironaDevice, BaseDevice  # BaseDevice alias preserved
from ..protocol.tcp import DeviceInfo as TCPDeviceInfo, P2KConnectionError
from ..protocol.udp import DeviceAnnounce

log = logging.getLogger(__name__)

# Maps device_type WORD → SironaDevice subclass.
# Populated by _auto_register() at import time; extend via register().
_REGISTRY: dict[int, Type[SironaDevice]] = {}


def register(cls: Type[SironaDevice]) -> None:
    """Register a driver class for every device type code in its SUPPORTED_TYPES."""
    for code in cls.SUPPORTED_TYPES:
        _REGISTRY[code] = cls
        log.debug(
            "Registered %s for device type 0x%04X", cls.__name__, code
        )


def _auto_register() -> None:
    from .orthophos_xg import OrthophosXG
    register(OrthophosXG)


_auto_register()


def create_device(announce: DeviceAnnounce) -> SironaDevice:
    """Instantiate the best-matching driver for *announce*.

    Looks up :attr:`~SironaDevice.SUPPORTED_TYPES` in the registry.
    Falls back to :class:`GenericP2KDevice` for unknown type codes.
    """
    cls = _REGISTRY.get(announce.device_type, GenericP2KDevice)
    device = cls.from_announce(announce)
    log.info(
        "Created %s for device type 0x%04X at %s",
        cls.__name__,
        announce.device_type,
        announce.ip,
    )
    return device


class DeviceRegistry:
    """Session-scoped registry mapping MAC → SironaDevice."""

    def __init__(self) -> None:
        self._devices: dict[str, SironaDevice] = {}

    def add(self, device: SironaDevice) -> None:
        self._devices[device.mac] = device

    def remove(self, mac: str) -> None:
        self._devices.pop(mac, None)

    def get(self, mac: str) -> SironaDevice | None:
        return self._devices.get(mac)

    def all(self) -> list[SironaDevice]:
        return list(self._devices.values())

    def by_ip(self, ip: str) -> SironaDevice | None:
        for dev in self._devices.values():
            if dev.ip == ip:
                return dev
        return None

    def __len__(self) -> int:
        return len(self._devices)

    def __iter__(self):
        return iter(self._devices.values())


# ── fallback driver ───────────────────────────────────────────────────────────

class GenericP2KDevice(SironaDevice):
    """Minimal driver used when no specific driver is registered for a device type."""

    async def async_connect(self) -> None:
        from ..protocol.tcp import SiNet2Client
        client = SiNet2Client()
        await client.connect(self._ip, self._port)
        self._client = client

    async def async_disconnect(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    async def async_get_info(self) -> TCPDeviceInfo:
        self._require_connected()
        assert self._client is not None
        info = await self._client.request_info()
        self._serial_number = info.serial_number
        self._firmware_version = info.firmware_version
        return info

    async def async_request_xray(self, **kwargs: object) -> bytes:
        self._require_connected()
        assert self._client is not None
        return await self._client.send(0x0010, b"")  # FUNC_GET_IMAGE fallback
