"""SiNet2 / P2K protocol layer."""

from .constants import (
    MAGIC,
    DEFAULT_PORT,
    FUNC_DISCOVER,
    DEVICE_TYPES,
    ERROR_CODES,
    TCPFuncCode,
    UDPFuncCode,
    DeviceType,
    ErrorCode,
)
from .packets import (
    DiscoveryResponse,
    DISCOVERY_PROBE,
    build_udp_header,
    parse_udp_header,
)
from .udp import SiNet2Discovery, UDPDiscovery, DeviceAnnounce
from .tcp import (
    SiNet2Client,
    TCPSession,       # backward-compat alias
    DeviceInfo,
    P2KError,
    P2KConnectionError,
    P2KProtocolError,
    P2KDeviceError,
)

__all__ = [
    # constants
    "MAGIC",
    "DEFAULT_PORT",
    "FUNC_DISCOVER",
    "DEVICE_TYPES",
    "ERROR_CODES",
    "TCPFuncCode",
    "UDPFuncCode",
    "DeviceType",
    "ErrorCode",
    # UDP
    "SiNet2Discovery",
    "UDPDiscovery",
    "DeviceAnnounce",
    "DiscoveryResponse",
    "DISCOVERY_PROBE",
    "build_udp_header",
    "parse_udp_header",
    # TCP
    "SiNet2Client",
    "TCPSession",
    "DeviceInfo",
    "P2KError",
    "P2KConnectionError",
    "P2KProtocolError",
    "P2KDeviceError",
]
