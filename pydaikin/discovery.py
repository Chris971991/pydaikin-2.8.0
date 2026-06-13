"""Discovery module to autodiscover Daikin devices on local network."""

import logging
import socket
from typing import Optional

import netifaces

from .response import parse_response

_LOGGER = logging.getLogger(__name__)

UDP_SRC_PORT = 30000
UDP_DST_PORT = 30050
RCV_BUFSIZ = 1024

GRACE_SECONDS = 1

DISCOVERY_MSG = "DAIKIN_UDP/common/basic_info"


class Discovery:  # pylint: disable=too-few-public-methods
    """Discovery class."""

    def __init__(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", UDP_SRC_PORT))
            sock.settimeout(GRACE_SECONDS)
        except OSError:
            sock.close()
            raise

        self.sock = sock
        self.dev = {}

    def close(self) -> None:
        """Close the underlying UDP socket."""
        self.sock.close()

    @staticmethod
    def _handle_datagram(data: bytes, addr) -> Optional[dict]:
        """Parse one discovery datagram into a device entry.

        Returns None for invalid/undecodable payloads or payloads without a
        'mac'. The device-advertised 'port' from basic_info (if any) is left
        untouched; the UDP reply source port is stored separately as
        'udp_port' because it is never the device's HTTP port.
        """
        try:
            _LOGGER.debug("Discovered %s, %s", addr, data.decode('UTF-8'))
            entry = parse_response(data.decode('UTF-8'))
        except ValueError:  # invalid message received (incl. UnicodeDecodeError)
            return None

        if 'mac' not in entry:
            return None

        entry['ip'] = addr[0]
        entry['udp_port'] = str(addr[1])
        return entry

    def poll(self, stop_if_found=None, ip=None):  # pylint: disable=invalid-name
        """Poll for available devices."""
        if ip:
            broadcast_ips = [ip]
        else:
            # get all IPv4 definitions in the system
            net_groups = [
                netifaces.ifaddresses(i)[netifaces.AF_INET]
                for i in netifaces.interfaces()
                if netifaces.AF_INET in netifaces.ifaddresses(i)
            ]

            # flatten the previous list
            net_ips = [item for sublist in net_groups for item in sublist]

            # from those, get the broadcast IPs, if available
            broadcast_ips = [i['broadcast'] for i in net_ips if 'broadcast' in i.keys()]

        # send a daikin broadcast to each one of the ips
        for ip_address in broadcast_ips:
            self.sock.sendto(bytes(DISCOVERY_MSG, 'UTF-8'), (ip_address, UDP_DST_PORT))

        try:
            while True:  # for anyone who answers
                data, addr = self.sock.recvfrom(RCV_BUFSIZ)

                entry = self._handle_datagram(data, addr)
                if entry is None:
                    continue

                self.dev[entry['mac']] = entry

                # 'name' is not guaranteed by parse_response — tolerate it
                # missing instead of crashing the whole poll.
                if (
                    stop_if_found is not None
                    and entry.get('name', '').lower() == stop_if_found.lower()
                ):
                    return self.dev.values()

        except socket.timeout:  # nobody else is answering
            pass

        return self.dev.values()


def get_devices():
    """Returns discovered devices."""
    discovery = Discovery()
    try:
        return discovery.poll()
    finally:
        discovery.close()


def get_name(name):
    """Returns the entry of the discovered device matching name, or None."""
    discovery = Discovery()
    try:
        devices = discovery.poll(name)
    finally:
        discovery.close()

    ret = None

    for device in devices:
        if device.get('name', '').lower() == name.lower():
            ret = device

    return ret
