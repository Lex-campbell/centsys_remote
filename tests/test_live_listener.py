"""Tests for the persistent live listener.

Covers the transport (``mqtt_remote.listen_overview_blocking`` -- frame dispatch,
stop/return semantics, wake publishing) and the ``LiveListener`` supervisor
policy (target selection, reconnect/backoff, cert-expiry handling, active flag).

Everything is loaded from file paths with fake collaborators so it runs under
plain pytest with no Home Assistant install; ``paho-mqtt`` is only needed for the
``Properties``/``PacketTypes`` import inside the listen function.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import types
from contextlib import contextmanager
from pathlib import Path

_INT = Path(__file__).resolve().parents[1] / "custom_components" / "centsys_remote"
_API = _INT / "api"

# Synthetic package so mqtt_remote's ``from . import enums`` resolves.
_api_pkg = types.ModuleType("centsys_api")
_api_pkg.__path__ = [str(_API)]
sys.modules["centsys_api"] = _api_pkg

# Synthetic integration package so live_listener's ``from .const`` /
# ``from .api.exceptions`` resolve (neither pulls in Home Assistant).
_int_pkg = types.ModuleType("centsys_int")
_int_pkg.__path__ = [str(_INT)]
sys.modules["centsys_int"] = _int_pkg
_int_api_pkg = types.ModuleType("centsys_int.api")
_int_api_pkg.__path__ = [str(_API)]
sys.modules["centsys_int.api"] = _int_api_pkg


def _load(mod_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_load("centsys_api.enums", _API / "enums.py")
mqtt_remote = _load("centsys_api.mqtt_remote", _API / "mqtt_remote.py")
const = _load("centsys_int.const", _INT / "const.py")
exceptions = _load("centsys_int.api.exceptions", _API / "exceptions.py")
live_listener = _load("centsys_int.live_listener", _INT / "live_listener.py")


def _v2_frame(gate_st: int = 1) -> bytes:
    """A minimal 4-byte header + 36-byte v2 body with gate status at offset 22."""
    body = bytearray(36)
    body[22] = gate_st
    return b"\x00\x00\x00\x00" + bytes(body)


def _fake_session_factory(published: list, *, deliver=None, auto_stop: threading.Event | None = None):
    """Build a fake ``operator_session`` that records publishes and can deliver a frame."""

    @contextmanager
    def fake_session(
        *,
        on_connect,
        on_message,
        on_subscribe=None,
        on_disconnect=None,
        on_close=None,
        **_kw,
    ):
        class _Client:
            def subscribe(self, *a, **k):
                pass

            def publish(self, topic, payload=b"", qos=0, properties=None):
                published.append(topic)

        client = _Client()
        on_connect(client, None, None, None)
        if on_subscribe is not None:
            on_subscribe(client, None, None, None)
        if deliver is not None:
            topic, payload = deliver
            msg = types.SimpleNamespace(
                topic=topic,
                payload=payload,
                properties=types.SimpleNamespace(UserProperty=[]),
            )
            on_message(client, None, msg)
        if auto_stop is not None:
            auto_stop.set()  # so the hold-open loop returns immediately
        try:
            yield client
        finally:
            if on_close is not None:
                on_close(client)

    return fake_session


# --- transport: listen_overview_blocking ---------------------------------


def test_listen_dispatches_frame_and_stops():
    serial = "SER1"
    received: list = []
    connected: list = []
    stop = threading.Event()
    published: list = []

    fake = _fake_session_factory(
        published, deliver=(f"{serial}/deviceOverview", _v2_frame(1)), auto_stop=stop
    )
    orig, mqtt_remote.operator_session = mqtt_remote.operator_session, fake
    try:
        reason = mqtt_remote.listen_overview_blocking(
            host="h",
            port=1,
            client_id="mcr:1:ha",
            serials=[serial],
            cert_pem=b"c",
            key_pem=b"k",
            on_overview=lambda s, ov: received.append((s, ov)),
            on_connected=lambda: connected.append(True),
            stop_event=stop,
            poll_interval=0.01,
        )
    finally:
        mqtt_remote.operator_session = orig

    assert reason == "stopped"
    assert connected == [True]
    assert len(received) == 1
    got_serial, ov = received[0]
    assert got_serial == serial
    assert ov.gate_status == "closed"


def test_listen_sends_wakes_and_disconnect():
    serial = "SER1"
    published: list = []
    stop = threading.Event()

    fake = _fake_session_factory(published, auto_stop=stop)
    orig, mqtt_remote.operator_session = mqtt_remote.operator_session, fake
    try:
        mqtt_remote.listen_overview_blocking(
            host="h",
            port=1,
            client_id="mcr:1:ha",
            serials=[serial],
            cert_pem=b"c",
            key_pem=b"k",
            on_overview=lambda s, o: None,
            stop_event=stop,
            wakes={serial: b"\x01\x01\x01\x01"},
            poll_interval=0.01,
        )
    finally:
        mqtt_remote.operator_session = orig

    assert f"{serial}/connectionRequest" in published
    assert f"{serial}/userRemoteTrigger" in published
    assert f"{serial}/disconnect" in published  # on_close release (wake path)


def test_listen_passive_publishes_nothing():
    serial = "SER1"
    published: list = []
    stop = threading.Event()

    fake = _fake_session_factory(published, auto_stop=stop)
    orig, mqtt_remote.operator_session = mqtt_remote.operator_session, fake
    try:
        mqtt_remote.listen_overview_blocking(
            host="h",
            port=1,
            client_id="mcr:1:ha",
            serials=[serial],
            cert_pem=b"c",
            key_pem=b"k",
            on_overview=lambda s, o: None,
            stop_event=stop,
            poll_interval=0.01,
        )
    finally:
        mqtt_remote.operator_session = orig

    # Purely passive: no connectionRequest, no wake, no disconnect release.
    assert published == []


# --- supervisor: LiveListener --------------------------------------------


class _FakeLoop:
    def __init__(self) -> None:
        self._t = 0.0

    def time(self) -> float:
        self._t += 1.0
        return self._t

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


class _FakeHass:
    def __init__(self) -> None:
        self.loop = _FakeLoop()


class _FakeDevice:
    def __init__(self, mac) -> None:
        self.mac_address = mac


class _FakeClient:
    def __init__(self, script, stop_after=None) -> None:
        self.script = list(script)
        self.stop_after = stop_after
        self.calls: list = []
        self.listener = None

    async def run_live_listener(
        self, serials, *, macs, on_overview, on_connected, stop_event, wake_interval, au=False
    ) -> str:
        self.calls.append(
            {"serials": list(serials), "macs": dict(macs), "wake_interval": wake_interval}
        )
        n = len(self.calls)
        action = self.script[n - 1] if n - 1 < len(self.script) else "dropped"
        if self.stop_after and n >= self.stop_after and self.listener is not None:
            self.listener._stop = True
        if action == "cert":
            raise exceptions.CentsysCertExpiredError("expired")
        on_connected()
        return "dropped"


class _FakeCoord:
    def __init__(self, client, data) -> None:
        self.hass = _FakeHass()
        self.client = client
        self.data = data
        self.updates = 0
        self.ingested: list = []

    def async_spawn(self, coro, name=None):
        coro.close()  # not used in these direct-_run tests

    def async_update_listeners(self):
        self.updates += 1

    def _thread_safe_ingest(self, serial, overview):
        self.ingested.append((serial, overview))


def test_targets_selects_wifi_with_macs():
    coord = _FakeCoord(
        _FakeClient([]),
        {
            "S1": {"kind": "wifi", "device": _FakeDevice("AA:BB:CC:DD")},
            "G1": {"kind": "gsm", "gsm_device": object()},
            "S2": {"kind": "wifi", "device": _FakeDevice(None)},
            "SH": {"kind": "shared"},
        },
    )
    listener = live_listener.LiveListener(coord)
    serials, macs = listener._targets()
    assert set(serials) == {"S1", "S2"}
    assert macs == {"S1": "AA:BB:CC:DD"}


def test_supervisor_reconnects_with_backoff_and_survives_cert_expiry():
    coord = _FakeCoord(
        _FakeClient(["cert", "dropped", "dropped"], stop_after=3),
        {"S1": {"kind": "wifi", "device": _FakeDevice("AA:BB:CC:DD")}},
    )
    listener = live_listener.LiveListener(coord)
    coord.client.listener = listener

    sleeps: list = []

    async def _fake_sleep(d):
        sleeps.append(d)

    saved, live_listener.asyncio = live_listener.asyncio, types.SimpleNamespace(sleep=_fake_sleep)
    try:
        asyncio.run(listener._run())
    finally:
        live_listener.asyncio = saved

    calls = coord.client.calls
    assert len(calls) == 3  # cert-expiry did not abort the supervisor
    assert calls[0]["serials"] == ["S1"]
    assert calls[0]["macs"] == {"S1": "AA:BB:CC:DD"}
    assert calls[0]["wake_interval"] == float(const.TELEMETRY_SCAN_INTERVAL)
    # Backoff grows on repeated fast drops: MIN, then MIN*2 (two gaps, three tries).
    assert sleeps == [const.LISTENER_RECONNECT_MIN, const.LISTENER_RECONNECT_MIN * 2]
    # A connection reported connected at least once, toggling the active flag.
    assert coord.updates >= 1
    assert listener.active is False  # cleared after the final drop/stop
