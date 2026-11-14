"""Network facts read straight from Linux (no reader login needed). Standard library only.

    netinfo.summary() -> {"interface", "mac", "ipv4", "ipv6", "hostname"}
"""
import fcntl
import os
import socket
import struct

SIOCGIFADDR = 0x8915


def default_interface(proc_route="/proc/net/route"):
    """Interface carrying the default route (e.g. 'eth0'), or the first non-loopback one."""
    try:
        with open(proc_route, encoding="utf-8") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 2 and parts[1] == "00000000":
                    return parts[0]
    except OSError:
        pass
    try:
        names = sorted(n for n in os.listdir("/sys/class/net") if n != "lo")
        return names[0] if names else None
    except OSError:
        return None


def mac_address(iface, sys_net="/sys/class/net"):
    try:
        with open(os.path.join(sys_net, iface, "address"), encoding="utf-8") as f:
            mac = f.read().strip().upper()
        return mac if mac and mac != "00:00:00:00:00:00" else None
    except (OSError, TypeError):
        return None


def ipv4_address(iface):
    if not iface:
        return None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            packed = fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", iface[:15].encode()))
            return socket.inet_ntoa(packed[20:24])
    except OSError:
        return None


def ipv6_addresses(iface, proc_if_inet6="/proc/net/if_inet6"):
    """Global (scope 00) IPv6 addresses of iface, formatted, e.g. ['2601:c2:f00:...']."""
    out = []
    try:
        with open(proc_if_inet6, encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 6 and parts[5] == iface and parts[3] == "00":
                    raw = bytes.fromhex(parts[0])
                    out.append(socket.inet_ntop(socket.AF_INET6, raw))
    except (OSError, ValueError):
        pass
    return out


def summary():
    iface = default_interface()
    v6 = ipv6_addresses(iface) if iface else []
    return {"interface": iface, "mac": mac_address(iface) if iface else None,
            "ipv4": ipv4_address(iface), "ipv6": v6[0] if v6 else None, "ipv6_all": v6,
            "hostname": socket.gethostname()}
