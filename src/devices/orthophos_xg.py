"""
Device driver for the Sirona ORTHOPHOS XG / SL family and related panoramic
and CBCT imaging systems.

Supported device type codes (Netapi114.xml TCPExtInfo variants):

  DX6  (0x0006) — HELIODENT DS        intraoral sensor
  DX7  (0x0007) — HELIODENT VARIO     intraoral sensor, extended
  DX41 (0x0029) — ORTHOPHOS XG        panoramic / cephalometric flat-panel
  DX81P(0x0051) — ORTHOPHOS SL Pan    panoramic flat-panel variant
  DX81C(0x0052) — ORTHOPHOS SL Ceph   panoramic + cephalometric variant
  DX89 (0x0059) — GALILEOS            cone-beam CT (CBCT)
  DX91 (0x005B) — GALILEOS COMFORT    CBCT extended FOV

Confirmed acquisition parameters (from SiPanCtl.dll / Sidexis packet capture):

  Acquisition params (0x0010 – 0x0050)
  Lifetime stats    (0x0100 – 0x010F)  ← confirmed parameter space
  Sensor geometry   (0x0200 – 0x020F)  ← confirmed parameter space

Image transfer sequence (device-push after TCPTrigger):
  1. TCPXRayImgBegin (0x000A)  — header: dimensions, block count, compression
  2. N × TCPXRayImgBlock (0x000B) — pixel data chunks
  3. TCPXRayImgEnd (0x000C)    — status, CRC-32 checksum
"""

from __future__ import annotations

import asyncio
import logging
import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum, unique
from typing import Final

from ..protocol.constants import DeviceType, TCPFuncCode
from ..protocol.tcp import (
    DeviceInfo as TCPDeviceInfo,
    P2KConnectionError,
    P2KDeviceError,
    P2KProtocolError,
    SiNet2Client,
    _dec_ba,
    _dec_dw,
    _dec_w,
    _enc_w,
)
from .base import SironaDevice

log = logging.getLogger(__name__)

# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Protocol constants used by this driver
# ╚══════════════════════════════════════════════════════════════════════════════

# TCP function codes (Netapi114.xml names)
_FC_STATUS: Final[int] = 0x0030       # TCPReqStatus  (not in enum yet)
_FC_TRIGGER: Final[int] = 0x0040      # TCPTrigger
_FC_GET_PARAM: Final[int] = 0x0021    # TCPReqParam
_FC_SET_PARAM: Final[int] = 0x0020    # TCPSetParam

# Image push function codes (device → host, unsolicited after trigger)
_FC_IMG_BEGIN: Final[int] = TCPFuncCode.TCPXRayImgBegin   # 0x000A
_FC_IMG_BLOCK: Final[int] = TCPFuncCode.TCPXRayImgBlock   # 0x000B
_FC_IMG_END: Final[int] = TCPFuncCode.TCPXRayImgEnd       # 0x000C
_FC_PROGRESS: Final[int] = TCPFuncCode.TCPProgressBar      # 0x000D

# Extended-info function codes (mapped via DeviceType)
_EXT_INFO_FC: Final[dict[int, int]] = {
    DeviceType.DX6:   TCPFuncCode.TCPReqExtInfoDX6,     # 0x000E
    DeviceType.DX7:   TCPFuncCode.TCPReqExtInfoDX7,     # 0x000F
    DeviceType.DX41:  TCPFuncCode.TCPReqExtInfoDX41,    # 0x0010
    DeviceType.DX81C: TCPFuncCode.TCPReqExtInfoDX81C,   # 0x0011
    DeviceType.DX81P: TCPFuncCode.TCPReqExtInfoDX81P,   # 0x0012
    DeviceType.DX89:  TCPFuncCode.TCPReqExtInfoDX89,    # 0x0013
    DeviceType.DX91:  TCPFuncCode.TCPReqExtInfoDX91,    # 0x0014
}

# ── Acquisition parameter IDs (observed in Sidexis traffic) ──────────────────
_PARAM_KV: Final[int]             = 0x0010  # tube voltage, integer kV            (W)
_PARAM_MA: Final[int]             = 0x0011  # tube current × 10, e.g. 80 = 8 mA  (W)
_PARAM_EXPOSURE_MS: Final[int]    = 0x0012  # exposure time in milliseconds        (W)
_PARAM_PROGRAM: Final[int]        = 0x0020  # examination program code             (W)
_PARAM_PATIENT_SIZE: Final[int]   = 0x0021  # patient size preset                  (W)
_PARAM_ROTATION_SPEED: Final[int] = 0x0030  # gantry rotation speed (device units) (W)
_PARAM_FOCUS: Final[int]          = 0x0040  # focal spot: 0=large, 1=small         (W)
_PARAM_LAYER: Final[int]          = 0x0050  # tomographic layer width (device units)(W)

# ── Lifetime statistics parameter IDs (read-only) ─────────────────────────────
_PARAM_PAN_XRAY_DOSE: Final[int]   = 0x0100  # panoramic dose, µGy × 10            (DW)
_PARAM_PAN_RECORDINGS: Final[int]  = 0x0101  # total panoramic recording count      (DW)
_PARAM_CEPH_XRAY_DOSE: Final[int]  = 0x0102  # cephalometric dose, µGy × 10        (DW)
_PARAM_CEPH_RECORDINGS: Final[int] = 0x0103  # total cephalometric recording count  (DW)
_PARAM_TOTAL_XRAY_MS: Final[int]   = 0x0104  # cumulative X-ray on-time, ms        (DW)

# ── Sensor geometry parameter IDs (read-only) ─────────────────────────────────
_PARAM_DIE_WIDTH: Final[int]       = 0x0200  # detector die width in pixels         (W)
_PARAM_DIE_HEIGHT: Final[int]      = 0x0201  # detector die height in pixels        (W)
_PARAM_PIXEL_UNIT_SIZE: Final[int] = 0x0202  # physical pixel pitch in µm           (W)

# ── Status codes returned by TCPReqStatus ────────────────────────────────────
_STATUS_READY: Final[int]  = 0x0000
_STATUS_BUSY: Final[int]   = 0x0001
_STATUS_ERROR: Final[int]  = 0x0002
_STATUS_WARMUP: Final[int] = 0x0003

# ── Image compression codes in TCPXRayImgBegin ───────────────────────────────
_COMPRESSION_RAW: Final[int]     = 0
_COMPRESSION_RLE: Final[int]     = 1
_COMPRESSION_DEFLATE: Final[int] = 2


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Enums
# ╚══════════════════════════════════════════════════════════════════════════════

@unique
class ExposureProgram(IntEnum):
    """Examination program codes for :meth:`OrthophosXG.set_program`."""

    PANORAMIC            = 0x01
    CEPHALO_LATERAL      = 0x02
    CEPHALO_FRONTAL      = 0x03
    BITEWING_LEFT        = 0x10
    BITEWING_RIGHT       = 0x11
    BITEWING_BILATERAL   = 0x12

    @property
    def display_name(self) -> str:
        return _PROGRAM_NAMES.get(self, self.name)


_PROGRAM_NAMES: dict[ExposureProgram, str] = {
    ExposureProgram.PANORAMIC:          "Panoramic",
    ExposureProgram.CEPHALO_LATERAL:    "Cephalometric Lateral",
    ExposureProgram.CEPHALO_FRONTAL:    "Cephalometric Frontal",
    ExposureProgram.BITEWING_LEFT:      "Bitewing Left",
    ExposureProgram.BITEWING_RIGHT:     "Bitewing Right",
    ExposureProgram.BITEWING_BILATERAL: "Bitewing Bilateral",
}


@unique
class PatientSize(IntEnum):
    """Patient size presets for automatic kV / mA selection."""

    CHILD   = 0x00
    ADULT_S = 0x01
    ADULT_M = 0x02
    ADULT_L = 0x03


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Dataclasses
# ╚══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class ExposureParams:
    """Snapshot of the current exposure parameter set.

    Retrieved by :meth:`OrthophosXG.get_exposure_params` or supplied to
    :meth:`OrthophosXG.set_exposure_params`.
    """

    kv: int
    """Tube voltage in kilovolts.  Typical range: 60–90 kV."""

    ma_tenths: int
    """Tube current in tenths of milliamps.  E.g. ``80`` → 8.0 mA."""

    exposure_ms: int
    """Exposure duration in milliseconds."""

    program: ExposureProgram
    """Active examination program."""

    patient_size: PatientSize
    """Patient size preset used for auto exposure."""

    rotation_speed: int
    """Gantry rotation speed in device-native units."""

    focus: int
    """Focal spot selection: 0 = large, 1 = small."""

    layer: int
    """Tomographic layer width in device-native units."""

    @property
    def ma(self) -> float:
        """Tube current in milliamps (``ma_tenths / 10``)."""
        return self.ma_tenths / 10.0


@dataclass(frozen=True, slots=True)
class LifetimeStats:
    """Cumulative dose and recording counters from the device's NVRAM.

    All dose values are stored with one decimal place of precision:
    ``pan_xray_dose_ugy10 / 10`` gives µGy.

    Retrieved by :meth:`OrthophosXG.get_lifetime_stats`.
    """

    pan_xray_dose_ugy10: int
    """Total panoramic X-ray dose in µGy × 10.  Divide by 10 for µGy."""

    no_of_pan_recordings: int
    """Total number of panoramic exposures taken over the device lifetime."""

    ceph_xray_dose_ugy10: int
    """Total cephalometric X-ray dose in µGy × 10."""

    no_of_ceph_recordings: int
    """Total number of cephalometric exposures."""

    total_xray_ms: int
    """Cumulative X-ray tube on-time in milliseconds."""

    @property
    def pan_dose_ugy(self) -> float:
        """Panoramic dose in µGy (float)."""
        return self.pan_xray_dose_ugy10 / 10.0

    @property
    def ceph_dose_ugy(self) -> float:
        """Cephalometric dose in µGy (float)."""
        return self.ceph_xray_dose_ugy10 / 10.0

    @property
    def total_xray_s(self) -> float:
        """Cumulative X-ray on-time in seconds (float)."""
        return self.total_xray_ms / 1000.0

    def to_dict(self) -> dict[str, object]:
        return {
            "pan_xray_dose_ugy10":   self.pan_xray_dose_ugy10,
            "no_of_pan_recordings":  self.no_of_pan_recordings,
            "ceph_xray_dose_ugy10":  self.ceph_xray_dose_ugy10,
            "no_of_ceph_recordings": self.no_of_ceph_recordings,
            "total_xray_ms":         self.total_xray_ms,
            "pan_dose_ugy":          self.pan_dose_ugy,
            "ceph_dose_ugy":         self.ceph_dose_ugy,
            "total_xray_s":          self.total_xray_s,
        }


@dataclass(frozen=True, slots=True)
class SensorGeometry:
    """Physical detector geometry for the active imaging detector.

    Retrieved by :meth:`OrthophosXG.get_sensor_geometry`.
    """

    die_width: int
    """Detector die width in pixels."""

    die_height: int
    """Detector die height in pixels."""

    pixel_unit_size: int
    """Physical pixel pitch in micrometres (µm).  E.g. 127 → 127 µm pitch."""

    @property
    def width_mm(self) -> float:
        """Detector active area width in mm."""
        return self.die_width * self.pixel_unit_size / 1000.0

    @property
    def height_mm(self) -> float:
        """Detector active area height in mm."""
        return self.die_height * self.pixel_unit_size / 1000.0

    def to_dict(self) -> dict[str, object]:
        return {
            "die_width":       self.die_width,
            "die_height":      self.die_height,
            "pixel_unit_size": self.pixel_unit_size,
            "width_mm":        self.width_mm,
            "height_mm":       self.height_mm,
        }


@dataclass(frozen=True, slots=True)
class _ImageHeader:
    """Decoded TCPXRayImgBegin (0x000A) payload."""

    image_id: int
    total_blocks: int
    total_bytes: int
    width: int
    height: int
    bit_depth: int
    compression: int

    @classmethod
    def decode(cls, payload: bytes) -> "_ImageHeader":
        off = 0
        image_id,     n = _dec_w(payload, off);  off += n
        total_blocks, n = _dec_w(payload, off);  off += n
        total_bytes,  n = _dec_dw(payload, off); off += n
        width,        n = _dec_w(payload, off);  off += n
        height,       n = _dec_w(payload, off);  off += n
        bit_depth,    n = _dec_w(payload, off);  off += n
        compression,  n = _dec_w(payload, off);  off += n
        return cls(
            image_id=image_id,
            total_blocks=total_blocks,
            total_bytes=total_bytes,
            width=width,
            height=height,
            bit_depth=bit_depth,
            compression=compression,
        )


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  OrthophosXG
# ╚══════════════════════════════════════════════════════════════════════════════

class OrthophosXG(SironaDevice):
    """Async driver for the Sirona ORTHOPHOS XG / SL panoramic platform.

    Handles all device variants that share the XG-generation detector and
    P2K firmware: DX6, DX7, DX41, DX81P, DX81C, DX89, DX91.

    Example — basic acquisition::

        async with OrthophosXG(ip="192.168.1.50") as xg:
            info = await xg.get_info()
            print(info.display())

            await xg.set_program(ExposureProgram.PANORAMIC)
            await xg.set_patient_size(PatientSize.ADULT_M)
            image_bytes = await xg.request_pan_image()

    Example — from discovery::

        async with SiNet2Discovery() as disc:
            for announce in await disc.discover():
                if announce.device_type in OrthophosXG.SUPPORTED_TYPES:
                    xg = OrthophosXG.from_announce(announce)
                    async with xg:
                        stats = await xg.get_lifetime_stats()
    """

    SUPPORTED_TYPES: frozenset[int] = frozenset({
        DeviceType.DX6,    # HELIODENT DS
        DeviceType.DX7,    # HELIODENT VARIO
        DeviceType.DX41,   # ORTHOPHOS XG
        DeviceType.DX81P,  # ORTHOPHOS SL (Pan)
        DeviceType.DX81C,  # ORTHOPHOS SL (Ceph)
        DeviceType.DX89,   # GALILEOS
        DeviceType.DX91,   # GALILEOS COMFORT
    })

    def __init__(
        self,
        ip: str,
        port: int = 1999,
        mac: str = "",
        device_type: int = DeviceType.DX41,
        connect_timeout: float = 10.0,
        io_timeout: float = 30.0,
        image_timeout: float = 120.0,
    ) -> None:
        super().__init__(ip=ip, port=port, mac=mac, device_type=device_type)
        self._connect_timeout = connect_timeout
        self._io_timeout = io_timeout
        self._image_timeout = image_timeout

    # ── SironaDevice abstract implementations ─────────────────────────────────

    async def async_connect(self) -> None:
        """Open a P2K TCP session to the device.

        Creates a new :class:`~purexs.protocol.tcp.SiNet2Client`, performs the
        TCP three-way handshake, and completes the P2K TCPConnect handshake.
        The assigned ``session_id`` is retained in the client for subsequent frames.

        Raises:
            P2KConnectionError: TCP connect failed, timed out, or refused.
            P2KDeviceError:     Device sent TCPError during handshake.
        """
        if self.is_connected:
            log.debug("connect() called on already-connected %s — skipping", self._ip)
            return

        client = SiNet2Client(
            connect_timeout=self._connect_timeout,
            io_timeout=self._io_timeout,
        )
        await client.connect(self._ip, self._port)
        self._client = client
        log.info("OrthophosXG connected  ip=%s  type=0x%04X", self._ip, self._device_type)

    async def async_disconnect(self) -> None:
        """Send TCPDisconnect and close the stream."""
        if self._client is None:
            return
        await self._client.close()
        self._client = None
        log.info("OrthophosXG disconnected  ip=%s", self._ip)

    async def async_get_info(self) -> TCPDeviceInfo:
        """Send TCPReqInfo and populate :attr:`serial_number` / :attr:`firmware_version`.

        Also sends the device-type-specific ``TCPReqExtInfo`` command if
        supported, and logs the extended parameters at DEBUG level.

        Returns:
            :class:`~purexs.protocol.tcp.DeviceInfo` with firmware, serial,
            device type, and hardware revision.
        """
        self._require_connected()
        assert self._client is not None

        info = await self._client.request_info()
        self._serial_number = info.serial_number
        self._firmware_version = info.firmware_version
        log.info(
            "Device info: %s  type=0x%04X  hwrev=%d",
            info.display(), info.device_type, info.hardware_rev,
        )

        # Request device-type-specific extended info if we know the variant.
        ext_fc = _EXT_INFO_FC.get(self._device_type)
        if ext_fc is not None:
            try:
                await self._fetch_ext_info(ext_fc)
            except (P2KDeviceError, P2KProtocolError) as exc:
                log.debug("TCPReqExtInfo skipped for 0x%04X: %s", self._device_type, exc)

        return info

    async def async_request_xray(self, **kwargs: object) -> bytes:
        """Trigger an X-ray exposure and return raw image bytes.

        Delegates to :meth:`request_pan_image`.  Pass ``program`` to override
        the examination program::

            raw = await xg.request_xray(program=ExposureProgram.CEPHALO_LATERAL)
        """
        program = kwargs.get("program")
        if program is not None:
            await self.set_program(ExposureProgram(int(program)))  # type: ignore[arg-type]
        return await self.request_pan_image()

    # ── exposure parameter read/write ─────────────────────────────────────────

    async def get_exposure_params(self) -> ExposureParams:
        """Read all eight exposure parameters in a single batch.

        Raises:
            P2KConnectionError: Not connected.
            P2KDeviceError:     Device error on any parameter read.
        """
        kv             = await self._read_w(_PARAM_KV)
        ma_tenths      = await self._read_w(_PARAM_MA)
        exposure_ms    = await self._read_w(_PARAM_EXPOSURE_MS)
        program_raw    = await self._read_w(_PARAM_PROGRAM)
        patient_raw    = await self._read_w(_PARAM_PATIENT_SIZE)
        rotation_speed = await self._read_w(_PARAM_ROTATION_SPEED)
        focus          = await self._read_w(_PARAM_FOCUS)
        layer          = await self._read_w(_PARAM_LAYER)

        return ExposureParams(
            kv=kv,
            ma_tenths=ma_tenths,
            exposure_ms=exposure_ms,
            program=ExposureProgram(program_raw),
            patient_size=PatientSize(patient_raw),
            rotation_speed=rotation_speed,
            focus=focus,
            layer=layer,
        )

    async def set_exposure_params(self, params: ExposureParams) -> None:
        """Write all eight exposure parameters from an :class:`ExposureParams` snapshot."""
        await self.set_kv(params.kv)
        await self.set_ma(params.ma_tenths)
        await self._write_w(_PARAM_EXPOSURE_MS, params.exposure_ms)
        await self.set_program(params.program)
        await self.set_patient_size(params.patient_size)
        await self._write_w(_PARAM_ROTATION_SPEED, params.rotation_speed)
        await self._write_w(_PARAM_FOCUS, params.focus)
        await self._write_w(_PARAM_LAYER, params.layer)

    async def set_kv(self, kv: int) -> None:
        """Set tube voltage.

        Args:
            kv: Kilovolts.  Accepted range: 60–90.

        Raises:
            ValueError: *kv* is outside [60, 90].
        """
        if not 60 <= kv <= 90:
            raise ValueError(f"kV out of range [60, 90]: {kv}")
        await self._write_w(_PARAM_KV, kv)
        log.debug("kV set to %d", kv)

    async def get_kv(self) -> int:
        """Read the current tube voltage setting in kV."""
        return await self._read_w(_PARAM_KV)

    async def set_ma(self, ma_tenths: int) -> None:
        """Set tube current.

        Args:
            ma_tenths: Current in tenths of mA.  E.g. ``80`` → 8.0 mA.
        """
        await self._write_w(_PARAM_MA, ma_tenths)
        log.debug("mA × 10 set to %d (%.1f mA)", ma_tenths, ma_tenths / 10.0)

    async def get_ma(self) -> int:
        """Read the current tube current setting (tenths of mA)."""
        return await self._read_w(_PARAM_MA)

    async def set_program(self, program: ExposureProgram) -> None:
        """Set the active examination program."""
        await self._write_w(_PARAM_PROGRAM, program.value)
        log.debug("Program set to %s (0x%02X)", program.display_name, program.value)

    async def get_program(self) -> ExposureProgram:
        """Read the active examination program."""
        raw = await self._read_w(_PARAM_PROGRAM)
        return ExposureProgram(raw)

    async def set_patient_size(self, size: PatientSize) -> None:
        """Set the patient size preset."""
        await self._write_w(_PARAM_PATIENT_SIZE, size.value)
        log.debug("Patient size set to %s", size.name)

    async def get_patient_size(self) -> PatientSize:
        """Read the current patient size preset."""
        raw = await self._read_w(_PARAM_PATIENT_SIZE)
        return PatientSize(raw)

    # ── PAN / image acquisition ───────────────────────────────────────────────

    async def request_pan_image(self) -> bytes:
        """Trigger an exposure and receive the raw panoramic image.

        Sends a TCPTrigger (0x0040) command.  After the ACK the device
        begins pushing the image data stream:

        ::

            → TCPTrigger
            ← TCPAck
            ← TCPXRayImgBegin    (dimensions, block count, compression)
            ← TCPXRayImgBlock ×N (pixel data chunks)
            ← TCPXRayImgEnd      (status, CRC-32)

        Each block is reassembled in order and the final CRC-32 is verified
        against the uncompressed image buffer.

        Returns:
            Raw uncompressed image bytes (row-major, 16-bit big-endian pixels
            for standard panoramic mode).

        Raises:
            P2KConnectionError: Not connected or stream closed during transfer.
            P2KDeviceError:     Device reported acquisition error in TCPXRayImgEnd.
            P2KProtocolError:   Unexpected frame sequence or bad checksum.
        """
        self._require_connected()
        assert self._client is not None

        # Verify device is ready before committing to an exposure.
        status = await self._read_status()
        if status != _STATUS_READY:
            status_names = {
                _STATUS_BUSY: "BUSY",
                _STATUS_ERROR: "ERROR",
                _STATUS_WARMUP: "WARMUP",
            }
            raise P2KDeviceError(
                status,
                f"Device not ready for exposure (status={status_names.get(status, hex(status))})"
            )

        # Send trigger; device ACKs and then pushes the image stream.
        await self._client.send(_FC_TRIGGER, b"")
        log.info("Trigger sent to %s; waiting for image stream …", self._ip)

        try:
            async with asyncio.timeout(self._image_timeout):
                return await self._receive_image_stream()
        except TimeoutError as exc:
            raise P2KConnectionError(
                f"Image transfer from {self._ip} timed out "
                f"({self._image_timeout:.0f}s)"
            ) from exc

    # ── lifetime statistics ───────────────────────────────────────────────────

    async def get_lifetime_stats(self) -> LifetimeStats:
        """Read cumulative dose and recording counters from the device's NVRAM.

        Parameters are read individually via FUNC_GET_PARAM (0x0021).

        Returns:
            :class:`LifetimeStats` with dose and count fields.

        Raises:
            P2KConnectionError: Not connected.
            P2KDeviceError:     Any parameter read fails.
        """
        pan_dose      = await self._read_dw(_PARAM_PAN_XRAY_DOSE)
        pan_count     = await self._read_dw(_PARAM_PAN_RECORDINGS)
        ceph_dose     = await self._read_dw(_PARAM_CEPH_XRAY_DOSE)
        ceph_count    = await self._read_dw(_PARAM_CEPH_RECORDINGS)
        total_xray_ms = await self._read_dw(_PARAM_TOTAL_XRAY_MS)

        stats = LifetimeStats(
            pan_xray_dose_ugy10=pan_dose,
            no_of_pan_recordings=pan_count,
            ceph_xray_dose_ugy10=ceph_dose,
            no_of_ceph_recordings=ceph_count,
            total_xray_ms=total_xray_ms,
        )
        log.debug(
            "LifetimeStats: pan=%.1fµGy (%d shots)  ceph=%.1fµGy (%d shots)  "
            "total_xray=%.1fs",
            stats.pan_dose_ugy, stats.no_of_pan_recordings,
            stats.ceph_dose_ugy, stats.no_of_ceph_recordings,
            stats.total_xray_s,
        )
        return stats

    # ── sensor geometry ───────────────────────────────────────────────────────

    async def get_sensor_geometry(self) -> SensorGeometry:
        """Read the physical detector geometry.

        Returns:
            :class:`SensorGeometry` with die dimensions and pixel pitch.

        Raises:
            P2KConnectionError: Not connected.
            P2KDeviceError:     Any parameter read fails.
        """
        die_width       = await self._read_w(_PARAM_DIE_WIDTH)
        die_height      = await self._read_w(_PARAM_DIE_HEIGHT)
        pixel_unit_size = await self._read_w(_PARAM_PIXEL_UNIT_SIZE)

        geom = SensorGeometry(
            die_width=die_width,
            die_height=die_height,
            pixel_unit_size=pixel_unit_size,
        )
        log.debug(
            "SensorGeometry: %d×%d px  pitch=%dµm  area=%.1f×%.1fmm",
            geom.die_width, geom.die_height, geom.pixel_unit_size,
            geom.width_mm, geom.height_mm,
        )
        return geom

    # ── image stream receiver ─────────────────────────────────────────────────

    async def _receive_image_stream(self) -> bytes:
        """Reassemble and verify the TCPXRayImgBegin/Block/End sequence.

        After the trigger ACK the device pushes frames without being asked.
        We call :meth:`~purexs.devices.base.SironaDevice._pop_frame` for
        each one.  TCPProgressBar frames (0x000D) are silently consumed so
        callers do not have to filter them.

        Returns:
            Uncompressed image bytes (decompressed if needed).

        Raises:
            P2KDeviceError:  Device reported a non-zero status in TCPXRayImgEnd.
            P2KProtocolError: Image ID mismatch, wrong frame order, or bad CRC.
        """
        # ── 1. TCPXRayImgBegin ────────────────────────────────────────────────
        img_hdr = await self._expect_frame(_FC_IMG_BEGIN, "TCPXRayImgBegin")
        begin = _ImageHeader.decode(img_hdr)
        log.info(
            "Image begin: id=%d  %d×%d  depth=%d  blocks=%d  compression=%d",
            begin.image_id, begin.width, begin.height,
            begin.bit_depth, begin.total_blocks, begin.compression,
        )

        # ── 2. TCPXRayImgBlock × N ────────────────────────────────────────────
        blocks: dict[int, bytes] = {}
        while len(blocks) < begin.total_blocks:
            func_code, payload = await self._pop_frame()

            if func_code == _FC_PROGRESS:
                # TCPProgressBar — consume and log, then wait for next block.
                self._log_progress(payload)
                continue

            if func_code != _FC_IMG_BLOCK:
                raise P2KProtocolError(
                    f"Expected TCPXRayImgBlock (0x{_FC_IMG_BLOCK:04X}) "
                    f"or TCPProgressBar, got 0x{func_code:04X}"
                )

            off = 0
            block_image_id, n = _dec_w(payload, off); off += n
            block_index,    n = _dec_w(payload, off); off += n
            block_data,     n = _dec_ba(payload, off); off += n

            if block_image_id != begin.image_id:
                raise P2KProtocolError(
                    f"Block image_id mismatch: got {block_image_id}, "
                    f"expected {begin.image_id}"
                )
            blocks[block_index] = block_data
            log.debug(
                "Block %d/%d  %d bytes", block_index + 1, begin.total_blocks, len(block_data)
            )

        # ── 3. TCPXRayImgEnd ──────────────────────────────────────────────────
        end_payload = await self._expect_frame(_FC_IMG_END, "TCPXRayImgEnd")
        off = 0
        end_image_id, n = _dec_w(end_payload, off); off += n
        status,       n = _dec_w(end_payload, off); off += n
        checksum,     n = _dec_dw(end_payload, off); off += n

        if end_image_id != begin.image_id:
            raise P2KProtocolError(
                f"End image_id mismatch: got {end_image_id}, expected {begin.image_id}"
            )
        if status != 0x0000:
            raise P2KDeviceError(
                status,
                f"Device reported acquisition error in TCPXRayImgEnd (status=0x{status:04X})"
            )

        # ── 4. Reassemble in block order ──────────────────────────────────────
        raw = b"".join(blocks[i] for i in sorted(blocks))

        # ── 5. Decompress if needed ───────────────────────────────────────────
        if begin.compression == _COMPRESSION_DEFLATE:
            try:
                raw = zlib.decompress(raw)
            except zlib.error as exc:
                raise P2KProtocolError(f"Image decompression failed: {exc}") from exc
        elif begin.compression == _COMPRESSION_RLE:
            raw = _rle_decompress(raw)

        # ── 6. CRC-32 verification ────────────────────────────────────────────
        actual_crc = zlib.crc32(raw) & 0xFFFFFFFF
        if actual_crc != checksum:
            raise P2KProtocolError(
                f"Image CRC-32 mismatch: computed 0x{actual_crc:08X}, "
                f"device reported 0x{checksum:08X}"
            )

        log.info(
            "Image transfer complete: %d bytes  CRC OK (0x%08X)", len(raw), checksum
        )
        return raw

    async def _expect_frame(self, expected_fc: int, name: str) -> bytes:
        """Pop one frame, consuming TCPProgressBar frames until the expected one arrives."""
        while True:
            func_code, payload = await self._pop_frame()
            if func_code == _FC_PROGRESS:
                self._log_progress(payload)
                continue
            if func_code != expected_fc:
                raise P2KProtocolError(
                    f"Expected {name} (0x{expected_fc:04X}), got 0x{func_code:04X}"
                )
            return payload

    @staticmethod
    def _log_progress(payload: bytes) -> None:
        """Decode and log a TCPProgressBar frame at DEBUG level."""
        if len(payload) < 6:
            return
        off = 0
        phase,    n = _dec_w(payload, off); off += n
        percent,  n = _dec_w(payload, off); off += n
        remain_ds, n = _dec_w(payload, off); off += n
        phase_names = {0: "positioning", 1: "pre-exposure", 2: "exposure", 3: "readout"}
        log.debug(
            "Progress: phase=%s  %d%%  %.1fs remaining",
            phase_names.get(phase, str(phase)),
            percent,
            remain_ds / 10.0,
        )

    # ── extended info ─────────────────────────────────────────────────────────

    async def _fetch_ext_info(self, fc: int) -> None:
        """Send a TCPReqExtInfoDX* command and log the response at DEBUG level."""
        assert self._client is not None
        _, payload = await self._client._exchange(fc, b"")  # noqa: SLF001
        log.debug(
            "TCPReqExtInfo 0x%04X → %d bytes: %s",
            fc, len(payload), payload.hex(),
        )

    # ── status query ──────────────────────────────────────────────────────────

    async def _read_status(self) -> int:
        """Read the device's ready state via TCPReqStatus (0x0030).

        Returns the raw status WORD: 0x0000 = READY.
        """
        assert self._client is not None
        payload = await self._client.send(_FC_STATUS, b"")
        if len(payload) < 2:
            return _STATUS_ERROR
        val, _ = _dec_w(payload, 0)
        return val

    # ── parameter helpers ─────────────────────────────────────────────────────

    async def _read_w(self, param_id: int) -> int:
        """Read a W (2-byte WORD) parameter and return its integer value."""
        assert self._client is not None
        # Request payload: param_id encoded as W
        payload = await self._client.send(_FC_GET_PARAM, _enc_w(param_id))
        # Response payload: param_id (W, echoed) + value (W)
        if len(payload) < 4:
            raise P2KProtocolError(
                f"FUNC_GET_PARAM 0x{param_id:04X}: response payload too short "
                f"({len(payload)} bytes, expected ≥4)"
            )
        val, _ = _dec_w(payload, 2)   # skip echoed param_id at offset 0
        return val

    async def _read_dw(self, param_id: int) -> int:
        """Read a DW (4-byte DWORD) parameter and return its integer value."""
        assert self._client is not None
        payload = await self._client.send(_FC_GET_PARAM, _enc_w(param_id))
        # Response: param_id (W, echoed) + value (DW)
        if len(payload) < 6:
            raise P2KProtocolError(
                f"FUNC_GET_PARAM 0x{param_id:04X}: response too short for DW "
                f"({len(payload)} bytes, expected ≥6)"
            )
        val, _ = _dec_dw(payload, 2)
        return val

    async def _write_w(self, param_id: int, value: int) -> None:
        """Write a W (2-byte WORD) parameter."""
        assert self._client is not None
        await self._client.send(_FC_SET_PARAM, _enc_w(param_id) + _enc_w(value))

    async def _write_dw(self, param_id: int, value: int) -> None:
        """Write a DW (4-byte DWORD) parameter."""
        assert self._client is not None
        payload = _enc_w(param_id) + struct.pack(">I", value)
        await self._client.send(_FC_SET_PARAM, payload)


# ── RLE decompressor ──────────────────────────────────────────────────────────

def _rle_decompress(data: bytes) -> bytes:
    """Minimal RLE decompressor for P2K block-RLE image data.

    Encoding: pairs of (count: uint8, value: uint8).
    ``count = 0`` is a literal run of 256 copies.
    """
    out = bytearray()
    i = 0
    while i + 1 < len(data):
        count = data[i] or 256
        value = data[i + 1]
        out.extend(bytes([value]) * count)
        i += 2
    return bytes(out)
