# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet device simulator (P7.F.1).

Implements the ``Simulator`` mix-in protocol.  When started, exposes a
bacpypes3-backed BACnet/IP server on the configured UDP port with a fully
configurable object model.  All standard APDU services (RP, RPM, WP, WPM,
COV subscribe/notify, Who-Is/I-Am) are handled by bacpypes3 automatically
via the local object model.

Configuration keys
------------------
``objects``
    List of dicts, each with ``type``, ``instance``, ``objectName``,
    ``presentValue``, and optional ``units``, ``covIncrement``,
    ``stateText`` (list[str] for multi-state).
``device_id``
    The simulated device instance number (default 9999).
``address``
    Local bind address (default ``"0.0.0.0"``).
``port``
    UDP port (default 47808).

Design notes
------------
* All bacpypes3 interaction runs exclusively on the simulator's private
  event loop (``self._loop``), isolated from any caller thread.
* ``update_value()`` / ``get_value()`` are thread-safe; they submit a
  coroutine to the loop and block on the result via a ``concurrent.futures``
  future.
* Property changes on local objects trigger COV notifications automatically
  when a client has subscribed.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Mapping
from typing import Any

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Object-type registry
# ---------------------------------------------------------------------------

_ANALOG_FLOAT_TYPES = frozenset(["analog-input", "ai", "analog-output", "ao", "analog-value", "av"])
_BINARY_TYPES = frozenset(["binary-input", "bi", "binary-output", "bo", "binary-value", "bv"])
_MULTISTATE_TYPES = frozenset(
    [
        "multi-state-input",
        "msi",
        "multi-state-output",
        "mso",
        "multi-state-value",
        "msv",
        "multistate-input",
        "multistate-output",
        "multistate-value",
    ]
)

# Canonical BACnet object-type tag used by ObjectIdentifier
_CANONICAL_TYPE: dict[str, str] = {
    "ai": "analog-input",
    "ao": "analog-output",
    "av": "analog-value",
    "bi": "binary-input",
    "bo": "binary-output",
    "bv": "binary-value",
    "msi": "multi-state-input",
    "mso": "multi-state-output",
    "msv": "multi-state-value",
    "multistate-input": "multi-state-input",
    "multistate-output": "multi-state-output",
    "multistate-value": "multi-state-value",
}


def _canon_type(raw: str) -> str:
    return _CANONICAL_TYPE.get(raw, raw)


class BacnetSimulator:
    """Full BACnet/IP device simulator backed by bacpypes3 (P7.F.1).

    Implements the ``Simulator`` protocol from ``protoskipper.core.driver``.
    Answers Who-Is/I-Am; serves RP/RPM/WP/WPM; sends COV notifications.

    Thread safety
    -------------
    ``update_value``, ``get_value``, and ``stop_simulator`` are safe to call
    from any thread.  All other methods are for the simulator's event loop.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._app: Any = None
        # Map "analog-value:1" → local object (populated after _async_start)
        self._objects: dict[str, Any] = {}
        self._ready = threading.Event()
        self._alarm_log: list[dict[str, Any]] = []  # P7.F.3

    # ------------------------------------------------------------------
    # Public: Simulator protocol
    # ------------------------------------------------------------------

    def start_simulator(self, config: Mapping[str, Any]) -> None:
        """Start the simulated BACnet device.

        Blocks until the UDP socket is bound and the app is ready to serve
        requests.
        """
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

        self._ready.clear()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run,
            name="bacnet-simulator",
            daemon=True,
        )
        self._init_args = (device_id, address, port, objects_cfg)
        self._thread.start()
        # Block until the UDP socket is bound and objects are created
        if not self._ready.wait(timeout=10):
            raise RuntimeError("BACnet simulator failed to start within 10 s")

    def stop_simulator(self) -> None:
        """Stop the simulated device and release resources."""
        if self._app is not None and self._loop is not None:
            import contextlib

            with contextlib.suppress(Exception):
                self._loop.call_soon_threadsafe(self._app.close)
            self._app = None
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:
                self._thread.join(timeout=5)
            self._loop = None
        self._objects.clear()
        _logger.info("BACnet simulator stopped")

    # ------------------------------------------------------------------
    # Public: value injection
    # ------------------------------------------------------------------

    def update_value(self, object_id: str, prop: str, value: Any) -> None:
        """Thread-safe: update an object property and trigger COV if subscribed.

        ``object_id`` must match the ``"type:instance"`` format, e.g.
        ``"analog-value:1"`` or ``"binary-value:2"``.
        Raises ``KeyError`` if the object does not exist.
        Raises ``RuntimeError`` if the simulator is not running.
        """
        if self._loop is None:
            raise RuntimeError("Simulator is not running")
        fut: concurrent.futures.Future[None] = concurrent.futures.Future()

        async def _do() -> None:
            obj = self._objects.get(object_id)
            if obj is None:
                fut.set_exception(KeyError(object_id))
                return
            try:
                setattr(obj, prop, value)
                fut.set_result(None)
            except Exception as exc:
                fut.set_exception(exc)

        self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_do()))
        fut.result(timeout=5)

    def get_value(self, object_id: str, prop: str = "presentValue") -> Any:
        """Thread-safe: read back an object property from the simulator's model.

        Raises ``KeyError`` if the object does not exist.
        """
        if self._loop is None:
            raise RuntimeError("Simulator is not running")
        fut: concurrent.futures.Future[Any] = concurrent.futures.Future()

        async def _do() -> None:
            obj = self._objects.get(object_id)
            if obj is None:
                fut.set_exception(KeyError(object_id))
                return
            try:
                fut.set_result(getattr(obj, prop))
            except Exception as exc:
                fut.set_exception(exc)

        self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_do()))
        return fut.result(timeout=5)

    # ------------------------------------------------------------------
    # P7.F.3 — Alarm generation engine
    # ------------------------------------------------------------------

    def trigger_event(
        self,
        object_id: str,
        event_state: str = "offnormal",
        notify_type: str = "alarm",
        priority: int = 100,
        message_text: str = "",
    ) -> None:
        """Inject a simulated event-notification for *object_id*.

        Marks the object's ``eventState`` (if supported) and logs the event
        internally.  External clients that have subscribed via COV / alarms
        will receive the change notification from bacpypes3's normal dispatch.

        *event_state* — ``"normal"``, ``"offnormal"``, ``"fault"``,
        ``"highLimit"``, ``"lowLimit"``, or ``"lifeSafetyAlarm"``.
        *notify_type* — ``"alarm"``, ``"event"``, or ``"ackNotification"``.
        """
        entry = {
            "object_id": object_id,
            "event_state": event_state,
            "notify_type": notify_type,
            "priority": priority,
            "message_text": message_text,
        }
        if self._loop is None:
            # Allow logging without a running simulator loop (tests / offline use)
            self._alarm_log.append(entry)
            return
        fut: concurrent.futures.Future[None] = concurrent.futures.Future()

        async def _do() -> None:
            obj = self._objects.get(object_id)
            if obj is None:
                fut.set_exception(KeyError(object_id))
                return
            try:
                if hasattr(obj, "eventState"):
                    obj.eventState = event_state
                _logger.info(
                    "Simulator alarm: %s state=%s type=%s pri=%d msg=%r",
                    object_id,
                    event_state,
                    notify_type,
                    priority,
                    message_text,
                )
                self._alarm_log.append(entry)
                fut.set_result(None)
            except Exception as exc:
                fut.set_exception(exc)

        self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_do()))
        fut.result(timeout=5)

    def alarm_log(self) -> list[dict[str, Any]]:
        """Return a snapshot of injected alarm events (thread-safe copy)."""
        return list(self._alarm_log)

    def clear_alarm_log(self) -> None:
        """Clear the injected alarm event log (thread-safe)."""
        self._alarm_log.clear()

    # ------------------------------------------------------------------
    # P7.F.4 — Script-driven simulator
    # ------------------------------------------------------------------

    def run_script(self, script: str) -> None:
        """Execute *script* (Python source) inside the simulator context.

        The script receives a ``sim`` global bound to this simulator instance.
        It may call ``sim.update_value()``, ``sim.trigger_event()``, etc.

        .. warning::
            This executes arbitrary Python.  Do not expose to untrusted input.
            Only available when the safety profile is not PRODUCTION.
        """
        globs: dict[str, Any] = {"sim": self, "__builtins__": __builtins__}
        exec(compile(script, "<simulator-script>", "exec"), globs)

    def inject_sequence(
        self,
        object_id: str,
        sequence: list[tuple[float, str, Any]],
    ) -> None:
        """Schedule a time-based value injection sequence.

        *sequence* is a list of ``(delay_seconds, prop, value)`` tuples.
        Each step fires after *delay_seconds* from the previous step (or
        from now for the first).  Runs asynchronously on the simulator loop.
        Does not block.
        """
        if self._loop is None:
            raise RuntimeError("Simulator is not running")

        async def _play() -> None:
            for delay, prop, value in sequence:
                await asyncio.sleep(delay)
                obj = self._objects.get(object_id)
                if obj is None:
                    _logger.warning("inject_sequence: object %r not found", object_id)
                    return
                try:
                    setattr(obj, prop, value)
                    _logger.debug("inject_sequence: %s.%s = %r", object_id, prop, value)
                except Exception as exc:
                    _logger.warning("inject_sequence: set %s.%s failed: %s", object_id, prop, exc)

        self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_play()))

    # ------------------------------------------------------------------
    # Internal: event loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._async_start(*self._init_args))
        self._ready.set()
        self._loop.run_forever()

    async def _async_start(
        self,
        device_id: int,
        address: str,
        port: int,
        objects_cfg: list[dict[str, Any]],
    ) -> None:
        from bacpypes3.ipv4.app import NormalApplication
        from bacpypes3.local.device import DeviceObject
        from bacpypes3.pdu import Address

        local_device = DeviceObject(
            objectIdentifier=("device", device_id),
            objectName=f"ProtoSkipper-Simulator-{device_id}",
            vendorIdentifier=0xFFFF,
            vendorName="ProtoSkipper",
            modelName="Simulator",
            firmwareRevision="1.0",
            applicationSoftwareVersion="P7.F.1",
            description="ProtoSkipper built-in BACnet device simulator",
        )
        bind_addr = f"{address}:{port}" if port != 47808 else address
        self._app = NormalApplication(local_device, Address(bind_addr))

        added = 0
        for obj_cfg in objects_cfg:
            raw_type = str(obj_cfg.get("type", "analog-input")).lower().strip()
            obj_type = _canon_type(raw_type)
            instance = int(obj_cfg.get("instance", 0))
            obj_name = str(obj_cfg.get("objectName", f"{obj_type}-{instance}"))
            pv = obj_cfg.get("presentValue", 0.0)
            obj = self._make_object(obj_type, instance, obj_name, pv, obj_cfg)
            if obj is None:
                _logger.debug(
                    "Simulator: unsupported type %r (instance %d), skipped",
                    obj_type,
                    instance,
                )
                continue
            self._app.add_object(obj)
            key = f"{obj_type}:{instance}"
            self._objects[key] = obj
            added += 1

        _logger.info(
            "BACnet simulator ready: device=%d at %s, %d objects",
            device_id,
            bind_addr,
            added,
        )

    # ------------------------------------------------------------------
    # Object factory
    # ------------------------------------------------------------------

    def _make_object(
        self,
        obj_type: str,
        instance: int,
        name: str,
        pv: Any,
        cfg: dict[str, Any],
    ) -> Any:
        """Return a fully-configured bacpypes3 local object, or None."""
        cov_incr = float(cfg.get("covIncrement", 0.1))
        units = cfg.get("units", "no-units")
        state_text: list[str] = list(cfg.get("stateText", []))
        num_states = int(cfg.get("numberOfStates", max(len(state_text), 2)))

        if obj_type == "analog-input":
            from bacpypes3.local.analog import AnalogInputObject

            return AnalogInputObject(
                objectIdentifier=("analog-input", instance),
                objectName=name,
                presentValue=float(pv),
                units=units,
                covIncrement=cov_incr,
            )
        if obj_type == "analog-output":
            from bacpypes3.local.analog import AnalogOutputObject

            return AnalogOutputObject(
                objectIdentifier=("analog-output", instance),
                objectName=name,
                presentValue=float(pv),
                units=units,
                covIncrement=cov_incr,
            )
        if obj_type == "analog-value":
            from bacpypes3.local.analog import AnalogValueObject

            return AnalogValueObject(
                objectIdentifier=("analog-value", instance),
                objectName=name,
                presentValue=float(pv),
                units=units,
                covIncrement=cov_incr,
            )
        if obj_type == "binary-input":
            from bacpypes3.local.binary import BinaryInputObject

            return BinaryInputObject(
                objectIdentifier=("binary-input", instance),
                objectName=name,
                presentValue="inactive" if not pv else "active",
            )
        if obj_type == "binary-output":
            from bacpypes3.local.binary import BinaryOutputObject

            return BinaryOutputObject(
                objectIdentifier=("binary-output", instance),
                objectName=name,
                presentValue="inactive" if not pv else "active",
            )
        if obj_type == "binary-value":
            from bacpypes3.local.binary import BinaryValueObject

            return BinaryValueObject(
                objectIdentifier=("binary-value", instance),
                objectName=name,
                presentValue="inactive" if not pv else "active",
            )
        if obj_type == "multi-state-input":
            from bacpypes3.local.multistate import MultiStateInputObject

            kwargs: dict[str, Any] = dict(
                objectIdentifier=("multi-state-input", instance),
                objectName=name,
                presentValue=int(pv) or 1,
                numberOfStates=num_states,
            )
            if state_text:
                kwargs["stateText"] = state_text
            return MultiStateInputObject(**kwargs)
        if obj_type == "multi-state-output":
            from bacpypes3.local.multistate import MultiStateOutputObject

            kwargs = dict(
                objectIdentifier=("multi-state-output", instance),
                objectName=name,
                presentValue=int(pv) or 1,
                numberOfStates=num_states,
            )
            if state_text:
                kwargs["stateText"] = state_text
            return MultiStateOutputObject(**kwargs)
        if obj_type == "multi-state-value":
            from bacpypes3.local.multistate import MultiStateValueObject

            kwargs = dict(
                objectIdentifier=("multi-state-value", instance),
                objectName=name,
                presentValue=int(pv) or 1,
                numberOfStates=num_states,
            )
            if state_text:
                kwargs["stateText"] = state_text
            return MultiStateValueObject(**kwargs)
        return None


# ---------------------------------------------------------------------------
# Schedule evaluation (P7.F.2)
# ---------------------------------------------------------------------------


def _time_to_seconds(t: Any) -> int:
    """Convert a (HH, MM, SS) or time-string to seconds-since-midnight."""
    try:
        if hasattr(t, "hour"):
            return t.hour * 3600 + t.minute * 60 + t.second
        parts = str(t).split(":")
        h, m, s = int(parts[0]), int(parts[1]), int(float(parts[2]))
        return h * 3600 + m * 60 + s
    except Exception:
        return 0


def evaluate_weekly_schedule(
    weekly_data: list[list[tuple[str, Any]]],
    dt: Any | None = None,
) -> Any:
    """Return the scheduled value active at *dt* (default: now).

    *weekly_data* — 7-element list of day schedules (index 0 = Monday).
    Each day schedule is a list of ``("HH:MM:SS", value)`` tuples sorted
    ascending.  The last entry whose time ≤ *dt.time()* is the active
    setpoint.  If no entry applies (all later), returns ``None``.

    ASHRAE 135 day-of-week: 1=Monday … 7=Sunday.  Python weekday(): 0=Mon.
    """
    import datetime as _dt

    if dt is None:
        dt = _dt.datetime.now()

    dow = dt.weekday()  # 0=Mon … 6=Sun
    now_secs = dt.hour * 3600 + dt.minute * 60 + dt.second

    day_schedule = weekly_data[dow] if 0 <= dow < len(weekly_data) else []
    active_value: Any = None
    for time_str, value in day_schedule:
        entry_secs = _time_to_seconds(time_str)
        if entry_secs <= now_secs:
            active_value = value
        else:
            break
    return active_value
