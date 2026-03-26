"""
PureXS REST API — FastAPI route definitions.

Endpoints:

  GET  /devices                      List all discovered devices
  POST /devices/scan                 Trigger a discovery scan
  GET  /devices/{mac}                Get a single device by MAC
  GET  /devices/{mac}/status         Query live device status
  POST /devices/{mac}/connect        Open a TCP session
  POST /devices/{mac}/disconnect     Close the TCP session
  POST /devices/{mac}/acquire        Trigger image acquisition
  GET  /devices/{mac}/param/{id}     Read a device parameter
  PUT  /devices/{mac}/param/{id}     Write a device parameter
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Body, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ..core.discovery import DiscoveryService
from ..devices.registry import DeviceRegistry
from ..devices.base import BaseDevice, DeviceState
from ..protocol.tcp import P2KError

log = logging.getLogger(__name__)
router = APIRouter(prefix="/devices", tags=["devices"])

# ── dependency injection helpers ──────────────────────────────────────────────

# These are set once at application startup via init_app().
_registry: DeviceRegistry | None = None
_discovery: DiscoveryService | None = None


def init_app(registry: DeviceRegistry, discovery: DiscoveryService) -> None:
    """Bind module-level singletons used by the dependency functions."""
    global _registry, _discovery
    _registry = registry
    _discovery = discovery


def get_registry() -> DeviceRegistry:
    if _registry is None:
        raise RuntimeError("API not initialised — call init_app() first")
    return _registry


def get_discovery() -> DiscoveryService:
    if _discovery is None:
        raise RuntimeError("API not initialised — call init_app() first")
    return _discovery


def _get_device(mac: str, registry: DeviceRegistry = Depends(get_registry)) -> BaseDevice:
    device = registry.get(mac.upper())
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device {mac!r} not found")
    return device


# ── response schemas ──────────────────────────────────────────────────────────

class DeviceListEntry(BaseModel):
    """Richer device representation returned by GET /devices."""

    id: str            # MAC address — primary key in the registry
    display_name: str  # human-readable product name
    ip: str
    port: int
    mac: str
    connected: bool    # True when an async TCP session is open
    serial: str        # factory serial number (empty until async_get_info called)


class DeviceSummary(BaseModel):
    mac: str
    ip: str
    tcp_port: int
    device_type: int
    device_type_name: str
    state: str


class ScanRequest(BaseModel):
    timeout: float = 5.0


class ParamWriteRequest(BaseModel):
    value_hex: str  # hex-encoded bytes, e.g. "0064"


# ── helpers ───────────────────────────────────────────────────────────────────

def _list_entry(device: BaseDevice) -> DeviceListEntry:
    return DeviceListEntry(
        id=device.info.mac,
        display_name=device.info.device_type_name,
        ip=device.info.ip,
        port=device.info.tcp_port,
        mac=device.info.mac,
        connected=device.is_connected,
        serial=device.serial_number,
    )


def _summarise(device: BaseDevice) -> DeviceSummary:
    return DeviceSummary(
        mac=device.info.mac,
        ip=device.info.ip,
        tcp_port=device.info.tcp_port,
        device_type=device.info.device_type,
        device_type_name=device.info.device_type_name,
        state=device.state.name,
    )


def _p2k_err(exc: P2KError) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail=f"Device returned error 0x{exc.error_code:04X}: {exc}",
    )


# ── routes ────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[DeviceListEntry])
def list_devices(registry: DeviceRegistry = Depends(get_registry)):
    """Return all known devices with connection and serial info."""
    return [_list_entry(d) for d in registry.all()]


@router.post("/scan", response_model=list[DeviceSummary])
def scan_devices(
    req: ScanRequest = Body(default=ScanRequest()),
    discovery: DiscoveryService = Depends(get_discovery),
):
    """Broadcast a P2K discovery probe and return responding devices."""
    devices = discovery.scan(timeout=req.timeout)
    return [_summarise(d) for d in devices]


@router.get("/{mac}", response_model=DeviceSummary)
def get_device(device: BaseDevice = Depends(_get_device)):
    return _summarise(device)


@router.get("/{mac}/status")
def device_status(device: BaseDevice = Depends(_get_device)) -> dict[str, Any]:
    """Query live status from the device over TCP."""
    if device.state not in (DeviceState.CONNECTED, DeviceState.READY, DeviceState.BUSY):
        raise HTTPException(
            status_code=409,
            detail=f"Device is not connected (state={device.state.name}). "
                   "POST /connect first.",
        )
    try:
        return device.get_status()
    except P2KError as exc:
        raise _p2k_err(exc)


@router.post("/{mac}/connect", response_model=DeviceSummary)
def connect_device(device: BaseDevice = Depends(_get_device)):
    """Open a TCP session to the device."""
    try:
        device.connect()
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return _summarise(device)


@router.post("/{mac}/disconnect", response_model=DeviceSummary)
def disconnect_device(device: BaseDevice = Depends(_get_device)):
    """Close the TCP session."""
    device.disconnect()
    return _summarise(device)


@router.post("/{mac}/acquire")
def acquire_image(device: BaseDevice = Depends(_get_device)) -> dict[str, Any]:
    """Trigger an X-ray exposure and return image metadata.

    The raw image bytes are base64-encoded in the response body.
    """
    import base64

    if device.state not in (DeviceState.CONNECTED, DeviceState.READY):
        raise HTTPException(
            status_code=409,
            detail=f"Device not ready for acquisition (state={device.state.name})",
        )
    try:
        raw = device.acquire_image()
    except P2KError as exc:
        raise _p2k_err(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return {
        "mac": device.info.mac,
        "ip": device.info.ip,
        "size_bytes": len(raw),
        "data_b64": base64.b64encode(raw).decode(),
    }


@router.get("/{mac}/param/{param_id}")
def read_param(
    param_id: int,
    device: BaseDevice = Depends(_get_device),
) -> dict[str, str]:
    """Read a raw device parameter by numeric ID."""
    try:
        raw = device.get_param(param_id)
    except P2KError as exc:
        raise _p2k_err(exc)
    return {"param_id": hex(param_id), "value_hex": raw.hex()}


@router.put("/{mac}/param/{param_id}", status_code=204)
def write_param(
    param_id: int,
    req: ParamWriteRequest,
    device: BaseDevice = Depends(_get_device),
) -> None:
    """Write a raw device parameter by numeric ID."""
    try:
        value = bytes.fromhex(req.value_hex)
    except ValueError:
        raise HTTPException(status_code=422, detail="value_hex must be valid hex")
    try:
        device.set_param(param_id, value)
    except P2KError as exc:
        raise _p2k_err(exc)


# ── live acquisition endpoints ────────────────────────────────────────────────

@router.post("/{device_id}/acquire/{program}")
async def acquire_live(
    device_id: str,
    program: str,
    registry: DeviceRegistry = Depends(get_registry),
) -> dict[str, str]:
    """Trigger an X-ray acquisition for *program* (e.g. ``PANORAMIC``).

    Returns immediately with ``{"status": "acquiring", "program": <program>}``.
    Connect a WebSocket to ``/devices/{device_id}/live`` to receive the image
    blocks as they stream from the device.
    """
    device = registry.get(device_id.upper())
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device {device_id!r} not found")
    if device.state not in (DeviceState.CONNECTED, DeviceState.READY):
        raise HTTPException(
            status_code=409,
            detail=f"Device not ready (state={device.state.name}). Connect first.",
        )
    try:
        await device.async_request_xray(program=program)
    except P2KError as exc:
        raise _p2k_err(exc)
    return {"status": "acquiring", "program": program}


@router.websocket("/{device_id}/live")
async def ws_live(
    device_id: str,
    websocket: WebSocket,
    registry: DeviceRegistry = Depends(get_registry),
) -> None:
    """Stream raw 16-bit grayscale image blocks from an ongoing acquisition.

    After the WebSocket is accepted the handler iterates :meth:`live_images`,
    forwarding each raw-pixel-byte chunk to the client as a binary message.
    The final message is the complete CRC-verified image.

    Close codes:
      1008  — device not found in registry.
      1011  — protocol or device error during streaming.
    """
    device = registry.get(device_id.upper())
    if device is None:
        await websocket.close(code=1008, reason=f"Device {device_id!r} not found")
        return

    await websocket.accept()
    try:
        async for chunk in device.live_images():
            await websocket.send_bytes(chunk)
    except WebSocketDisconnect:
        pass
    except P2KError as exc:
        log.warning("ws_live P2K error for %s: %s", device_id, exc)
        try:
            await websocket.close(code=1011, reason=str(exc))
        except Exception:
            pass
    except Exception as exc:
        log.error("ws_live unexpected error for %s: %s", device_id, exc, exc_info=True)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
