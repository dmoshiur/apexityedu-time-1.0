"""EMS clients: iClock wire format, REST contract, protocol probe, errors."""
from __future__ import annotations

import pytest

from apexity_bridge.ems.base import (
    EmsAuthError, EmsProtocolError, EmsUnavailableError,
)
from apexity_bridge.ems.iclock import IclockEmsClient
from apexity_bridge.ems.probe import probe_ems
from apexity_bridge.ems.rest import RestEmsClient
from fakes import FakeIclockServer, FakeRestServer


def _events(n=3, serial="SN-A"):
    return [
        {
            "id": i + 1,
            "dedup_hash": f"hash-{i}",
            "device_serial": serial,
            "device_user_id": str(i + 1),
            "event_timestamp": f"2026-10-15T08:0{i}:00",
            "event_timestamp_tz": "Asia/Dhaka",
            "status": i,
            "punch": 1,
            "verify_mode": 1,
            "ems_student_id": f"S100{i}",
            "retry_count": 0,
        }
        for i in range(n)
    ]


# ------------------------------------------------------------------ iClock --
@pytest.fixture()
def iclock_server():
    server = FakeIclockServer()
    yield server
    server.stop()


def test_iclock_health_and_handshake(iclock_server):
    client = IclockEmsClient(iclock_server.url, path="/iclock")
    health = client.health_check()
    assert health.ok
    assert client.handshake("SN-A")


def test_iclock_registration_accepts_standard_get_option_handshake(iclock_server):
    client = IclockEmsClient(iclock_server.url, path="/iclock")
    result = client.register_device(
        serial="SN-A", model="K40", firmware="6.60", label="Main gate"
    )

    assert result.ok
    assert result.remote_device_id == "SN-A"
    assert "handshake OK" in result.detail


def test_iclock_push_uses_zk_wire_format(iclock_server):
    client = IclockEmsClient(iclock_server.url, path="/iclock")
    outcome = client.push_attendance(_events(3))
    assert set(outcome.accepted_ids) == {1, 2, 3}
    assert len(iclock_server.accepted) == 3
    first = iclock_server.accepted[0]
    assert first["sn"] == "SN-A"
    assert first["pin"] == "1"
    assert first["ts"] == "2026-10-15 08:00:00"       # space-separated, as ZK
    assert first["status"] == "0"
    assert first["verify"] == "1"


def test_iclock_requires_same_serial_batch(iclock_server):
    client = IclockEmsClient(iclock_server.url)
    mixed = _events(2, "SN-A") + _events(1, "SN-B")
    mixed[2]["id"] = 99
    with pytest.raises(EmsProtocolError):
        client.push_attendance(mixed)


def test_iclock_partial_acceptance(iclock_server):
    iclock_server.mode = "partial"
    client = IclockEmsClient(iclock_server.url)
    outcome = client.push_attendance(_events(3))
    assert outcome.partial_index == 1
    assert outcome.accepted_ids == (1,)


def test_iclock_error_body_is_protocol_error(iclock_server):
    iclock_server.mode = "error"
    client = IclockEmsClient(iclock_server.url)
    with pytest.raises(EmsProtocolError):
        client.push_attendance(_events(1))


def test_iclock_server_5xx_is_retryable(iclock_server):
    iclock_server.mode = "http500"
    client = IclockEmsClient(iclock_server.url)
    with pytest.raises(EmsUnavailableError):
        client.push_attendance(_events(1))


def test_iclock_roster_is_honestly_unsupported(iclock_server):
    client = IclockEmsClient(iclock_server.url)
    with pytest.raises(EmsProtocolError, match="roster"):
        client.fetch_students()


def test_iclock_token_added_to_handshake(iclock_server, monkeypatch):
    client = IclockEmsClient(iclock_server.url, auth_token="sekrit")
    captured = {}
    real_endpoint = client._endpoint

    def capture(suffix, serial, **params):
        captured["url"] = real_endpoint(suffix, serial, **params)
        return captured["url"]

    monkeypatch.setattr(client, "_endpoint", capture)
    client.health_check()
    assert "AuthToken=sekrit" in captured["url"]


# -------------------------------------------------------------------- REST --
@pytest.fixture()
def rest_server():
    server = FakeRestServer()
    yield server
    server.stop()


def _rest_client(server, token=FakeRestServer.TOKEN):
    return RestEmsClient(server.url, token_provider=lambda: token)


def test_rest_health_and_register(rest_server):
    client = _rest_client(rest_server)
    assert client.health_check().ok
    result = client.register_device(serial="SN-A", model="K40", firmware="6.60", label="Gate")
    assert result.ok and result.remote_device_id == "dev-SN-A"
    assert rest_server.registered_devices[0]["serial"] == "SN-A"


def test_rest_students_pagination(rest_server):
    client = _rest_client(rest_server)
    page1 = client.fetch_students()
    assert len(page1.students) == 2                       # server.page_size = 2
    assert page1.next_cursor == "2"
    page2 = client.fetch_students(page1.next_cursor)
    assert len(page2.students) == 1
    assert page2.complete
    assert page1.students[0].name == "Afnan Rahman"
    assert page2.students[0].active is False


def test_rest_push_accept_duplicate_reject(rest_server):
    client = _rest_client(rest_server)
    events = _events(3)
    first = client.push_attendance(events)
    assert set(first.accepted_ids) == {1, 2, 3}
    assert first.remote_ids  # server returned ids

    second = client.push_attendance(events)               # re-push after "crash"
    assert set(second.duplicates) == {1, 2, 3}
    assert not second.accepted_ids

    bad = [{"id": 9, "dedup_hash": "bad", "device_serial": "SN-A",
            "device_user_id": "", "event_timestamp": "",
            "event_timestamp_tz": "Asia/Dhaka", "status": 0, "punch": 1}]
    third = client.push_attendance(bad)
    assert "bad" in third.rejected


def test_rest_missing_token_is_auth_error(rest_server):
    client = RestEmsClient(rest_server.url, token_provider=lambda: None)
    with pytest.raises(EmsAuthError, match="set-token"):
        client.health_check()


def test_rest_wrong_token_unauthorized(rest_server):
    client = RestEmsClient(rest_server.url, token_provider=lambda: "WRONG")
    with pytest.raises(EmsAuthError):
        client.health_check()


def test_rest_5xx_is_retryable(rest_server):
    rest_server.mode = "http500"
    with pytest.raises(EmsUnavailableError):
        _rest_client(rest_server).health_check()


def test_rest_non_json_is_protocol_error(rest_server):
    rest_server.mode = "badjson"
    with pytest.raises(EmsProtocolError):
        _rest_client(rest_server).health_check()


# -------------------------------------------------------------------- probe --
def test_probe_detects_iclock(iclock_server):
    report = probe_ems(iclock_server.url)
    assert report.reachable
    assert report.iclock_verdict == "yes"
    assert report.recommended_protocol == "iclock"
    assert report.rest_verdict == "no"


def test_probe_detects_rest(rest_server):
    report = probe_ems(rest_server.url)
    # REST health endpoint requires auth → 401 means the endpoint exists.
    assert report.rest_verdict == "yes"
    assert report.recommended_protocol in ("rest", "iclock")


def test_probe_unreachable_reports_errors():
    report = probe_ems("http://127.0.0.1:9", timeout=2)
    assert not report.reachable
    assert report.errors
    assert report.recommended_protocol is None
    assert "iClock" in report.human() or "iClock endpoint" in report.human()
