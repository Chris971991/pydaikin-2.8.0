"""Tests for pydaikin.discovery hardening.

Covers: socket lifecycle (close / try-finally), advertised-port semantics
('port' from basic_info preserved, UDP reply port stored as 'udp_port'),
and tolerance of payloads without a 'name'.
"""

import socket

import pytest

from pydaikin.discovery import Discovery, get_name


class _FakeSock:
    """recvfrom() pops queued datagrams then times out; tracks close()."""

    def __init__(self, datagrams):
        self._datagrams = list(datagrams)
        self.closed = False

    def sendto(self, data, addr):
        pass

    def recvfrom(self, bufsiz):
        if self._datagrams:
            return self._datagrams.pop(0)
        raise socket.timeout()

    def close(self):
        self.closed = True


def _stub_discovery(datagrams):
    """Build a Discovery without binding a real UDP socket."""
    discovery = Discovery.__new__(Discovery)
    discovery.sock = _FakeSock(datagrams)
    discovery.dev = {}
    return discovery


def test_handle_datagram_preserves_advertised_non_udp_port():
    entry = Discovery._handle_datagram(
        b'ret=OK,mac=AABB,name=Kitchen,port=8080', ('1.2.3.4', 30050)
    )
    assert entry['ip'] == '1.2.3.4'
    assert entry['port'] == '8080'  # advertised value preserved
    assert entry['udp_port'] == '30050'  # reply source port kept separately


def test_handle_datagram_advertised_udp_port_kept_verbatim():
    # The common BRP069-era firmware advertises port=30050 (the UDP port
    # itself) in basic_info. Discovery keeps it verbatim; the factory
    # consumer maps it to None (see test_factory.py).
    entry = Discovery._handle_datagram(
        b'ret=OK,mac=AABB,name=Kitchen,port=30050', ('1.2.3.4', 30050)
    )
    assert entry['port'] == '30050'
    assert entry['udp_port'] == '30050'


def test_handle_datagram_without_advertised_port():
    entry = Discovery._handle_datagram(
        b'ret=OK,mac=AABB,name=Kitchen', ('1.2.3.4', 30050)
    )
    assert 'port' not in entry
    assert entry['udp_port'] == '30050'


def test_handle_datagram_without_name():
    entry = Discovery._handle_datagram(b'ret=OK,mac=AABB', ('1.2.3.4', 30050))
    assert entry is not None
    assert entry['mac'] == 'AABB'
    assert 'name' not in entry


@pytest.mark.parametrize(
    'payload',
    [
        b'garbage without equals',  # no parsable pairs -> missing 'ret'
        b'ret=KO,mac=AABB',  # rejected -> {'ret': 'KO'}, no mac
        b'ret=OK,name=Kitchen',  # OK but no mac
        b'\xff\xfe\xfd',  # undecodable UTF-8
    ],
)
def test_handle_datagram_invalid_payloads_return_none(payload):
    assert Discovery._handle_datagram(payload, ('1.2.3.4', 30050)) is None


def test_poll_stop_if_found_tolerates_missing_name():
    discovery = _stub_discovery([(b'ret=OK,mac=AABB', ('1.2.3.4', 30050))])
    devices = discovery.poll(stop_if_found='kitchen', ip='192.168.1.255')
    assert [d['mac'] for d in devices] == ['AABB']


def test_poll_stop_if_found_matches_case_insensitively():
    discovery = _stub_discovery(
        [
            (b'ret=OK,mac=AABB,name=Kitchen', ('1.2.3.4', 30050)),
            (b'ret=OK,mac=CCDD,name=Bedroom', ('1.2.3.5', 30050)),
        ]
    )
    devices = discovery.poll(stop_if_found='KITCHEN', ip='192.168.1.255')
    # stop early: only the matching device collected
    assert [d['mac'] for d in devices] == ['AABB']


def test_get_name_nameless_device_returns_none(monkeypatch):
    closed = []

    def fake_init(self):
        self.sock = _FakeSock([])
        self.dev = {}

    def fake_poll(self, stop_if_found=None, ip=None):
        return [{'mac': 'AABB', 'ip': '1.2.3.4', 'udp_port': '30050'}]

    monkeypatch.setattr(Discovery, '__init__', fake_init)
    monkeypatch.setattr(Discovery, 'poll', fake_poll)
    monkeypatch.setattr(
        Discovery, 'close', lambda self: closed.append(True), raising=True
    )

    assert get_name('kitchen') is None  # no KeyError on missing 'name'
    assert closed == [True]  # socket closed via try/finally


def test_get_name_closes_socket_even_when_poll_raises(monkeypatch):
    closed = []

    def fake_init(self):
        self.sock = _FakeSock([])
        self.dev = {}

    def fake_poll(self, stop_if_found=None, ip=None):
        raise OSError('network is unreachable')

    monkeypatch.setattr(Discovery, '__init__', fake_init)
    monkeypatch.setattr(Discovery, 'poll', fake_poll)
    monkeypatch.setattr(Discovery, 'close', lambda self: closed.append(True))

    with pytest.raises(OSError):
        get_name('kitchen')
    assert closed == [True]


def test_discovery_close_closes_socket():
    try:
        discovery = Discovery()
    except OSError:
        pytest.skip("cannot bind UDP discovery port in this environment")
    discovery.close()
    assert discovery.sock.fileno() == -1
