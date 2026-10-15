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
            on_subscribe(client, None, None, [0])
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

    def invalidate_wifi_positions(self):
        self.invalidated = True

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
    assert calls[0]["wake_interval"] == float(const.POSITION_IDLE_REFRESH_INTERVAL)
    # Backoff grows on repeated fast drops: MIN, then MIN*2 (two gaps, three tries).
    assert sleeps == [const.LISTENER_RECONNECT_MIN, const.LISTENER_RECONNECT_MIN * 2]
    # A connection reported connected at least once, toggling the active flag.
    assert coord.updates >= 1
    assert listener.active is False  # cleared after the final drop/stop


def test_retained_position_is_not_a_fresh_measurement():
    msg = types.SimpleNamespace(
        payload=_v2_frame(1), retain=True,
        properties=types.SimpleNamespace(UserProperty=[]),
    )
    assert mqtt_remote.overview_from_message(msg) is None


def test_disconnect_clears_verified_positions():
    coord = _FakeCoord(_FakeClient([]), {})
    listener = live_listener.LiveListener(coord)
    listener._set_connected(True)
    listener._set_connected(False)
    assert coord.invalidated is True


def test_listener_rejects_failed_subscription(monkeypatch):
    @contextmanager
    def rejected(*, on_subscribe, **kwargs):
        on_subscribe(None, None, 1, [128])
        yield object()
    monkeypatch.setattr(mqtt_remote, 'operator_session', rejected)
    connected = []
    assert mqtt_remote.listen_overview_blocking(
        host='h', port=1, client_id='test', serials=['S'],
        cert_pem=b'c', key_pem=b'k', on_overview=lambda *a: None,
        stop_event=threading.Event(), on_connected=lambda: connected.append(True),
    ) == 'dropped'
    assert not connected


# A cold, stationary operator ignores cmd01 before its connection response.
# These transports model that device behavior, not the implementation's order.
import pytest


def _bootstrap(monkeypatch, *, statuses=None, ack_at=2, respond_on_attempt=1,
               stop_at=12, serials=("S",), wake_interval=60, retained=False,
               retained_ack=False):
    now = [0.0]
    published = []
    received = []
    requests = {s: 0 for s in serials}
    callbacks = {}
    statuses = statuses or {s: 1 for s in serials}

    def deliver(topic, payload=b"", *, retain=False):
        callbacks["message"](callbacks["client"], None, types.SimpleNamespace(
            topic=topic, payload=payload, retain=retain,
            properties=types.SimpleNamespace(UserProperty=[]),
        ))

    class Stop:
        def is_set(self):
            return now[0] >= stop_at
        def wait(self, interval):
            now[0] += interval
            if now[0] == ack_at:
                for serial in serials:
                    if statuses.get(serial) is not None:
                        deliver(f"{serial}/connectionRequestResponse", retain=retained_ack)
            return self.is_set()

    @contextmanager
    def session(*, on_connect, on_subscribe, on_message, on_close=None, **kwargs):
        class Client:
            def subscribe(self, *args, **kwargs):
                pass
            def publish(self, topic, payload=b"", **kwargs):
                published.append((now[0], topic, payload))
                serial = topic.split("/")[0]
                if topic.endswith("/connectionRequest"):
                    requests[serial] += 1
                    if now[0] >= ack_at and statuses.get(serial) is not None:
                        deliver(f"{serial}/connectionRequestResponse", retain=retained_ack)
                elif topic.endswith("/userRemoteTrigger"):
                    deliver(f"{serial}/userRemoteTriggerResponse", b"challenge")
                    if now[0] >= ack_at and requests[serial] >= respond_on_attempt:
                        deliver(f"{serial}/deviceOverview", _v2_frame(statuses[serial]), retain=retained)
        client = Client()
        callbacks.update(client=client, message=on_message)
        on_connect(client, None, None, 0)
        on_subscribe(client, None, 1, [0])
        try:
            yield client
        finally:
            if on_close:
                on_close(client)

    monkeypatch.setattr(mqtt_remote, "operator_session", session)
    monkeypatch.setattr(mqtt_remote.time, "monotonic", lambda: now[0])
    result = mqtt_remote.listen_overview_blocking(
        host="offline", port=1, client_id="test", serials=serials,
        cert_pem=b"c", key_pem=b"k", on_overview=lambda s, ov: received.append((s, ov)),
        stop_event=Stop(), wakes={s: b"identity-only" for s in serials},
        connect_timeout=3, poll_interval=1, wake_interval=wake_interval,
    )
    return result, published, received, requests


@pytest.mark.parametrize("status,label", [(1, "closed"), (0, "open")])
def test_cold_stationary_status_without_remote_movement(monkeypatch, status, label):
    result, published, received, requests = _bootstrap(monkeypatch, statuses={"S": status})
    assert result == "stopped"
    assert [(s, ov.gate_status) for s, ov in received] == [("S", label)]
    identity = [(t, payload) for t, topic, payload in published if topic.endswith("/userRemoteTrigger")]
    assert identity == [(2, b"identity-only")]
    assert requests == {"S": 1}
    assert published[-1][1] == "S/disconnect"


def test_lost_first_status_is_retried_without_movement(monkeypatch):
    _, published, received, requests = _bootstrap(monkeypatch, respond_on_attempt=2)
    assert requests == {"S": 2}
    assert received[0][1].gate_status == "closed"
    assert all(payload == b"identity-only" for _, topic, payload in published if topic.endswith("/userRemoteTrigger"))


def test_silent_operator_has_bounded_retries_and_no_trigger(monkeypatch):
    _, published, received, requests = _bootstrap(monkeypatch, statuses={"S": None})
    assert requests == {"S": 3}
    assert not received
    assert not any(topic.endswith("/userRemoteTrigger") for _, topic, _ in published)


def test_silent_operator_does_not_block_other_operator(monkeypatch):
    _, published, received, requests = _bootstrap(
        monkeypatch, serials=("S", "OTHER"), statuses={"S": None, "OTHER": 0},
    )
    assert [(s, ov.gate_status) for s, ov in received] == [("OTHER", "open")]
    assert requests == {"S": 3, "OTHER": 1}


def test_stop_before_ack_sends_no_identity(monkeypatch):
    _, published, received, _ = _bootstrap(monkeypatch, stop_at=1)
    assert not received
    assert not any(topic.endswith("/userRemoteTrigger") for _, topic, _ in published)


def test_retained_status_does_not_complete_bootstrap(monkeypatch):
    _, _, received, requests = _bootstrap(monkeypatch, retained=True, stop_at=16)
    assert not received
    assert requests == {"S": 3}


def test_idle_cycle_refreshes_without_remote_movement(monkeypatch):
    _, _, received, requests = _bootstrap(monkeypatch, wake_interval=20, stop_at=25)
    assert requests == {"S": 2}
    assert len(received) == 2


def test_retained_handshake_ack_cannot_start_identity_request(monkeypatch):
    _, published, received, requests = _bootstrap(monkeypatch, retained_ack=True)
    assert requests == {"S": 3}
    assert not received
    assert not any(topic.endswith("/userRemoteTrigger") for _, topic, _ in published)


def test_unknown_measurement_keeps_bootstrap_retrying(monkeypatch):
    _, _, received, requests = _bootstrap(monkeypatch, statuses={"S": 255}, stop_at=16)
    assert requests == {"S": 3}
    assert all(ov.gate_status is None for _, ov in received)


def test_bootstrap_never_echoes_activation_challenge(monkeypatch):
    _, published, _, _ = _bootstrap(monkeypatch)
    assert [(topic, payload) for _, topic, payload in published] == [
        ("S/connectionRequest", b""),
        ("S/userRemoteTrigger", b"identity-only"),
        ("S/disconnect", b""),
    ]


# Diagnostic evidence must distinguish missing handshake, rejected cached data,
# and decoded frames with no position, without exposing identifiers or contents.
def test_startup_diagnostics_missing_handshake(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger=mqtt_remote.__name__)
    _bootstrap(monkeypatch, statuses={"S": None})
    assert "stage=handshake counts={}" in caplog.text
    assert "sending identity only" not in caplog.text


def test_startup_diagnostics_retained_frame(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger=mqtt_remote.__name__)
    _bootstrap(monkeypatch, retained=True, stop_at=16)
    assert "kind=identity_reply" in caplog.text
    assert "retained=True decoded=False position_known=False" in caplog.text
    assert "stage=position" in caplog.text
    assert "S/" not in caplog.text
    assert "challenge" not in caplog.text


def test_startup_diagnostics_unknown_position(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger=mqtt_remote.__name__)
    _bootstrap(monkeypatch, statuses={"S": 255}, stop_at=16)
    assert "decoded=True position_known=False" in caplog.text
    assert "stage=position" in caplog.text


def test_startup_diagnostics_are_bounded_and_metadata_only(caplog):
    caplog.set_level("DEBUG", logger=mqtt_remote.__name__)
    diagnostics = mqtt_remote._StartupDiagnostics("listener-slot-1")
    msg = types.SimpleNamespace(topic="PRIVATE_DEVICE/overview", payload=b"PRIVATE_TOKEN",
                                retain=False, properties={"secret": "PRIVATE_SECRET"})
    for _ in range(100):
        diagnostics.record("overview", msg, decoded=False)
    assert diagnostics.counts == {"overview": 100}
    assert len(caplog.records) == 3
    assert "PRIVATE" not in caplog.text
