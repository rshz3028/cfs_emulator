"""Telemetry intake of the Ground Station and the UDP route it uses."""
import types

import pytest

from ground_station.station import GroundStation
from hardware import ground_link


def packet(mid, payload=b'\x00\x00', sequence=1):
    """CCSDS space packet: 6-byte primary header whose length field is the data length minus one."""
    return (mid.to_bytes(2, 'big') + (0xC000 | sequence).to_bytes(2, 'big')
            + (len(payload) - 1).to_bytes(2, 'big') + payload)


@pytest.fixture
def station(tmp_path):
    station = GroundStation(tmp_path)
    station._official = types.SimpleNamespace(RoutingService=types.SimpleNamespace(
        get_pkt_id=lambda data: hex(int.from_bytes(data[:2], 'big'))))
    station._definitions_files = types.SimpleNamespace(pages={})
    station._definitions = {0x0883: {'name': 'SAMPLE_APP_HkTlm_t', 'app': 'sample_app'}}
    return station


def test_accepts_a_well_formed_packet(station):
    station._accept(packet(0x0883, sequence=7))
    snapshot = station.snapshot()
    assert (snapshot['received'], snapshot['malformed']) == (1, 0)
    received = snapshot['packets'][0]
    assert (received['name'], received['app'], received['mid'], received['sequence'], received['count']) == (
        'SAMPLE_APP_HkTlm_t', 'sample_app', 0x0883, 7, 1)


def test_unknown_message_id_is_kept_under_its_number(station):
    station._accept(packet(0x0999))
    assert station.snapshot()['packets'][0]['name'] == '0x999'


@pytest.mark.parametrize('data', [
    b'\x08\x83\xc0',                       # shorter than a primary header
    packet(0x0883) + b'\x00',              # longer than its length field
    packet(0x0883)[:-1],                   # shorter than its length field
    bytes([0x28]) + packet(0x0883)[1:],    # CCSDS version other than 0
])
def test_counts_malformed_packets_without_decoding_them(station, data):
    station._accept(data)
    snapshot = station.snapshot()
    assert (snapshot['received'], snapshot['malformed']) == (0, 1)
    assert snapshot['error'] == 'Invalid CCSDS version or packet size'


def test_packets_after_returns_only_newer_packets_in_order(station):
    for sequence in range(3):
        station._accept(packet(0x0883, sequence=sequence))
    every = station.packets_after(0)
    assert [item['sequence'] for item in every] == [0, 1, 2]
    assert [item['sequence'] for item in station.packets_after(every[0]['event_id'])] == [1, 2]
    assert station.packets_after(every[-1]['event_id']) == []


def test_packets_after_does_not_wait_when_disconnected(station):
    assert station.connected() is False
    assert station.packets_after(0, timeout=30) == []


def test_send_requires_a_connection(station):
    with pytest.raises(ValueError, match='Connect the ground station before sending commands'):
        station.send('SAMPLE_APP_NOOP_CC')


def test_native_link_uses_the_loopback_address():
    catalog = {'transport': {'command_port': 1234, 'telemetry_port': 1235}}
    assert ground_link.resolve_ground_link(None, 'native', catalog) == {
        'host': '127.0.0.1', 'listen_host': '127.0.0.1', 'return_host': '127.0.0.1'}


@pytest.mark.parametrize('transport', [{}, {'command_port': 0, 'telemetry_port': 1235},
                                       {'command_port': 1234, 'telemetry_port': '1235'},
                                       {'command_port': 70000, 'telemetry_port': 1235}])
def test_link_needs_valid_ports_from_the_build(transport):
    with pytest.raises(ValueError, match='no valid UDP transport ports'):
        ground_link.resolve_ground_link(None, 'native', {'transport': transport})


@pytest.mark.parametrize('address', ['127.0.0.1', '0.0.0.0', '224.0.0.1', '255.255.255.255', 'board.local', '::1'])
def test_board_link_rejects_addresses_that_cannot_carry_telemetry(address):
    with pytest.raises(ValueError):
        ground_link._ipv4(address)


def test_qemu_forward_is_recognized_only_for_the_same_guest_and_port():
    info = ('Hub 0 (net0):\n'
            '  Protocol[State]    FD  Source Address  Port   Dest. Address  Port RecvQ SendQ\n'
            '  UDP[HOST_FORWARD]  13      172.17.0.2  1234       10.0.2.15  1234     0     0\n')
    assert ground_link._forwarded(info, 'net0', '172.17.0.2', '10.0.2.15', 1234) is True
    assert ground_link._forwarded(info, 'net1', '172.17.0.2', '10.0.2.15', 1234) is False
    assert ground_link._forwarded(info, 'net0', '172.17.0.2', '10.0.2.15', 1235) is False
    with pytest.raises(ValueError, match='already forwards'):
        ground_link._forwarded(info, 'net0', '172.17.0.2', '10.0.2.16', 1234)
