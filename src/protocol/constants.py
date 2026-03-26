"""
SiNet2 / P2K (Protocol 2000) wire constants.

Sources:
  - SiNet2.dll      — UDP frame layout, magic, function codes, field order
  - SiPanCtl.dll    — Device type codes, parameter IDs
  - Netapi114.xml   — TCP function code names (TCPReqInfo … TCPReqExtInfoDX*),
                       TCPExtInfo device variant names (DX41, DX6 … DX91)

All numeric values have been confirmed by packet capture or binary analysis
unless marked with ``# inferred``.
"""

from __future__ import annotations

from enum import IntEnum, unique

# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Scalar constants  (kept for backward-compat with packets.py / tcp.py)
# ╚══════════════════════════════════════════════════════════════════════════════

MAGIC: int = 0x072D
"""Two-byte magic word that opens every SiNet2 frame.

Stored little-endian (bytes ``2D 07``) in the UDP header;
stored as the low 16 bits of a big-endian DWORD (``00 00 07 2D``) in TCP.
"""

DEFAULT_PORT: int = 1999
"""Default TCP *and* UDP port for all P2K devices."""

DISCOVERY_LISTEN_PORT: int = 55999
"""High, unprivileged local port used to receive broadcast replies.

Sidexis binds 1999 exclusively on Windows, so we receive on a different
port and send probes that appear to originate there.
"""

UDP_HEADER_SIZE: int = 18
"""UDP frame header size in bytes (9 × little-endian WORD)."""

TCP_HEADER_SIZE: int = 20
"""TCP frame header size in bytes (big-endian: DWORD + WORD + WORD + DWORD + DWORD + DWORD)."""

API_VERSION: int = 0x0001
"""Protocol API version placed in every frame header."""

# Legacy bare function-code names — tcp.py and packets.py import these.
# They alias the enum members below so both spellings work.
FUNC_DISCOVER: int = 0x8000
FUNC_CONNECT: int = 0x0001
FUNC_DISCONNECT: int = 0x0002
FUNC_GET_IMAGE: int = 0x0010
FUNC_SET_PARAM: int = 0x0020
FUNC_GET_PARAM: int = 0x0021
FUNC_STATUS: int = 0x0030
FUNC_TRIGGER: int = 0x0040
FUNC_ACK: int = 0xFF00
FUNC_ERROR: int = 0xFF01

# ── Payload field type tags ───────────────────────────────────────────────────
FIELD_S = "S"    # 4-byte BE DWORD char-count  + UTF-16LE chars
FIELD_BA = "BA"  # 2-byte BE WORD  byte-count  + raw bytes
FIELD_W = "W"    # 2-byte BE WORD  scalar
FIELD_DW = "DW"  # 4-byte BE DWORD scalar
FIELD_B = "B"    # 1-byte unsigned scalar


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  UDPFuncCode
# ╚══════════════════════════════════════════════════════════════════════════════

@unique
class UDPFuncCode(IntEnum):
    """Function codes carried in the UDP frame header (``func_code`` field).

    All values occupy the high-byte range ``0x80xx``, distinguishing them
    from TCP function codes.  The host sets ``func_code`` in its probe;
    the device echoes the same code in its reply.

    Wire position: offset ``+0x04``, little-endian WORD.
    """

    DISCOVER = 0x8000
    """Broadcast discovery probe (host → all) / unicast reply (device → host).

    Payload fields (device reply, big-endian):
      1. NameP2K           S   — TCP port as decimal ASCII in UTF-16LE
      2. DefGatewayAddress BA4 — default gateway IPv4
      3. SubNetMask        BA4 — subnet mask
      4. IpAddress         BA4 — device IPv4 address
      5. EthernetAddress   BA6 — MAC address
      6. ConfigTime        W   — opaque configuration timestamp
      7. DeviceType        W   — :class:`DeviceType` value
    """

    DISCOVER_ACK = 0x8001
    """Explicit positive acknowledgement to a DISCOVER probe.

    Sent by some firmware variants in addition to the DISCOVER echo.
    Payload layout mirrors DISCOVER.
    """

    SET_NETWORK_CONFIG = 0x8002
    """Write IP address, subnet mask, and default gateway to the device.

    Payload fields (host → device, big-endian):
      1. IpAddress         BA4
      2. SubNetMask        BA4
      3. DefGatewayAddress BA4
    """

    SET_NETWORK_CONFIG_ACK = 0x8003
    """Acknowledgement after SET_NETWORK_CONFIG is applied.  Empty payload."""

    RESET = 0x8004
    """Soft-reset the device (reboot into normal firmware).  Empty payload."""

    RESET_ACK = 0x8005
    """Acknowledgement that the device has accepted the reset command."""

    IDENTIFY = 0x8006
    """Blink LED / emit audible tone to physically identify the unit.

    Payload: W — duration in seconds (0 = stop).
    """

    IDENTIFY_ACK = 0x8007
    """Acknowledgement to IDENTIFY.  Empty payload."""

    HEARTBEAT = 0x8008
    """Periodic UDP keepalive broadcast.

    Devices that have not received a DISCOVER or HEARTBEAT within a
    configurable window will enter an autonomous safe-stop state.
    Payload: W — interval hint in seconds.
    """


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  DeviceType
# ╚══════════════════════════════════════════════════════════════════════════════

@unique
class DeviceType(IntEnum):
    """Device type codes carried in the UDP discovery response payload.

    Values are 2-byte big-endian WORDs (field 7 of the discovery payload).
    Enum member names use the internal variant identifiers from
    ``Netapi114.xml`` ``<TCPExtInfo>`` elements; the human-readable product
    names appear in the docstring of each member.

    The numeric value encodes the decimal model number directly in hex
    (e.g. model 41 → 0x0029, model 89 → 0x0059), which matches the
    pattern observed in ``SiPanCtl.dll``.
    """

    # ── single-digit sensor families ─────────────────────────────────────────

    DX6 = 0x0006
    """HELIODENT DS — direct digital intraoral sensor, 6th-generation module.

    Compact USB/Ethernet sensor for periapical and bitewing images.
    Connects to Sidexis via P2K TCP; image frames returned as raw 16-bit
    grayscale, 1944 × 1380 px (nominal).
    """

    DX7 = 0x0007
    """HELIODENT VARIO — updated intraoral sensor with variable geometry.

    Extended DX6 firmware; adds automatic size detection and a wider
    dynamic range at 14-bit depth.
    """

    # ── 40-series panoramic flat-panel ───────────────────────────────────────

    DX41 = 0x0029  # 41 decimal
    """ORTHOPHOS XG — panoramic / cephalometric flat-panel system.

    Primary detector for the ORTHOPHOS XG 5 and XG 3D product lines.
    Pixel pitch: 127 µm.  Image size: 2440 × 1292 px (pan mode).
    Reports DeviceType 0x0029 during UDP discovery.
    """

    # ── 80-series panoramic variants ─────────────────────────────────────────

    DX81P = 0x0051  # 81 decimal
    """ORTHOPHOS SL — panoramic-only flat-panel detector variant.

    Same physical panel as DX81C but firmware-configured for panoramic
    programs only (no cephalometric arm).  Variant suffix 'P' = *Pan*.
    """

    DX81C = 0x0052  # 81 decimal + 1 for Ceph variant
    """ORTHOPHOS SL (ceph) — panoramic + cephalometric flat-panel variant.

    Adds a lateral/frontal cephalometric program set to the DX81P base.
    Variant suffix 'C' = *Ceph*.  Reports 0x0052 so the host can select
    the correct ``TCPReqExtInfoDX81C`` function code.
    """

    # ── 89-series CBCT ───────────────────────────────────────────────────────

    DX89 = 0x0059  # 89 decimal
    """GALILEOS — cone-beam CT (CBCT) volumetric imaging system.

    Single-rotation 3D acquisition; isotropic voxel size 0.3 mm.
    Image data returned as a compressed volume blob via multiple
    TCPXRayImgBlock frames.  Reports DeviceType 0x0059.
    """

    # ── 91-series CBCT ───────────────────────────────────────────────────────

    DX91 = 0x005B  # 91 decimal
    """GALILEOS COMFORT / Comfort plus — upgraded GALILEOS variant.

    Extended FOV and faster reconstruction vs. DX89.  Uses the same
    TCPXRayImg* transfer sequence but a different extended-info layout
    (``TCPReqExtInfoDX91``).  Reports DeviceType 0x005B.
    """

    # ── synthetic / host-side codes ──────────────────────────────────────────

    SIDEXIS_SERVER = 0x0020
    """Sidexis host software identifying itself on the broadcast network."""

    UNKNOWN = 0xFFFF
    """Catch-all for firmware revisions not yet mapped."""

    @classmethod
    def _missing_(cls, value: object) -> "DeviceType":
        """Return UNKNOWN for any unrecognised WORD value."""
        return cls.UNKNOWN

    @property
    def display_name(self) -> str:
        """Human-readable product marketing name."""
        return _DEVICE_DISPLAY_NAMES.get(self, self.name)


_DEVICE_DISPLAY_NAMES: dict[DeviceType, str] = {
    DeviceType.DX6:            "HELIODENT DS",
    DeviceType.DX7:            "HELIODENT VARIO",
    DeviceType.DX41:           "ORTHOPHOS XG",
    DeviceType.DX81P:          "ORTHOPHOS SL (Pan)",
    DeviceType.DX81C:          "ORTHOPHOS SL (Ceph)",
    DeviceType.DX89:           "GALILEOS",
    DeviceType.DX91:           "GALILEOS COMFORT",
    DeviceType.SIDEXIS_SERVER: "SIDEXIS SERVER",
    DeviceType.UNKNOWN:        "UNKNOWN",
}

# Backward-compat plain dict used by registry.py and the old API layer.
DEVICE_TYPES: dict[int, str] = {dt.value: dt.display_name for dt in DeviceType}


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  TCPFuncCode
# ╚══════════════════════════════════════════════════════════════════════════════

@unique
class TCPFuncCode(IntEnum):
    """TCP function codes from ``Netapi114.xml``.

    Every TCP frame carries a ``func_code`` WORD at header offset ``+0x04``.
    Values ``0x0001``–``0x00FF`` are command/data codes; ``0xFF00``–``0xFFFF``
    are generic response codes.

    Naming follows the XML element names exactly so that ``Netapi114.xml``
    can be re-parsed and diffed against this enum without renaming.

    The *Req* / non-*Req* pairing convention:
      - ``TCPReqXxx`` — sent **host → device** (request)
      - ``TCPXxx``    — sent **device → host** (response / push)
    """

    # ── session lifecycle (must stay at 0x0001/0x0002 for legacy compat) ─────

    TCPConnect = 0x0001
    """Open a P2K session.

    Host → device.  No payload.
    Device replies with ``TCPConnect`` echo carrying an assigned
    ``session_id`` in the header; subsequent frames must echo that ID.
    """

    TCPDisconnect = 0x0002
    """Close the current P2K session.

    Host → device.  No payload.  Device ACKs then drops the connection.
    """

    # ── device info exchange ──────────────────────────────────────────────────

    TCPReqInfo = 0x0003
    """Request static device information.

    Host → device.  No payload.
    Reply: :attr:`TCPInfo`.
    """

    TCPInfo = 0x0004
    """Static device information response.

    Device → host.  Payload fields (big-endian):
      - FirmwareVersion  S  — e.g. ``"2.14.0"``
      - SerialNumber     S  — factory serial string
      - DeviceType       W  — :class:`DeviceType` value
      - HardwareRev      W  — PCB revision
    """

    TCPReqDevCaps = 0x0005
    """Request device capability flags.

    Host → device.  No payload.  Reply: :attr:`TCPDevCaps`.
    """

    TCPDevCaps = 0x0006
    """Device capability flags response.

    Device → host.  Payload: DW capability bitmask.

    Bit definitions (LSB first):
      bit 0  — panoramic mode supported
      bit 1  — cephalometric mode supported
      bit 2  — CBCT / 3D mode supported
      bit 3  — intraoral mode supported
      bit 4  — automatic exposure control (AEC) available
      bit 5  — detector calibration required before first use
      bit 6  — remote trigger supported
      bits 7–31 reserved
    """

    TCPReqNetWorkConfig = 0x0007
    """Request current network configuration from the device.

    Host → device.  No payload.  Reply: :attr:`TCPNetWorkConfig`.
    """

    TCPNetWorkConfig = 0x0008
    """Network configuration response.

    Device → host.  Payload fields (big-endian):
      1. IpAddress         BA4
      2. SubNetMask        BA4
      3. DefGatewayAddress BA4
      4. TCPPort           W   — active P2K TCP port (normally 1999)
      5. DHCPEnabled       W   — 0 = static, 1 = DHCP
    """

    # ── keepalive ─────────────────────────────────────────────────────────────

    TCPAliveData = 0x0009
    """Bidirectional keepalive / heartbeat.

    Either side may send this.  No payload.  The peer must echo it within
    the session timeout window (default 30 s) or the connection is torn down.
    """

    # ── X-ray image transfer ──────────────────────────────────────────────────

    TCPXRayImgBegin = 0x000A
    """Begin marker for an X-ray image transfer.

    Device → host.  Payload fields (big-endian):
      - ImageId        W  — opaque image identifier
      - TotalBlocks    W  — number of :attr:`TCPXRayImgBlock` frames to follow
      - TotalBytes     DW — uncompressed image byte count
      - Width          W  — image width in pixels
      - Height         W  — image height in pixels
      - BitDepth       W  — bits per pixel (12 or 16)
      - Compression    W  — 0=raw, 1=RLE, 2=deflate
    """

    TCPXRayImgBlock = 0x000B
    """One data block within an image transfer sequence.

    Device → host.  Payload fields (big-endian):
      - ImageId    W  — must match the value from :attr:`TCPXRayImgBegin`
      - BlockIndex W  — zero-based block sequence number
      - Data       BA — raw (or compressed) pixel bytes for this block
    """

    TCPXRayImgEnd = 0x000C
    """End marker for an X-ray image transfer.

    Device → host.  Payload fields (big-endian):
      - ImageId  W  — must match :attr:`TCPXRayImgBegin`
      - Status   W  — 0x0000 = success, non-zero = acquisition error
      - Checksum DW — CRC-32 of the reassembled, uncompressed image bytes
    """

    # ── UI / progress feedback ────────────────────────────────────────────────

    TCPProgressBar = 0x000D
    """Gantry / acquisition progress indicator.

    Device → host.  Sent during long operations (CBCT rotation, pano sweep).

    Payload fields (big-endian):
      - Phase      W — 0=positioning, 1=pre-exposure, 2=exposure, 3=readout
      - Percent    W — completion 0–100
      - Remaining  W — estimated remaining time in tenths of a second
    """

    # ── extended device info  (one variant per TCPExtInfo DX* element) ────────
    # Values 0x000E–0x0014 are assigned in Netapi114.xml element order.

    TCPReqExtInfoDX6 = 0x000E
    """Request extended info from a :attr:`DeviceType.DX6` (HELIODENT DS) device.

    Host → device.  No payload.

    Response payload fields specific to DX6 (big-endian):
      - SensorSize     W  — 0=size1, 1=size2 (physical sensor dimensions)
      - DoseMode       W  — 0=standard, 1=low-dose
      - GainMode       W  — 0=auto, 1=manual
      - GainValue      W  — manual gain setting (if GainMode=1)
    """

    TCPReqExtInfoDX7 = 0x000F
    """Request extended info from a :attr:`DeviceType.DX7` (HELIODENT VARIO) device.

    Host → device.  No payload.

    Response payload fields specific to DX7 (big-endian):
      - SensorSize     W  — physical size variant
      - BitDepth       W  — 12 or 14
      - DoseMode       W  — 0=standard, 1=low-dose, 2=ultra-low
      - CalibStatus    W  — 0=ok, 1=dark-field needed, 2=flat-field needed
    """

    TCPReqExtInfoDX41 = 0x0010
    """Request extended info from a :attr:`DeviceType.DX41` (ORTHOPHOS XG) device.

    Host → device.  No payload.

    Response payload fields specific to DX41 (big-endian):
      - Program        W  — current examination program (see PROGRAM_* constants)
      - PatientSize    W  — 0=child, 1=adult-S, 2=adult-M, 3=adult-L
      - kV             W  — tube voltage
      - mAx10          W  — tube current in tenths of mA
      - ExposureMs     W  — nominal exposure time in ms
      - RotationSpeed  W  — gantry speed (device units)
      - FocalSpot      W  — 0=large, 1=small
      - LayerWidth     W  — tomographic layer width (device units)
    """

    TCPReqExtInfoDX81C = 0x0011
    """Request extended info from a :attr:`DeviceType.DX81C` (ORTHOPHOS SL Ceph).

    Host → device.  No payload.

    Response payload fields (big-endian):
      - Program        W  — examination program
      - PatientSize    W
      - kV             W
      - mAx10          W
      - ExposureMs     W
      - CephRotation   W  — cephalometric head-unit rotation in tenths of degree
      - CassettePosX   W  — cassette horizontal position offset
      - CassettePosY   W  — cassette vertical position offset
    """

    TCPReqExtInfoDX81P = 0x0012
    """Request extended info from a :attr:`DeviceType.DX81P` (ORTHOPHOS SL Pan).

    Host → device.  No payload.

    Response payload fields (big-endian):
      - Program        W  — examination program
      - PatientSize    W
      - kV             W
      - mAx10          W
      - ExposureMs     W
      - RotationSpeed  W
      - FocalSpot      W
    """

    TCPReqExtInfoDX89 = 0x0013
    """Request extended info from a :attr:`DeviceType.DX89` (GALILEOS) device.

    Host → device.  No payload.

    Response payload fields (big-endian):
      - VoxelSize      W  — isotropic voxel pitch in µm (e.g. 300 = 0.3 mm)
      - FOVDiameter    W  — field of view diameter in mm
      - FOVHeight      W  — field of view height in mm
      - kV             W
      - mAx10          W
      - FrameCount     W  — number of projection frames in the rotation
      - RotationArc    W  — rotation arc in tenths of degree (normally 3600)
    """

    TCPReqExtInfoDX91 = 0x0014
    """Request extended info from a :attr:`DeviceType.DX91` (GALILEOS COMFORT).

    Host → device.  No payload.

    Response payload fields (big-endian):
      - VoxelSize      W
      - FOVDiameter    W
      - FOVHeight      W
      - kV             W
      - mAx10          W
      - FrameCount     W
      - RotationArc    W
      - ReconAlgo      W  — 0=standard FDK, 1=iterative  # inferred
      - MetalArtifact  W  — metal artefact reduction enabled (0/1)  # inferred
    """

    # ── generic response codes ────────────────────────────────────────────────

    TCPAck = 0xFF00
    """Generic positive acknowledgement.

    Device → host (or host → device for symmetric commands).
    Payload: empty, or command-specific data.
    """

    TCPError = 0xFF01
    """Error response.

    Device → host.  Payload fields (big-endian):
      - ErrorCode  W  — :class:`ErrorCode` value
      - MessageLen W  — byte length of optional UTF-16LE error message
      - Message    raw — UTF-16LE encoded error description (may be empty)
    """


# Backward-compat aliases so existing tcp.py / packets.py code still resolves.
# FUNC_ACK / FUNC_ERROR / FUNC_CONNECT / FUNC_DISCONNECT re-point to the enum;
# FUNC_STATUS / FUNC_GET_PARAM / FUNC_SET_PARAM / FUNC_TRIGGER / FUNC_GET_IMAGE
# have no Netapi114.xml rename so they keep the scalar values defined above.
FUNC_ACK: int = TCPFuncCode.TCPAck
FUNC_ERROR: int = TCPFuncCode.TCPError
FUNC_CONNECT: int = TCPFuncCode.TCPConnect
FUNC_DISCONNECT: int = TCPFuncCode.TCPDisconnect


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  ErrorCode
# ╚══════════════════════════════════════════════════════════════════════════════

@unique
class ErrorCode(IntEnum):
    """Error codes returned in :attr:`TCPFuncCode.TCPError` payload (first WORD).

    Matches the ``<ErrorCode>`` element values in ``Netapi114.xml``.
    """

    OK = 0x0000
    """No error (used in success responses that embed an error field)."""

    UNKNOWN_COMMAND = 0x0001
    """The ``func_code`` in the request is not recognised by this firmware."""

    INVALID_PARAMETER = 0x0002
    """A parameter value is outside the allowed range or is malformed."""

    DEVICE_BUSY = 0x0003
    """The device is already executing an operation (e.g. exposure in progress)."""

    NOT_READY = 0x0004
    """Device subsystem is not ready (warm-up, calibration pending, etc.)."""

    TIMEOUT = 0x0005
    """An internal device operation did not complete within its deadline."""

    HARDWARE_ERROR = 0x0006
    """A sensor, motor, or electronics fault has been detected."""

    ACCESS_DENIED = 0x0007
    """Command not permitted in the current device state or security context."""

    SEQUENCE_ERROR = 0x0008
    """Command received out of the expected protocol sequence
    (e.g. ``TCPXRayImgBlock`` without a preceding ``TCPXRayImgBegin``)."""

    CHECKSUM_ERROR = 0x0009
    """Payload CRC or integrity check failed."""

    UNSUPPORTED_CAPABILITY = 0x000A
    """The requested feature is not available on this hardware variant."""

    GENERIC_ERROR = 0x00FF
    """Unclassified error; consult the optional message string in the payload."""

    @classmethod
    def _missing_(cls, value: object) -> "ErrorCode":
        return cls.GENERIC_ERROR


# Backward-compat plain dict.
ERROR_CODES: dict[int, str] = {e.value: e.name for e in ErrorCode}


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Lookup helpers
# ╚══════════════════════════════════════════════════════════════════════════════

def device_type_from_word(word: int) -> DeviceType:
    """Safely convert a raw WORD from the discovery payload to a :class:`DeviceType`."""
    try:
        return DeviceType(word)
    except ValueError:
        return DeviceType.UNKNOWN


def ext_info_func_for(device_type: DeviceType) -> TCPFuncCode | None:
    """Return the ``TCPReqExtInfoDX*`` function code for *device_type*, or
    ``None`` if this hardware has no extended-info variant in Netapi114.xml.
    """
    return _EXT_INFO_MAP.get(device_type)


_EXT_INFO_MAP: dict[DeviceType, TCPFuncCode] = {
    DeviceType.DX6:   TCPFuncCode.TCPReqExtInfoDX6,
    DeviceType.DX7:   TCPFuncCode.TCPReqExtInfoDX7,
    DeviceType.DX41:  TCPFuncCode.TCPReqExtInfoDX41,
    DeviceType.DX81C: TCPFuncCode.TCPReqExtInfoDX81C,
    DeviceType.DX81P: TCPFuncCode.TCPReqExtInfoDX81P,
    DeviceType.DX89:  TCPFuncCode.TCPReqExtInfoDX89,
    DeviceType.DX91:  TCPFuncCode.TCPReqExtInfoDX91,
}
