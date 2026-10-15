"""Execute production methods with deterministic HA collaborators (no HA install).

These are source-level unit regressions, not HA startup or hardware tests.
"""
import ast
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/centsys_remote'


def load_methods(file, name, methods, **deps):
    tree = ast.parse((ROOT / file).read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    cls.bases = []
    cls.body = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in methods]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[])
    ns = dict(Any=Any, asyncio=asyncio, time=time, LIVE_STATUS_TTL=20, STATIONARY_POSITION_TTL=90, HomeAssistantError=RuntimeError, **deps)
    exec(compile(ast.fix_missing_locations(module), str(ROOT / file), 'exec'), ns)
    return ns[name]


Coordinator = load_methods('coordinator.py', 'CentsysCoordinator', {
    'set_live_gate_status', 'live_gate_status', '_live_status_label', 'ingest_overview', 'invalidate_wifi_positions'
})
Cover = load_methods('cover.py', 'CentsysGateCover', {
    '_live', 'is_closed', 'is_opening', 'is_closing', '_move_to', '_trigger', 'async_open_cover', 'async_close_cover'
}, CentsysCertExpiredError=type('CertError', (Exception,), {}), CentsysError=type('ApiError', (Exception,), {}))
Sensor = load_methods('sensor.py', 'CentsysSensor', {'native_value'})
Binary = load_methods('binary_sensor.py', 'CentsysBinarySensor', {'is_on'})


@pytest.fixture
def rig(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    handles = []
    def call_later(delay, fn, *args):
        handle = SimpleNamespace(cancelled=False, fn=fn, args=args)
        handle.cancel = lambda: setattr(handle, 'cancelled', True)
        handles.append(handle)
        return handle
    c = Coordinator()
    c.hass = SimpleNamespace(loop=SimpleNamespace(call_later=call_later))
    c._live_status = {}; c._position_expiry = {}; c._overview = {}
    c.last_update_success = True
    c.data = {'S': {'kind': 'wifi', 'device': SimpleNamespace(is_online=True), 'status': SimpleNamespace(is_closed=True)}}
    c._remember_product_code = lambda *args: None
    notifications = []
    c.async_update_listeners = lambda: notifications.append(True)
    cover = Cover(); cover.coordinator = c; cover._serial = 'S'
    return c, cover, clock, handles, notifications


def test_http_closed_cannot_masquerade_as_position(rig):
    c, cover, *_ = rig
    assert cover.is_closed is None
    assert not cover.is_opening and not cover.is_closing


@pytest.mark.parametrize('live', [False, True])
@pytest.mark.parametrize('label,closed', [('open', False), ('closed', True), ('opening', False), ('closing', False)])
def test_every_telemetry_producer_overrides_http(rig, live, label, closed):
    c, cover, *_ = rig
    c.ingest_overview('S', SimpleNamespace(gate_status=label), live=live)
    assert cover.is_closed is closed
    assert cover.is_opening == (label == 'opening')
    assert cover.is_closing == (label == 'closing')


def test_expiry_publishes_unknown_without_waiting_for_poll(rig):
    c, cover, clock, handles, notifications = rig
    c.set_live_gate_status('S', 'open')
    clock[0] += 90
    handles[-1].fn(*handles[-1].args)
    assert cover.is_closed is None
    assert len(notifications) == 2


def test_new_frame_cancels_old_expiry(rig):
    c, cover, clock, handles, _ = rig
    c.set_live_gate_status('S', 'open'); old = handles[-1]
    clock[0] += 10
    c.set_live_gate_status('S', 'closed')
    assert old.cancelled
    assert cover.is_closed is True


def test_sensor_matches_unknown_and_verified_cover(rig):
    c, cover, *_ = rig
    s = Sensor(); s.coordinator = c; s._serial = 'S'; s._device_data = c.data['S']
    s.entity_description = SimpleNamespace(key='operator_status')
    assert s.native_value is None
    c.set_live_gate_status('S', 'open')
    assert s.native_value == 'open' and cover.is_closed is False


def test_verified_connection_is_not_cloud_online(rig):
    c, _, clock, *_ = rig
    b = Binary(); b.coordinator = c; b._serial = 'S'; b._device_data = c.data['S']
    b.entity_description = SimpleNamespace(key='telemetry_connected')
    assert b.is_on is False
    c.set_live_gate_status('S', 'open'); assert b.is_on is True
    clock[0] += 91; assert b.is_on is False
    c.set_live_gate_status('S', 'closed'); c.data['S']['device'].is_online = False
    assert b.is_on is False


@pytest.mark.parametrize('label', [None, 'unknown', 'lost', 'opening', 'closing', 'partly_open', 'partly_closed'])
def test_directionless_command_refused_without_stationary_endpoint(rig, label):
    c, cover, *_ = rig
    cover._device_data = {'device': SimpleNamespace(mac_address='test')}
    c.set_live_gate_status('S', label)
    triggers = []
    async def read(*a, **kw):
        pytest.fail('A command must not start an independent telemetry session')
    async def trigger(**kw): triggers.append(True)
    cover._read_overview = read; cover._trigger = trigger
    with pytest.raises(RuntimeError): asyncio.run(cover._move_to('open'))
    assert triggers == []


@pytest.mark.parametrize('target,label,count', [('open','open',0), ('closed','closed',0), ('open','closed',1), ('closed','open',1)])
def test_directional_actions_are_idempotent(rig, target, label, count):
    c, cover, *_ = rig
    cover._device_data = {'device': SimpleNamespace(mac_address='test')}
    c.set_live_gate_status('S', label)
    triggers = []
    async def read(*a, **kw): pytest.fail('Unexpected independent telemetry read')
    async def trigger(**kw):
        assert kw == {'expected_endpoint': label}
        triggers.append(True)
    cover._read_overview = read; cover._trigger = trigger
    asyncio.run(cover._move_to(target))
    assert len(triggers) == count


def test_stationary_and_motion_have_explicit_different_lifetimes(rig):
    c, cover, clock, _, _ = rig
    c.set_live_gate_status('S', 'open')
    clock[0] += 89
    assert cover.is_closed is False
    clock[0] += 1
    assert cover.is_closed is None
    c.set_live_gate_status('S', 'opening')
    clock[0] += 20
    assert cover.is_closed is None


def test_unknown_measurement_invalidates_previous_endpoint(rig):
    c, cover, *_ = rig
    c.set_live_gate_status('S', 'closed')
    c.ingest_overview('S', SimpleNamespace(gate_status='lost'), live=True)
    assert cover.is_closed is None


def test_disconnect_invalidates_only_wifi(rig):
    c, cover, *_ = rig
    c.set_live_gate_status('S', 'closed')
    c.data['G'] = {'kind': 'gsm'}
    c.set_live_gate_status('G', 'open')
    c.invalidate_wifi_positions()
    assert cover.is_closed is None
    assert c.live_gate_status('G') == 'open'


def test_gsm_retains_original_twenty_second_policy(rig):
    c, _, clock, *_ = rig
    c.data['G'] = {'kind': 'gsm'}
    c.set_live_gate_status('G', 'closed')
    clock[0] += 20
    assert c.live_gate_status('G') is None


def test_cloud_offline_invalidates_position_view(rig):
    c, cover, *_ = rig
    c.set_live_gate_status('S', 'open')
    c.data['S']['device'].is_online = False
    assert cover.is_closed is None


def test_concurrent_calls_are_serialized(rig):
    _, cover, *_ = rig
    cover._command_lock = asyncio.Lock()
    active = 0
    maximum = 0
    async def move(target):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
    cover._move_to = move
    async def run():
        await asyncio.gather(cover.async_open_cover(), cover.async_close_cover())
    asyncio.run(run())
    assert maximum == 1


def prepare_command(rig):
    c, cover, clock, *_ = rig
    cover._device_data = {'device': SimpleNamespace(mac_address='test')}
    cover._command_lock = asyncio.Lock()
    calls = []
    async def family(mac): return 'gate'
    async def activate(*args, **kwargs):
        calls.append(kwargs)
        return True
    async def refresh(): pass
    cover._resolve_family = family
    c.client = SimpleNamespace(open_gate=activate)
    c.async_request_refresh = refresh
    c.note_activation = lambda *args: None
    return c, cover, clock, calls


def test_live_endpoint_commands_without_one_shot_mqtt(rig):
    c, cover, _, calls = prepare_command(rig)
    async def forbidden(*args, **kwargs):
        pytest.fail('The working listener must not be bypassed by a second read')
    cover._read_overview = forbidden
    c.set_live_gate_status('S', 'closed')
    asyncio.run(cover.async_open_cover())
    assert len(calls) == 1
    assert calls[0]['is_garage'] is False
    assert cover._live is None  # consumed until another real frame arrives


def test_expired_endpoint_cannot_use_raw_cached_overview(rig):
    c, cover, clock, calls = prepare_command(rig)
    c.set_live_gate_status('S', 'closed')
    c.data['S']['overview'] = SimpleNamespace(gate_status='closed')
    clock[0] += 91
    with pytest.raises(RuntimeError, match='No fresh stationary endpoint'):
        asyncio.run(cover.async_open_cover())
    assert not calls


@pytest.mark.parametrize('new_state', [None, 'opening', 'open', 'partly_open'])
def test_position_rechecked_after_family_await(rig, new_state):
    c, cover, _, calls = prepare_command(rig)
    c.set_live_gate_status('S', 'closed')
    async def family(mac):
        await asyncio.sleep(0)
        c.set_live_gate_status('S', new_state)
        return 'gate'
    cover._resolve_family = family
    with pytest.raises(RuntimeError, match='changed or expired'):
        asyncio.run(cover.async_open_cover())
    assert not calls


def test_expiry_during_family_lookup_prevents_trigger(rig):
    c, cover, clock, calls = prepare_command(rig)
    c.set_live_gate_status('S', 'closed')
    async def family(mac):
        clock[0] += 91
        return 'gate'
    cover._resolve_family = family
    with pytest.raises(RuntimeError, match='changed or expired'):
        asyncio.run(cover.async_open_cover())
    assert not calls


def test_queued_commands_cannot_reuse_consumed_endpoint(rig):
    c, cover, _, calls = prepare_command(rig)
    c.set_live_gate_status('S', 'closed')
    async def run():
        return await asyncio.gather(cover.async_open_cover(), cover.async_close_cover(), return_exceptions=True)
    outcomes = asyncio.run(run())
    assert outcomes[0] is None
    assert isinstance(outcomes[1], RuntimeError)
    assert len(calls) == 1


def test_new_verified_endpoint_allows_next_command(rig):
    c, cover, _, calls = prepare_command(rig)
    c.set_live_gate_status('S', 'closed')
    asyncio.run(cover.async_open_cover())
    c.set_live_gate_status('S', 'open')
    asyncio.run(cover.async_close_cover())
    assert len(calls) == 2


def test_ack_failure_does_not_restore_old_endpoint(rig):
    c, cover, _, calls = prepare_command(rig)
    c.set_live_gate_status('S', 'closed')
    async def activate(*args, **kwargs): return False
    c.client.open_gate = activate
    with pytest.raises(RuntimeError, match='acknowledge'):
        asyncio.run(cover.async_open_cover())
    assert cover._live is None


@pytest.mark.parametrize("label,closed", [("closed", True), ("open", False)])
def test_first_refresh_position_uses_stationary_ttl_before_data_exists(rig, label, closed):
    c, cover, clock, _, _ = rig
    c.data = None
    c.ingest_overview("S", SimpleNamespace(gate_status=label), live=False, notify=False)
    # HA has not assigned the initial coordinator result until all discovery
    # calls finish; no movement or fresh frame is needed when it finally does.
    clock[0] += 25
    c.data = {"S": {"kind": "wifi", "device": SimpleNamespace(is_online=True)}}
    assert cover.is_closed is closed
    clock[0] += 65
    assert cover.is_closed is None
