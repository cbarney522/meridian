"""Network troubleshooting from the reader itself (Help › Troubleshooting › Network tools).

Standard library only. Everything reads Linux files directly or opens ordinary sockets, so it
works without root and without the reader's REST login.

    status()                      interfaces, default gateway, DNS servers, DHCP verdict
    dhcp_status(iface, ipv4)      was the address handed out by a DHCP server? (with evidence)
    port_test(host, port, tls)    can the reader open a TCP connection (and TLS) to host:port?
    dns_lookup(name)              resolve a name with the reader's DNS servers
    ping(host)                    ICMP echo using the system ping, when the reader has one
"""
import fcntl
import glob
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import time
from datetime import datetime, timezone

from core import netinfo

SIOCGIFNETMASK = 0x891B
HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9_]([A-Za-z0-9_-]{0,62})(\.[A-Za-z0-9_]([A-Za-z0-9_-]{0,62}))*\.?$")
DHCP_CLIENTS = ("udhcpc", "dhclient", "dhcpcd", "systemd-networkd", "connmand", "NetworkManager", "dhcp6c")
COMMON_PORTS = [
    {"port": 1883, "tls": False, "label": "MQTT"},
    {"port": 8883, "tls": True, "label": "MQTT over TLS"},
    {"port": 443, "tls": True, "label": "HTTPS"},
    {"port": 80, "tls": False, "label": "HTTP"},
]


# ---------------------------------------------------------------- input checks
def clean_host(host):
    host = str(host or "").strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if not HOST_RE.match(host):
        raise ValueError("Enter a host name (e.g. broker.example.com) or an IP address.")
    return host.rstrip(".")


def clean_port(port):
    try:
        p = int(port)
    except (TypeError, ValueError):
        raise ValueError("Port must be a number from 1 to 65535.")
    if not 1 <= p <= 65535:
        raise ValueError("Port must be a number from 1 to 65535.")
    return p


# ---------------------------------------------------------------- interfaces, routes, DNS
def _read(path, default=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def _netmask(iface):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            packed = fcntl.ioctl(s.fileno(), SIOCGIFNETMASK, struct.pack("256s", iface[:15].encode()))
            return socket.inet_ntoa(packed[20:24])
    except OSError:
        return None


def _ipv6_all(iface, proc="/proc/net/if_inet6"):
    out = []
    try:
        with open(proc, encoding="utf-8") as f:
            for line in f:
                p = line.split()
                if len(p) >= 6 and p[5] == iface:
                    scope = {"00": "global", "20": "link", "10": "host"}.get(p[3], p[3])
                    out.append({"address": socket.inet_ntop(socket.AF_INET6, bytes.fromhex(p[0])),
                                "prefix": int(p[2], 16), "scope": scope})
    except (OSError, ValueError):
        pass
    return out


def interfaces(sys_net="/sys/class/net"):
    out = []
    try:
        names = sorted(os.listdir(sys_net))
    except OSError:
        names = []
    for name in names:
        if name == "lo":
            continue
        base = os.path.join(sys_net, name)
        ipv4 = netinfo.ipv4_address(name)
        mask = _netmask(name) if ipv4 else None
        speed = _read(os.path.join(base, "speed"))
        stats = {k: _read(os.path.join(base, "statistics", k)) for k in ("rx_bytes", "tx_bytes", "rx_errors", "tx_errors", "rx_dropped")}
        out.append({
            "name": name,
            "mac": netinfo.mac_address(name, sys_net),
            "state": _read(os.path.join(base, "operstate"), "unknown"),
            "carrier": _read(os.path.join(base, "carrier")) == "1",
            "speed_mbps": int(speed) if speed and speed.lstrip("-").isdigit() and int(speed) > 0 else None,
            "duplex": _read(os.path.join(base, "duplex")),
            "mtu": int(_read(os.path.join(base, "mtu"), "0") or 0) or None,
            "ipv4": ipv4,
            "netmask": mask,
            "prefix": ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen if mask else None,
            "ipv6": _ipv6_all(name),
            "stats": {k: int(v) for k, v in stats.items() if v and v.isdigit()},
        })
    return out


def routes(proc_route="/proc/net/route"):
    """Default gateways from the kernel routing table: [{"interface", "gateway", "metric"}]."""
    out = []
    try:
        with open(proc_route, encoding="utf-8") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if len(p) >= 7 and p[1] == "00000000":
                    gw = socket.inet_ntoa(struct.pack("<L", int(p[2], 16)))
                    out.append({"interface": p[0], "gateway": gw, "metric": int(p[6])})
    except (OSError, ValueError):
        pass
    return sorted(out, key=lambda r: r["metric"])


def dns_servers(resolv="/etc/resolv.conf"):
    """Name servers and search domains. Follows a systemd-resolved stub to the real servers."""
    def parse(path):
        ns, search = [], []
        for line in (_read(path, "") or "").splitlines():
            p = line.split()
            if len(p) >= 2 and p[0] == "nameserver":
                ns.append(p[1])
            elif len(p) >= 2 and p[0] in ("search", "domain"):
                search += p[1:]
        return ns, search
    ns, search = parse(resolv)
    via = resolv
    if ns == ["127.0.0.53"] and os.path.exists("/run/systemd/resolve/resolv.conf"):
        ns, search = parse("/run/systemd/resolve/resolv.conf")
        via = "/run/systemd/resolve/resolv.conf (systemd-resolved)"
    return {"servers": ns, "search": search, "source": via}


# ---------------------------------------------------------------- DHCP
def _running_clients(proc="/proc"):
    found = []
    for d in glob.glob(os.path.join(proc, "[0-9]*")):
        try:
            with open(os.path.join(d, "cmdline"), "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        if not cmd:
            continue
        prog = os.path.basename(cmd.split()[0])
        for name in DHCP_CLIENTS:
            if prog == name or prog.startswith(name):
                found.append({"client": name, "cmdline": cmd[:200]})
                break
    return found


def _parse_keyvals(text):
    kv = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    return kv


def _parse_dhclient(text, iface):
    """Last lease block for iface in a dhclient-style lease file."""
    best = None
    for block in re.findall(r"lease\s*\{(.*?)\}", text, re.S):
        if iface and f'interface "{iface}"' not in block and "interface" in block:
            continue
        lease = {}
        m = re.search(r"fixed-address\s+([\d.]+);", block)
        if m:
            lease["address"] = m.group(1)
        m = re.search(r"option dhcp-server-identifier\s+([\d.]+);", block)
        if m:
            lease["server"] = m.group(1)
        m = re.search(r"option routers\s+([\d., ]+);", block)
        if m:
            lease["router"] = m.group(1).split(",")[0].strip()
        for key in ("renew", "rebind", "expire"):
            m = re.search(rf"{key}\s+\d+\s+(\d{{4}}/\d\d/\d\d \d\d:\d\d:\d\d);", block)
            if m:
                lease[key] = datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        if lease.get("address"):
            best = lease
    return best


def _lease_files(iface, roots=None):
    """Look for lease records in the places common Linux DHCP clients keep them."""
    roots = roots or {}
    r = lambda p: roots.get("prefix", "") + p      # noqa: E731  (tests point this at a temp dir)
    leases, seen = [], []
    # systemd-networkd: /run/systemd/netif/leases/<ifindex>, KEY=VALUE
    idx = _read(r(f"/sys/class/net/{iface}/ifindex")) if iface else None
    for path in sorted(glob.glob(r("/run/systemd/netif/leases/*"))):
        seen.append(path)
        if idx and os.path.basename(path) != idx:
            continue
        kv = _parse_keyvals(_read(path, "") or "")
        if kv.get("ADDRESS"):
            try:
                got = os.path.getmtime(path)
            except OSError:
                got = None
            life = int(kv["LIFETIME"]) if kv.get("LIFETIME", "").isdigit() else None
            leases.append({"source": path, "client": "systemd-networkd", "address": kv["ADDRESS"],
                           "server": kv.get("SERVER_ADDRESS"), "router": (kv.get("ROUTER") or "").split(" ")[0] or None,
                           "obtained": got, "expire": got + life if got and life else None, "lifetime_s": life})
    # dhclient / NetworkManager (dhclient format)
    for pat in ("/var/lib/dhcp/*.leases", "/var/lib/dhclient/*.lease*", "/var/lib/dhcp/dhclient*",
                "/var/lib/NetworkManager/*.lease"):
        for path in sorted(glob.glob(r(pat))):
            seen.append(path)
            text = _read(path, "") or ""
            lease = _parse_dhclient(text, iface)
            if not lease:
                kv = _parse_keyvals(text)          # NetworkManager internal client: KEY=VALUE
                if kv.get("ADDRESS"):
                    lease = {"address": kv["ADDRESS"], "server": kv.get("SERVER_ADDRESS"),
                             "router": (kv.get("ROUTER") or "").split(" ")[0] or None}
            if lease:
                leases.append(dict(lease, source=path, client="dhclient" if "lease {" in text else "NetworkManager"))
    # dhcpcd keeps binary leases: presence only
    for path in sorted(glob.glob(r(f"/var/lib/dhcpcd*/{iface or '*'}*.lease"))):
        seen.append(path)
        try:
            got = os.path.getmtime(path)
        except OSError:
            got = None
        leases.append({"source": path, "client": "dhcpcd", "address": None, "obtained": got})
    # udhcpc writes nothing by default; some images keep a copy here
    for pat in ("/tmp/udhcpc*", "/var/run/udhcpc*", "/run/udhcpc*", "/var/lib/udhcpc/*"):
        for path in sorted(glob.glob(r(pat))):
            seen.append(path)
            kv = _parse_keyvals(_read(path, "") or "")
            addr = kv.get("ip") or kv.get("ADDRESS")
            if addr:
                leases.append({"source": path, "client": "udhcpc", "address": addr,
                               "server": kv.get("serverid") or kv.get("SERVER_ADDRESS"), "router": kv.get("router")})
    return leases, seen


def _nm_options_to_lease(opts, source):
    """NetworkManager DHCP4 options (dict of str) -> our lease dict, or None."""
    addr = opts.get("ip_address")
    if not addr:
        return None
    lease = {"source": source, "client": "NetworkManager", "address": addr,
             "server": opts.get("dhcp_server_identifier"), "router": (opts.get("routers") or "").split(" ")[0] or None}
    life = opts.get("dhcp_lease_time")
    exp = opts.get("expiry")
    if exp and exp.isdigit():
        lease["expire"] = int(exp)
        if life and life.isdigit():
            lease["lifetime_s"] = int(life)
            lease["obtained"] = int(exp) - int(life)
    return lease


def parse_nmcli_dhcp4(text):
    """`nmcli -t -f DHCP4 device show eth0` lines like DHCP4.OPTION[3]:expiry = 1763500000."""
    opts = {}
    for line in (text or "").splitlines():
        if line.startswith("DHCP4.OPTION") and ":" in line:
            kv = line.split(":", 1)[1]
            if " = " in kv:
                k, v = kv.split(" = ", 1)
                opts[k.strip()] = v.strip()
    return opts


def _nm_lease(iface, evidence, run=None):
    """Ask NetworkManager for its DHCP lease: nmcli first, then D-Bus through busctl."""
    run = run or (lambda args: subprocess.run(args, capture_output=True, text=True, timeout=5))
    if not iface:
        return None
    if shutil.which("nmcli"):
        try:
            p = run(["nmcli", "-t", "-f", "DHCP4", "device", "show", iface])
            opts = parse_nmcli_dhcp4(p.stdout)
            if opts:
                evidence.append(f"nmcli: {len(opts)} DHCP options for {iface}")
                return _nm_options_to_lease(opts, "NetworkManager (nmcli)")
            evidence.append(f"nmcli: no DHCP options for {iface}" + (f" ({p.stderr.strip()[:120]})" if p.stderr.strip() else ""))
        except (OSError, subprocess.SubprocessError) as e:
            evidence.append(f"nmcli failed: {e}")
    else:
        evidence.append("nmcli: not installed")
    if shutil.which("busctl"):
        try:
            nm = ["busctl", "--system", "-j"]
            p = run(nm + ["call", "org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager",
                          "org.freedesktop.NetworkManager", "GetDeviceByIpIface", "s", iface])
            dev = json.loads(p.stdout)["data"][0] if p.returncode == 0 and p.stdout.strip() else None
            if not dev:
                evidence.append(f"busctl: NetworkManager didn't answer for {iface}" + (f" ({p.stderr.strip()[:120]})" if p.stderr.strip() else ""))
                return None
            p = run(nm + ["get-property", "org.freedesktop.NetworkManager", dev,
                          "org.freedesktop.NetworkManager.Device", "Dhcp4Config"])
            cfg = json.loads(p.stdout)["data"] if p.returncode == 0 and p.stdout.strip() else None
            if not cfg or cfg == "/":
                evidence.append("busctl: NetworkManager has no DHCP4 lease for this interface")
                return None
            p = run(nm + ["get-property", "org.freedesktop.NetworkManager", cfg,
                          "org.freedesktop.NetworkManager.DHCP4Config", "Options"])
            raw = json.loads(p.stdout)["data"] if p.returncode == 0 and p.stdout.strip() else {}
            opts = {k: str(v.get("data") if isinstance(v, dict) else v) for k, v in raw.items()}
            evidence.append(f"busctl: {len(opts)} DHCP options from NetworkManager")
            return _nm_options_to_lease(opts, "NetworkManager (D-Bus)")
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError) as e:
            evidence.append(f"busctl failed: {e}")
    else:
        evidence.append("busctl: not installed")
    return None


def _configured_method(iface, roots=None):
    """What the reader's own config files say: 'dhcp', 'static' or None."""
    roots = roots or {}
    r = lambda p: roots.get("prefix", "") + p      # noqa: E731
    found = []
    for line in (_read(r("/etc/network/interfaces"), "") or "").splitlines():
        p = line.split()
        if len(p) >= 4 and p[0] == "iface" and p[1] == iface and p[2] == "inet":
            found.append(("dhcp" if p[3] == "dhcp" else "static" if p[3] == "static" else None, "/etc/network/interfaces"))
    for path in sorted(glob.glob(r("/etc/systemd/network/*.network")) + glob.glob(r("/run/systemd/network/*.network"))):
        text = _read(path, "") or ""
        m = re.search(r"^\s*Name\s*=\s*(\S+)", text, re.M)
        if m and iface and not re.fullmatch(m.group(1).replace("*", ".*"), iface):
            continue
        if re.search(r"^\s*DHCP\s*=\s*(yes|true|ipv4|both)", text, re.M | re.I):
            found.append(("dhcp", path))
        elif re.search(r"^\s*Address\s*=", text, re.M):
            found.append(("static", path))
    for path in sorted(glob.glob(r("/var/lib/connman/ethernet_*/settings"))):
        m = re.search(r"^IPv4\.method\s*=\s*(\w+)", _read(path, "") or "", re.M)
        if m:
            found.append(("dhcp" if m.group(1) == "dhcp" else "static" if m.group(1) == "manual" else None, path))
    return [f for f in found if f[0]]


def dhcp_status(iface, ipv4, roots=None, now=None, clients=None, nm_run=None):
    """Decide, from what this reader shows, whether its IPv4 address came from DHCP.

    Returns {"verdict", "message", "lease", "evidence": [...]}; verdicts:
      dhcp        a lease record matches the current address
      dhcp_likely a DHCP client is running for this interface or the config says DHCP, but no lease record is readable
      static      the config says static and no DHCP client is running
      apipa       only a 169.254.x.x address: the reader asked for DHCP and nobody answered
      none        no IPv4 address at all
      unknown     not enough to tell
    """
    now = now or time.time()
    evidence = []
    clients = _running_clients() if clients is None else clients
    mine = [c for c in clients if not iface or iface in c["cmdline"] or c["client"] in ("systemd-networkd", "connmand", "NetworkManager")]
    for c in mine:
        evidence.append(f"DHCP client running: {c['cmdline']}")
    if not mine:
        evidence.append("No DHCP client process found.")
    leases, seen = _lease_files(iface, roots)
    evidence.append(f"Lease locations checked: {len(seen)} file(s) found" + (f" ({', '.join(seen[:4])}{'…' if len(seen) > 4 else ''})" if seen else ""))
    if any(c["client"] == "NetworkManager" for c in mine) and not any(l.get("address") == ipv4 for l in leases):
        nm = _nm_lease(iface, evidence, run=nm_run)
        if nm:
            leases.append(nm)
    config = _configured_method(iface, roots)
    for method, path in config:
        evidence.append(f"Config: {method} in {path}")

    lease = next((l for l in leases if ipv4 and l.get("address") == ipv4), None)
    if lease and lease.get("expire"):
        lease["expires_in_s"] = int(lease["expire"] - now)

    if not ipv4:
        return {"verdict": "none", "lease": None, "evidence": evidence,
                "message": "No IPv4 address. The reader has neither a DHCP lease nor a static address. Check the cable, the switch port, and that a DHCP server is on this network."}
    if ipaddress.ip_address(ipv4) in ipaddress.ip_network("169.254.0.0/16"):
        return {"verdict": "apipa", "lease": None, "evidence": evidence,
                "message": f"Only a self-assigned address ({ipv4}). The reader asked for DHCP but no DHCP server answered. Check the network has a DHCP server, and the VLAN on the switch port."}
    if lease:
        exp = lease.get("expires_in_s")
        when = "" if exp is None else (" Lease has expired." if exp < 0 else f" Lease expires in {_fmt_dur(exp)}.")
        srv = f" from DHCP server {lease['server']}" if lease.get("server") else ""
        return {"verdict": "dhcp", "lease": lease, "evidence": evidence,
                "message": f"DHCP lease for {ipv4}{srv}.{when}"}
    if mine or any(m == "dhcp" for m, _ in config):
        return {"verdict": "dhcp_likely", "lease": None, "evidence": evidence,
                "message": f"{ipv4} is very likely from DHCP ({(mine[0]['client'] if mine else 'configured for DHCP')}), "
                           "but this reader doesn't keep a readable lease record, so the server and expiry can't be shown."}
    if any(m == "static" for m, _ in config):
        return {"verdict": "static", "lease": None, "evidence": evidence,
                "message": f"{ipv4} is a static address set on the reader, not from DHCP."}
    return {"verdict": "unknown", "lease": None, "evidence": evidence,
            "message": f"The reader has {ipv4}, but nothing on it shows whether that came from DHCP or was set by hand. "
                       "Check the reader's network settings in the Impinj web UI."}


def _fmt_dur(s):
    s = abs(int(s))
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"


def status():
    ifs = interfaces()
    gw = routes()
    main = gw[0]["interface"] if gw else netinfo.default_interface()
    cur = next((i for i in ifs if i["name"] == main), None)
    return {"hostname": socket.gethostname(), "interface": main, "interfaces": ifs, "gateways": gw,
            "dns": dns_servers(), "dhcp": dhcp_status(main, cur["ipv4"] if cur else None)}


# ---------------------------------------------------------------- active tests
def _resolve(host, timeout):
    t0 = time.monotonic()
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    finally:
        socket.setdefaulttimeout(old)
    addrs = []
    for fam, *_rest, sa in infos:
        if sa[0] not in [a["address"] for a in addrs]:
            addrs.append({"address": sa[0], "family": "IPv6" if fam == socket.AF_INET6 else "IPv4"})
    return addrs, int((time.monotonic() - t0) * 1000)


def _cert_info(cert):
    def name(seq):
        return ", ".join(f"{k}={v}" for rdn in seq or () for k, v in rdn)
    out = {"subject": name(cert.get("subject")), "issuer": name(cert.get("issuer")),
           "not_after": cert.get("notAfter"), "san": [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"][:10]}
    try:
        out["expires_in_days"] = int((ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) // 86400)
    except (KeyError, ValueError):
        pass
    return out


def port_test(host, port, tls=False, timeout=5):
    """Open a TCP connection (and optionally a TLS handshake) to host:port. Never sends data
    beyond the TLS handshake. Returns a dict with ok, step, message and timings."""
    host, port = clean_host(host), clean_port(port)
    timeout = max(1, min(float(timeout or 5), 10))
    out = {"host": host, "port": port, "tls": bool(tls), "ok": False, "step": "dns"}
    try:
        addrs, out["dns_ms"] = _resolve(host, timeout)
    except socket.gaierror as e:
        out["message"] = f"Can't resolve {host}: the DNS server doesn't know this name ({e.strerror}). Check the spelling and the reader's DNS servers."
        return out
    except OSError as e:
        out["message"] = f"DNS lookup failed: {e}"
        return out
    out["addresses"] = addrs
    out["step"] = "connect"
    last = None
    for a in addrs:
        fam = socket.AF_INET6 if a["family"] == "IPv6" else socket.AF_INET
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.settimeout(timeout)
        t0 = time.monotonic()
        try:
            s.connect((a["address"], port))
        except ConnectionRefusedError:
            last = ("refused", a["address"], int((time.monotonic() - t0) * 1000))
            s.close()
            continue
        except (socket.timeout, TimeoutError):
            last = ("timeout", a["address"], None)
            s.close()
            continue
        except OSError as e:
            last = ("error", a["address"], str(e.strerror or e))
            s.close()
            continue
        out["address"] = a["address"]
        out["connect_ms"] = int((time.monotonic() - t0) * 1000)
        try:
            if not tls:
                out.update(ok=True, step="done",
                           message=f"Port {port} is open on {host} ({a['address']}): connected in {out['connect_ms']} ms.")
                return out
            out["step"] = "tls"
            return _tls(s, host, out, timeout)
        finally:
            s.close()
    kind, addr, extra = last
    out["address"] = addr
    if kind == "refused":
        out["message"] = (f"Connection refused by {host} ({addr}) in {extra} ms. The network path works, but nothing is "
                          f"listening on port {port}: the service is stopped, or uses a different port.")
    elif kind == "timeout":
        out["message"] = (f"No answer from {host} ({addr}) on port {port} within {timeout:g} s. A firewall is most likely "
                          f"dropping the traffic (on the reader's network or at the server), or the host is down.")
    else:
        out["message"] = f"Can't connect to {host} ({addr}) on port {port}: {extra}. Check the reader's gateway and network."
    return out


def _tls(sock, host, out, timeout):
    ctx = ssl.create_default_context()
    t0 = time.monotonic()
    try:
        with ctx.wrap_socket(sock, server_hostname=host) as ts:
            out["tls_ms"] = int((time.monotonic() - t0) * 1000)
            out["tls_version"] = ts.version()
            out["certificate"] = _cert_info(ts.getpeercert())
            out["trusted"] = True
            out.update(ok=True, step="done",
                       message=f"Port {out['port']} is open on {host} and TLS works ({ts.version()}, certificate trusted).")
            return out
    except ssl.SSLCertVerificationError as e:
        out["trusted"] = False
        out["cert_problem"] = e.verify_message
        info = _peek_cert(out["address"], out["port"], host, timeout)
        if info:
            out["certificate"] = info
        out.update(ok=True, step="done",
                   message=f"Port {out['port']} is open and TLS works, but the certificate isn't trusted by the reader "
                           f"({e.verify_message}). Clients that check certificates will refuse it unless given its CA.")
        return out
    except ssl.SSLError as e:
        out["message"] = (f"Port {out['port']} is open, but the TLS handshake failed ({e.reason or e}). "
                          f"The service may not use TLS on this port: try without TLS.")
    except (socket.timeout, TimeoutError):
        out["message"] = f"Port {out['port']} is open, but the TLS handshake got no answer. The service may not use TLS on this port."
    except OSError as e:
        out["message"] = f"Port {out['port']} is open, but the connection closed during TLS ({e})."
    return out


def _peek_cert(addr, port, host, timeout):
    """Fetch the certificate without verifying it, only to describe it."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((addr, port), timeout=timeout) as s, ctx.wrap_socket(s, server_hostname=host) as ts:
            der = ts.getpeercert(binary_form=True)
        pem = ssl.DER_cert_to_PEM_cert(der)
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
            f.write(pem)
            path = f.name
        try:
            return _cert_info(ssl._ssl._test_decode_cert(path))     # noqa: SLF001 (stdlib's own decoder)
        finally:
            os.unlink(path)
    except Exception:
        return None


def dns_lookup(name, timeout=5):
    name = clean_host(name)
    try:
        ipaddress.ip_address(name)
        try:
            rev = socket.gethostbyaddr(name)[0]
        except OSError:
            rev = None
        return {"name": name, "ok": True, "addresses": [{"address": name, "family": "IPv6" if ":" in name else "IPv4"}],
                "reverse": rev, "ms": 0, "servers": dns_servers()["servers"],
                "message": f"{name} is already an IP address" + (f"; it reverse-resolves to {rev}." if rev else ".")}
    except ValueError:
        pass
    try:
        addrs, ms = _resolve(name, timeout)
    except OSError as e:
        return {"name": name, "ok": False, "servers": dns_servers()["servers"],
                "message": f"Can't resolve {name} ({getattr(e, 'strerror', None) or e}). Check the spelling and the DNS servers below."}
    return {"name": name, "ok": True, "addresses": addrs, "ms": ms, "servers": dns_servers()["servers"],
            "message": f"{name} resolves to {', '.join(a['address'] for a in addrs)} ({ms} ms)."}


PING_RE = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
RTT_RE = re.compile(r"= ([\d.]+)/([\d.]+)/([\d.]+)")


def ping(host, count=4):
    host = clean_host(host)
    exe = shutil.which("ping")
    if not exe:
        return {"host": host, "ok": False, "available": False,
                "message": "This reader has no ping command. Use the port test instead: it shows whether a host is reachable."}
    args = [exe, "-c", str(count), "-W", "2", host]
    if ":" in host:
        args.insert(1, "-6")
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=count * 3 + 5)
    except subprocess.TimeoutExpired:
        return {"host": host, "ok": False, "available": True, "message": "Ping took too long and was stopped."}
    except OSError as e:
        return {"host": host, "ok": False, "available": False, "message": f"Ping couldn't run: {e}"}
    text = (p.stdout + p.stderr).strip()
    m, r = PING_RE.search(text), RTT_RE.search(text)
    sent, got = (int(m.group(1)), int(m.group(2))) if m else (count, 0)
    res = {"host": host, "available": True, "sent": sent, "received": got, "output": text[-1500:], "ok": got > 0}
    if r:
        res.update(min_ms=float(r.group(1)), avg_ms=float(r.group(2)), max_ms=float(r.group(3)))
    if "permission" in text.lower() or "operation not permitted" in text.lower():
        res.update(ok=False, available=False,
                   message="The reader doesn't allow this app to send ping packets. Use the port test instead.")
    elif got:
        res["message"] = f"{got} of {sent} replies" + (f", average {res['avg_ms']:.1f} ms." if r else ".")
    else:
        res["message"] = (f"No replies from {host}. It may be down, or it (or a firewall) may simply ignore ping: "
                          f"many servers do. A port test is a better check.")
    return res


# ---------------------------------------------------------------- quick check
def targets_from(mqtt_settings, webhook_settings):
    """Hosts this reader is configured to talk to: [{"label", "host", "port", "tls"}]."""
    from urllib.parse import urlsplit
    out = []
    m = mqtt_settings or {}
    if m.get("host"):
        out.append({"label": "MQTT broker", "host": m["host"], "port": int(m.get("port") or 1883), "tls": bool(m.get("tls"))})
    for ep in (webhook_settings or {}).get("endpoints", []):
        if not ep.get("enabled"):
            continue
        try:
            u = urlsplit(ep["url"])
            https = u.scheme.lower() == "https"
            out.append({"label": f"Webhook: {ep.get('name') or u.hostname}", "host": u.hostname,
                        "port": u.port or (443 if https else 80), "tls": https})
        except (ValueError, KeyError, TypeError):
            continue
    return [t for t in out if t["host"]]


def quick_checks(targets, ntp=None, st=None):
    """Run the usual checks in order and return [{"name", "status": pass|warn|fail|info, "detail"}]."""
    st = st or status()
    res = []
    add = lambda name, s, detail: res.append({"name": name, "status": s, "detail": detail})   # noqa: E731
    cur = next((i for i in st["interfaces"] if i["name"] == st["interface"]), None)
    if not cur:
        add("Network interface", "fail", "No network interface is up.")
        return res
    if not cur["carrier"]:
        add("Cable / link", "fail", f"{cur['name']} has no link: check the cable, the switch port, and PoE.")
        return res
    add("Cable / link", "pass", f"{cur['name']} is connected" + (f" at {cur['speed_mbps']} Mb/s" if cur["speed_mbps"] else "")
        + (f", {cur['duplex']} duplex" if cur.get("duplex") not in (None, "", "unknown") else "") + ".")
    d = st["dhcp"]
    if d["verdict"] in ("none", "apipa"):
        add("IP address", "fail", d["message"])
        return res
    add("IP address", "pass", f"{cur['ipv4']}/{cur['prefix']}")
    add("DHCP", {"dhcp": "pass", "dhcp_likely": "pass", "static": "info", "unknown": "info"}[d["verdict"]], d["message"])
    if not st["gateways"]:
        add("Default gateway", "fail", "No default gateway: the reader can only reach devices on its own subnet.")
    else:
        gw = st["gateways"][0]["gateway"]
        p = ping(gw, count=2)
        if p.get("ok"):
            add("Default gateway", "pass", f"{gw} answers ping ({p.get('avg_ms', 0):.1f} ms).")
        elif not p.get("available"):
            add("Default gateway", "info", f"{gw} (ping isn't available on this reader to test it).")
        else:
            add("Default gateway", "warn", f"{gw} doesn't answer ping. Some routers ignore ping; if the tests below pass, the gateway is fine.")
    servers = st["dns"]["servers"]
    if not servers:
        add("DNS servers", "fail", "No DNS servers configured: host names won't resolve.")
    else:
        add("DNS servers", "pass", ", ".join(servers))
    names = [t["host"] for t in targets if not _is_ip(t["host"])]
    for name in dict.fromkeys(names):
        r = dns_lookup(name)
        add(f"DNS lookup: {name}", "pass" if r["ok"] else "fail", r["message"])
    for t in targets:
        r = port_test(t["host"], t["port"], t["tls"], timeout=5)
        s = "pass" if r["ok"] and r.get("trusted", True) else "warn" if r["ok"] else "fail"
        add(f"{t['label']}: {t['host']}:{t['port']}" + (" (TLS)" if t["tls"] else ""), s, r["message"])
    if not targets:
        add("Configured servers", "info", "No MQTT broker or webhook is set up yet, so there's nothing else to test. Use the port test below.")
    if ntp is not None:
        tr = (ntp or {}).get("tracking")
        if not (ntp or {}).get("available"):
            add("Time sync", "info", "Can't read the time-sync status on this reader.")
        elif tr and tr.get("synced"):
            add("Time sync", "pass", f"Synced to {tr['reference']} (offset {tr['offset_ms']} ms). TLS needs the right time.")
        else:
            add("Time sync", "warn", "The clock isn't synced to a time server. TLS connections can fail when the clock is wrong. Check UDP port 123.")
    return res


def _is_ip(h):
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False
