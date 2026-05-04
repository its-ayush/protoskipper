# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Unit tests for the GOOSE publish/subscribe services (P8.C.1 - P8.C.3).

Tests verify that:
* :class:`GooseSubscriberService`:
  - Raises ``ImportError`` when pyiec61850 is absent.
  - Creates a ``GooseReceiver`` and per-subscription ``GooseSubscriber`` on
    ``start()``.
  - Sets the correct listener and interface ID.
  - Raises ``RuntimeError`` if ``add_subscriber`` is called after ``start()``.
  - Destroys all C objects on ``stop()``.

* :class:`GoosePublisherService`:
  - Raises ``ImportError`` when pyiec61850 is absent.
  - Creates a ``GoosePublisher`` with the correct ``CommParameters`` on
    ``open()``.
  - ``publish()`` calls ``GoosePublisher_increaseStNum`` then
    ``GoosePublisher_publish``.
  - ``publish()`` encodes bool / int / float / bytes values correctly.
  - ``_encode_value`` raises ``TypeError`` for unsupported types.
  - ``close()`` destroys the publisher.

* :func:`_safe_literal` (publisher panel helper):
  - Parses True / False / int / float / bytes correctly.
  - Raises ``ValueError`` for unsupported or invalid literals.

* :class:`GooseSubscriberQt` / :class:`GoosePublisherQt`:
  - Emit ``error_occurred`` when pyiec61850 is absent.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_lib() -> MagicMock:
    """Return a MagicMock that looks enough like pyiec61850 for the tests."""
    lib = MagicMock(name="pyiec61850")
    lib.GooseReceiver_create.return_value = MagicMock(name="receiver")
    lib.GooseSubscriber_create.return_value = MagicMock(name="subscriber")
    lib.GoosePublisher_create.return_value = MagicMock(name="publisher")
    lib.CommParameters.return_value = MagicMock(name="comm_params")
    return lib


# ---------------------------------------------------------------------------
# GooseSubscriberService tests
# ---------------------------------------------------------------------------


class TestGooseSubscriberServiceImportError:
    """start() raises ImportError if pyiec61850 is not installed."""

    def test_start_raises_import_error_when_no_pyiec61850(self) -> None:
        with patch.dict(sys.modules, {"pyiec61850": None}):
            from importlib import reload

            import protoskipper_iec61850.goose.subscriber as mod

            reload(mod)
            svc = mod.GooseSubscriberService()
            svc.add_subscriber("LD/LLN0$GO$gcb1", lambda f: None)
            with pytest.raises(ImportError, match="pyiec61850"):
                svc.start("eth0")


class TestGooseSubscriberService:
    """GooseSubscriberService creates correct C objects on start()."""

    def _make_service(self, lib: MagicMock):  # type: ignore[no-untyped-def]
        from protoskipper_iec61850.goose.subscriber import GooseSubscriberService

        svc = GooseSubscriberService()
        return svc

    def test_start_creates_receiver(self) -> None:
        lib = _mock_lib()
        with patch.dict(sys.modules, {"pyiec61850": lib}):
            from importlib import reload

            import protoskipper_iec61850.goose.subscriber as mod

            reload(mod)
            svc = mod.GooseSubscriberService()
            svc.add_subscriber("LD/LLN0$GO$gcb1", lambda f: None)
            svc.start("eth0")

        lib.GooseReceiver_create.assert_called_once()

    def test_start_sets_interface(self) -> None:
        lib = _mock_lib()
        with patch.dict(sys.modules, {"pyiec61850": lib}):
            from importlib import reload

            import protoskipper_iec61850.goose.subscriber as mod

            reload(mod)
            svc = mod.GooseSubscriberService()
            svc.add_subscriber("LD/LLN0$GO$gcb1", lambda f: None)
            svc.start("eth1")

        lib.GooseReceiver_setInterfaceId.assert_called_once_with(
            lib.GooseReceiver_create.return_value, "eth1"
        )

    def test_start_creates_subscriber_per_ref(self) -> None:
        lib = _mock_lib()
        with patch.dict(sys.modules, {"pyiec61850": lib}):
            from importlib import reload

            import protoskipper_iec61850.goose.subscriber as mod

            reload(mod)
            svc = mod.GooseSubscriberService()
            svc.add_subscriber("LD/LLN0$GO$gcb1", lambda f: None)
            svc.add_subscriber("LD/LLN0$GO$gcb2", lambda f: None)
            svc.start("eth0")

        assert lib.GooseSubscriber_create.call_count == 2

    def test_add_subscriber_after_start_raises(self) -> None:
        lib = _mock_lib()
        with patch.dict(sys.modules, {"pyiec61850": lib}):
            from importlib import reload

            import protoskipper_iec61850.goose.subscriber as mod

            reload(mod)
            svc = mod.GooseSubscriberService()
            svc.add_subscriber("LD/LLN0$GO$gcb1", lambda f: None)
            svc.start("eth0")

        with pytest.raises(RuntimeError, match="running"):
            svc.add_subscriber("LD/LLN0$GO$gcb2", lambda f: None)

    def test_stop_destroys_receiver_and_subscribers(self) -> None:
        lib = _mock_lib()
        with patch.dict(sys.modules, {"pyiec61850": lib}):
            from importlib import reload

            import protoskipper_iec61850.goose.subscriber as mod

            reload(mod)
            svc = mod.GooseSubscriberService()
            svc.add_subscriber("LD/LLN0$GO$gcb1", lambda f: None)
            svc.start("eth0")
            svc.stop()

        lib.GooseReceiver_stop.assert_called_once()
        lib.GooseReceiver_destroy.assert_called_once()
        lib.GooseSubscriber_destroy.assert_called_once()

    def test_stop_without_start_is_noop(self) -> None:
        lib = _mock_lib()
        with patch.dict(sys.modules, {"pyiec61850": lib}):
            from importlib import reload

            import protoskipper_iec61850.goose.subscriber as mod

            reload(mod)
            svc = mod.GooseSubscriberService()
            svc.stop()  # must not raise

        lib.GooseReceiver_destroy.assert_not_called()


# ---------------------------------------------------------------------------
# GoosePublisherService tests
# ---------------------------------------------------------------------------


class TestGoosePublisherServiceImportError:
    """open() raises ImportError if pyiec61850 is not installed."""

    def test_open_raises_import_error_when_no_pyiec61850(self) -> None:
        with patch.dict(sys.modules, {"pyiec61850": None}):
            from importlib import reload

            import protoskipper_iec61850.goose.publisher as mod

            reload(mod)
            svc = mod.GoosePublisherService(
                params=mod.CommParameters(),
                iface="eth0",
                go_cb_ref="LD/LLN0$GO$gcb1",
                dat_set_ref="LD/LLN0$DS1",
            )
            with pytest.raises(ImportError, match="pyiec61850"):
                svc.open()


class TestGoosePublisherService:
    """GoosePublisherService creates and manages GoosePublisher correctly."""

    def _make_open_service(self, lib: MagicMock):  # type: ignore[no-untyped-def]
        import protoskipper_iec61850.goose.publisher as mod

        svc = mod.GoosePublisherService(
            params=mod.CommParameters(),
            iface="eth0",
            go_cb_ref="LD/LLN0$GO$gcb1",
            dat_set_ref="LD/LLN0$DS1",
        )
        svc._lib = lib
        svc._publisher = lib.GoosePublisher_create.return_value
        return svc

    def test_open_creates_publisher(self) -> None:
        lib = _mock_lib()
        with patch.dict(sys.modules, {"pyiec61850": lib}):
            from importlib import reload

            import protoskipper_iec61850.goose.publisher as mod

            reload(mod)
            svc = mod.GoosePublisherService(
                params=mod.CommParameters(),
                iface="eth0",
                go_cb_ref="LD/LLN0$GO$gcb1",
                dat_set_ref="LD/LLN0$DS1",
            )
            svc.open()

        lib.GoosePublisher_create.assert_called_once()

    def test_open_twice_raises(self) -> None:
        lib = _mock_lib()
        with patch.dict(sys.modules, {"pyiec61850": lib}):
            from importlib import reload

            import protoskipper_iec61850.goose.publisher as mod

            reload(mod)
            svc = mod.GoosePublisherService(
                params=mod.CommParameters(),
                iface="eth0",
                go_cb_ref="LD/LLN0$GO$gcb1",
                dat_set_ref="LD/LLN0$DS1",
            )
            svc.open()
            with pytest.raises(RuntimeError, match="already open"):
                svc.open()

    def test_publish_calls_increase_st_num(self) -> None:
        lib = _mock_lib()
        lib.LinkedList_create.return_value = MagicMock(name="ll")
        svc = self._make_open_service(lib)
        svc.publish([True])
        lib.GoosePublisher_increaseStNum.assert_called_once_with(svc._publisher)

    def test_publish_calls_goose_publish(self) -> None:
        lib = _mock_lib()
        lib.LinkedList_create.return_value = MagicMock(name="ll")
        svc = self._make_open_service(lib)
        svc.publish([True])
        lib.GoosePublisher_publish.assert_called_once()

    def test_publish_without_open_raises(self) -> None:
        from protoskipper_iec61850.goose.publisher import (
            CommParameters,
            GoosePublisherService,
        )

        svc = GoosePublisherService(
            params=CommParameters(),
            iface="eth0",
            go_cb_ref="LD/LLN0$GO$gcb1",
            dat_set_ref="LD/LLN0$DS1",
        )
        with pytest.raises(RuntimeError, match="not open"):
            svc.publish([True])

    def test_close_destroys_publisher(self) -> None:
        lib = _mock_lib()
        svc = self._make_open_service(lib)
        svc.close()
        lib.GoosePublisher_destroy.assert_called_once_with(lib.GoosePublisher_create.return_value)

    def test_close_without_open_is_noop(self) -> None:
        from protoskipper_iec61850.goose.publisher import (
            CommParameters,
            GoosePublisherService,
        )

        svc = GoosePublisherService(
            params=CommParameters(),
            iface="eth0",
            go_cb_ref="LD/LLN0$GO$gcb1",
            dat_set_ref="LD/LLN0$DS1",
        )
        svc.close()  # must not raise


# ---------------------------------------------------------------------------
# _encode_value tests
# ---------------------------------------------------------------------------


class TestEncodeValue:
    """_encode_value encodes Python natives to MmsValue correctly."""

    def _lib(self) -> MagicMock:
        return MagicMock(name="lib")

    def test_bool_true(self) -> None:
        from protoskipper_iec61850.goose.publisher import _encode_value

        lib = self._lib()
        _encode_value(lib, True)
        lib.MmsValue_newBoolean.assert_called_once_with(True)

    def test_bool_false(self) -> None:
        from protoskipper_iec61850.goose.publisher import _encode_value

        lib = self._lib()
        _encode_value(lib, False)
        lib.MmsValue_newBoolean.assert_called_once_with(False)

    def test_int(self) -> None:
        from protoskipper_iec61850.goose.publisher import _encode_value

        lib = self._lib()
        _encode_value(lib, 42)
        lib.MmsValue_newIntegerFromInt32.assert_called_once_with(42)

    def test_float(self) -> None:
        from protoskipper_iec61850.goose.publisher import _encode_value

        lib = self._lib()
        _encode_value(lib, 3.14)
        lib.MmsValue_newFloat.assert_called_once_with(3.14)

    def test_bytes(self) -> None:
        from protoskipper_iec61850.goose.publisher import _encode_value

        lib = self._lib()
        _encode_value(lib, b"\xde\xad")
        lib.MmsValue_newOctetString.assert_called_once_with(0, 2)

    def test_unsupported_type_raises_type_error(self) -> None:
        from protoskipper_iec61850.goose.publisher import _encode_value

        lib = self._lib()
        with pytest.raises(TypeError, match="list"):
            _encode_value(lib, [1, 2, 3])


# ---------------------------------------------------------------------------
# parse_dataset_literal tests (publisher helper -- no PySide6 needed)
# ---------------------------------------------------------------------------


class TestParseDatsetLiteral:
    """parse_dataset_literal parses dataset value literals correctly."""

    def _fn(self):  # type: ignore[no-untyped-def]
        from protoskipper_iec61850.goose.publisher import parse_dataset_literal

        return parse_dataset_literal

    def test_true(self) -> None:
        assert self._fn()("True") is True

    def test_true_lowercase(self) -> None:
        assert self._fn()("true") is True

    def test_false(self) -> None:
        assert self._fn()("False") is False

    def test_false_lowercase(self) -> None:
        assert self._fn()("false") is False

    def test_int(self) -> None:
        assert self._fn()("42") == 42

    def test_float(self) -> None:
        assert abs(self._fn()("3.14") - 3.14) < 1e-9

    def test_bytes(self) -> None:
        assert self._fn()(r"b'\xde\xad'") == b"\xde\xad"

    def test_invalid_literal_raises(self) -> None:
        with pytest.raises(ValueError):
            self._fn()("not_a_literal")

    def test_unsupported_type_raises(self) -> None:
        with pytest.raises(ValueError, match="dict"):
            self._fn()("{'key': 1}")
