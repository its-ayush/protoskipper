# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet device simulator scaffold (P7.F — partial implementation).

Implements the ``Simulator`` mix-in protocol.  When started, exposes a
bacpypes3-backed BACnet/IP server on UDP/47808 with a configurable set of
objects so engineers can test without live hardware.

Configuration keys
------------------
``objects``
    List of dicts, each with ``type``, ``instance``, ``objectName``,
    ``presentValue``, and optional ``units``.
``device_id``
    The simulated device instance number (default 9999).
``address``
    Local bind address (default ``"0.0.0.0"``).
``port``
    UDP port (default 47808).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Mapping
from typing import Any

_logger = logging.getLogger(__name__)


class BacnetSimulator:
    """Minimal BACnet/IP device simulator backed by bacpypes3.

    Implements the ``Simulator`` protocol from ``protoskipper.core.driver``.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._app: Any = None

    def start_simulator(self, config: Mapping[str, Any]) -> None:
        """Start the simulated BACnet device."""
        try:
            from bacpypes3.ipv4.app import NormalApplication  # noqa: F401
            from bacpypes3.local.device import DeviceObject  # noqa: F401
            from bacpypes3.pdu import Address  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "bacpypes3 is not installed — run: pip install 'protoskipper[bacnet]'"
            ) from exc

        device_id = int(config.get("device_id", 9999))
        address = str(config.get("address", "0.0.0.0"))
        port = int(config.get("port", 47808))
        objects_cfg = list(config.get("objects", []))

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="bacnet-simulator", daemon=True)

        # Store creation args for the async init
        self._init_args = (device_id, address, port, objects_cfg)
        self._thread.start()

        # Wait a moment for the loop to be ready
        import time

        time.sleep(0.1)

    def _run(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._async_start(*self._init_args))
        self._loop.run_forever()

    async def _async_start(
        self,
        device_id: int,
        address: str,
        port: int,
        objects_cfg: list[dict[str, Any]],
    ) -> None:
        from bacpypes3.ipv4.app import NormalApplication
        from bacpypes3.local.analog import AnalogInputObject, AnalogValueObject
        from bacpypes3.local.device import DeviceObject
        from bacpypes3.pdu import Address

        local_device = DeviceObject(
            objectIdentifier=("device", device_id),
            objectName=f"ProtoSkipper-Simulator-{device_id}",
            vendorIdentifier=0xFFFF,
            vendorName="ProtoSkipper",
            modelName="Simulator",
            firmwareRevision="1.0",
        )
        bind_addr = f"{address}:{port}" if port != 47808 else address
        self._app = NormalApplication(local_device, Address(bind_addr))

        for obj_cfg in objects_cfg:
            obj_type = str(obj_cfg.get("type", "analog-input")).lower()
            instance = int(obj_cfg.get("instance", 0))
            obj_name = str(obj_cfg.get("objectName", f"{obj_type}-{instance}"))
            pv = obj_cfg.get("presentValue", 0.0)

            if obj_type in ("analog-input", "ai"):
                obj = AnalogInputObject(
                    objectIdentifier=("analog-input", instance),
                    objectName=obj_name,
                    presentValue=float(pv),
                )
            elif obj_type in ("analog-value", "av"):
                obj = AnalogValueObject(
                    objectIdentifier=("analog-value", instance),
                    objectName=obj_name,
                    presentValue=float(pv),
                )
            else:
                _logger.debug("Simulator: unsupported object type %r, skipped", obj_type)
                continue
            self._app.add_object(obj)

        _logger.info("BACnet simulator started: device=%d, %d objects", device_id, len(objects_cfg))

    def stop_simulator(self) -> None:
        """Stop the simulated device and release resources."""
        if self._app is not None:
            try:
                if self._loop:
                    self._loop.call_soon_threadsafe(self._app.close)
            except Exception:
                pass
            self._app = None
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:
                self._thread.join(timeout=3)
            self._loop = None
        _logger.info("BACnet simulator stopped")
