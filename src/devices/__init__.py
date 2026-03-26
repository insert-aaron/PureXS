"""PureXS device drivers."""

from .base import SironaDevice, BaseDevice, NetworkConfig
from .registry import DeviceRegistry, GenericP2KDevice, create_device, register
from .orthophos_xg import (
    OrthophosXG,
    ExposureProgram,
    ExposureParams,
    PatientSize,
    LifetimeStats,
    SensorGeometry,
)

__all__ = [
    # base
    "SironaDevice",
    "BaseDevice",      # backward-compat alias
    "NetworkConfig",
    # registry
    "DeviceRegistry",
    "GenericP2KDevice",
    "create_device",
    "register",
    # orthophos_xg
    "OrthophosXG",
    "ExposureProgram",
    "ExposureParams",
    "PatientSize",
    "LifetimeStats",
    "SensorGeometry",
]
