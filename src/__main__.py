"""
PureXS command-line interface.

Entry point registered in pyproject.toml::

    [project.scripts]
    purexs = "src.__main__:main"

Commands
--------
  purexs discover            Broadcast a UDP probe and print a device table.
  purexs info <ip>           Connect via TCP and print full static device info.
  purexs serve               Start the FastAPI REST server (0.0.0.0:8000).
  purexs mock                Run a mock P2K device for local testing.

Each command supports ``--help`` for its own options.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  discover
# ╚══════════════════════════════════════════════════════════════════════════════

def cmd_discover(args: argparse.Namespace) -> int:
    """Broadcast a UDP discovery probe and print a table of responding devices."""
    from .protocol.udp import UDPDiscovery
    from .protocol.constants import DEVICE_TYPES

    print(
        f"Scanning for P2K devices  "
        f"(broadcast={args.broadcast}, timeout={args.timeout:.1f}s) …"
    )

    t0 = time.monotonic()
    with UDPDiscovery(broadcast_addr=args.broadcast) as disc:
        responses = disc.scan(timeout=args.timeout)
    elapsed = time.monotonic() - t0

    if not responses:
        print(f"No devices found ({elapsed:.1f}s).")
        return 1

    # ── table ─────────────────────────────────────────────────────────────────
    col = "{:<18}  {:<15}  {:<6}  {:<8}  {}"
    print()
    print(col.format("MAC", "IP", "PORT", "TYPE", "DEVICE NAME"))
    print("─" * 72)
    for r in responses:
        name = DEVICE_TYPES.get(r.device_type, "UNKNOWN")
        print(col.format(
            r.mac,
            r.ip,
            str(r.tcp_port),
            f"0x{r.device_type:04X}",
            name,
        ))
    print()
    print(f"{len(responses)} device(s) found in {elapsed:.1f}s.")
    return 0


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  info
# ╚══════════════════════════════════════════════════════════════════════════════

def cmd_info(args: argparse.Namespace) -> int:
    """Connect via TCP and print full static device info."""

    async def _run() -> int:
        from .protocol.tcp import SiNet2Client, P2KConnectionError, P2KDeviceError
        from .protocol.constants import DEVICE_TYPES

        print(f"Connecting to {args.ip}:{args.port} …")
        try:
            async with SiNet2Client(connect_timeout=args.timeout) as client:
                await client.connect(args.ip, args.port)

                sid = client._session_id  # noqa: SLF001
                print(f"  Session ID   : 0x{sid:08X}")

                info = await client.request_info()
                type_name = DEVICE_TYPES.get(info.device_type, "UNKNOWN")

                print(f"  Firmware     : {info.firmware_version}")
                print(f"  Serial No    : {info.serial_number}")
                print(f"  Device Type  : 0x{info.device_type:04X}  ({type_name})")
                print(f"  Hardware Rev : {info.hardware_rev}")

        except P2KConnectionError as exc:
            print(f"Connection failed: {exc}", file=sys.stderr)
            return 1
        except P2KDeviceError as exc:
            print(f"Device error 0x{exc.error_code:04X}: {exc}", file=sys.stderr)
            return 2

        return 0

    return asyncio.run(_run())


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  serve
# ╚══════════════════════════════════════════════════════════════════════════════

def cmd_serve(args: argparse.Namespace) -> int:
    """Start the FastAPI REST server."""
    from .api.main import serve

    serve(
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level.lower(),
    )
    return 0


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  mock
# ╚══════════════════════════════════════════════════════════════════════════════

def cmd_mock(args: argparse.Namespace) -> int:
    """Run a local async mock P2K device (UDP + TCP) for testing."""

    async def _run() -> int:
        try:
            # MockSironaDevice lives in tests/ — importable when running from
            # the PureXS project root (i.e. python -m src or purexs CLI).
            from tests.mock_device import MockSironaDevice
        except ImportError as exc:
            print(
                f"Cannot import MockSironaDevice: {exc}\n"
                "Run purexs from the PureXS project root directory.",
                file=sys.stderr,
            )
            return 1

        device_type = int(args.device_type, 0)
        mock = MockSironaDevice(
            ip=args.host,
            port=args.port,
            udp_port=args.udp_port,
            device_type=device_type,
            serial_number=args.serial,
            firmware_version=args.firmware,
            broadcast_interval=args.broadcast_interval,
        )

        await mock.start()

        banner = (
            f"MockSironaDevice running\n"
            f"  TCP  {args.host}:{args.port}\n"
            f"  UDP  {args.host}:{args.udp_port}  "
            f"(broadcast every {args.broadcast_interval:.0f}s)\n"
            f"  device_type=0x{device_type:04X}  "
            f"serial={args.serial!r}  fw={args.firmware!r}"
        )
        print(banner)
        print("Press Ctrl+C to stop.")

        try:
            while True:
                await asyncio.sleep(1.0)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nShutting down mock device …")
        finally:
            await mock.stop()

        return 0

    return asyncio.run(_run())


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  argument parser
# ╚══════════════════════════════════════════════════════════════════════════════

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="purexs",
        description="PureXS — open-source Sirona P2K dental imaging toolkit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  purexs discover\n"
            "  purexs discover --broadcast 192.168.1.255 --timeout 10\n"
            "  purexs info 192.168.1.50\n"
            "  purexs serve --port 8080 --reload\n"
            "  purexs mock --device-type 0x0029 --broadcast-interval 2\n"
        ),
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        metavar="LEVEL",
        help="logging verbosity for internal messages (default: WARNING)",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # ── discover ──────────────────────────────────────────────────────────────
    p_disc = sub.add_parser(
        "discover",
        help="broadcast UDP probe, print discovered device table",
        description="Send a SiNet2 UDP broadcast probe and print all responding devices.",
    )
    p_disc.add_argument(
        "--timeout", "-t",
        type=float, default=5.0, metavar="SEC",
        help="listen window in seconds (default: 5)",
    )
    p_disc.add_argument(
        "--broadcast", "-b",
        default="255.255.255.255", metavar="ADDR",
        help="broadcast address (default: 255.255.255.255)",
    )
    p_disc.set_defaults(func=cmd_discover)

    # ── info ──────────────────────────────────────────────────────────────────
    p_info = sub.add_parser(
        "info",
        help="connect TCP, print full device info",
        description=(
            "Open a P2K TCP session to the device at <ip> and print "
            "firmware version, serial number, device type, and hardware revision."
        ),
    )
    p_info.add_argument("ip", help="device IPv4 address")
    p_info.add_argument(
        "--port", "-p",
        type=int, default=1999, metavar="PORT",
        help="TCP port (default: 1999)",
    )
    p_info.add_argument(
        "--timeout", "-t",
        type=float, default=10.0, metavar="SEC",
        help="connect timeout in seconds (default: 10)",
    )
    p_info.set_defaults(func=cmd_info)

    # ── serve ─────────────────────────────────────────────────────────────────
    p_serve = sub.add_parser(
        "serve",
        help="start the FastAPI REST server",
        description=(
            "Start uvicorn serving the PureXS REST API. "
            "OpenAPI docs are available at http://<host>:<port>/docs."
        ),
    )
    p_serve.add_argument(
        "--host",
        default="0.0.0.0", metavar="ADDR",
        help="bind address (default: 0.0.0.0)",
    )
    p_serve.add_argument(
        "--port", "-p",
        type=int, default=8000, metavar="PORT",
        help="HTTP port (default: 8000)",
    )
    p_serve.add_argument(
        "--reload",
        action="store_true",
        help="enable auto-reload on code changes (dev mode)",
    )
    p_serve.set_defaults(func=cmd_serve)

    # ── mock ──────────────────────────────────────────────────────────────────
    p_mock = sub.add_parser(
        "mock",
        help="run a mock P2K device for testing",
        description=(
            "Start an async mock SiNet2 / P2K device that responds to UDP "
            "discovery probes and accepts TCP connections.  Useful for "
            "integration testing without physical hardware."
        ),
    )
    p_mock.add_argument(
        "--host",
        default="127.0.0.1", metavar="ADDR",
        help="bind address (default: 127.0.0.1)",
    )
    p_mock.add_argument(
        "--port", "-p",
        type=int, default=1999, metavar="PORT",
        help="TCP listen port (default: 1999)",
    )
    p_mock.add_argument(
        "--udp-port",
        type=int, default=1999, dest="udp_port", metavar="PORT",
        help="UDP listen / broadcast port (default: 1999)",
    )
    p_mock.add_argument(
        "--device-type",
        default="0x0029", dest="device_type", metavar="HEX",
        help="DeviceType WORD, e.g. 0x0029 = ORTHOPHOS XG (default: 0x0029)",
    )
    p_mock.add_argument(
        "--serial",
        default="SN-MOCK-001", metavar="SN",
        help="serial number returned in TCPInfo (default: SN-MOCK-001)",
    )
    p_mock.add_argument(
        "--firmware",
        default="3.2.1", metavar="VER",
        help="firmware version string returned in TCPInfo (default: 3.2.1)",
    )
    p_mock.add_argument(
        "--broadcast-interval",
        type=float, default=5.0,
        dest="broadcast_interval", metavar="SEC",
        help="seconds between proactive UDP broadcasts; 0=off (default: 5.0)",
    )
    p_mock.set_defaults(func=cmd_mock)

    return parser


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  main
# ╚══════════════════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``purexs`` console script.

    Parses *argv* (defaults to ``sys.argv[1:]``), configures logging, and
    dispatches to the appropriate sub-command handler.  The handler's integer
    return value becomes the process exit code.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)-8s  %(name)s  %(message)s",
    )

    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
