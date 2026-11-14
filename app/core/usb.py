"""USB device discovery from sysfs, and checking devices against port assignments.

Standard library only; this is the code that runs on the reader. It reads the same files
`lsusb -t` does, so it needs no extra packages and no root beyond normal sysfs read access.

Devices are identified by **physical port path** (e.g. "1-1.3" = bus 1, root port 1, hub port 3),
which stays the same across reboots and replugs as long as the cabling doesn't change.
That's what assignments are keyed on: identical badge readers have identical VID:PID and
often no serial number, so the port is the only reliable way to tell them apart.

    from core.usb import scan, evaluate
    devices = scan()                       # or scan("/path/to/fake/sys") in tests / mock
    rows = evaluate(devices, assignments)  # adds status, fills in missing assigned devices
"""
import os
import re

DEVICE_NAME = re.compile(r"^(\d+)-(\d+(?:\.\d+)*)$")     # "1-1", "1-1.3"  (not "1-1:1.0" interfaces)
ROOT_HUB = re.compile(r"^usb(\d+)$")

# Roles a technician can give a port. "expects" lists acceptable device types; "single" means
# at most one port may hold the role.
ROLES = {
    "badge_in":    {"label": "Badge reader (in)",   "expects": ["hid-keyboard"], "single": True},
    "badge_out":   {"label": "Badge reader (out)",  "expects": ["hid-keyboard"], "single": True},
    "scanner":     {"label": "Barcode scanner",     "expects": ["hid-keyboard"], "single": False},
    "keypad":      {"label": "Keypad / buttons",    "expects": ["hid-keyboard", "hid"], "single": False},
    "io_board":    {"label": "I/O board",           "expects": ["serial-cdc"], "single": False},
    "relay_board": {"label": "HID relay board",     "expects": ["hid"], "single": False},
    "serial":      {"label": "Serial adapter (RS-485/232)", "expects": ["serial-usb", "serial-cdc"], "single": False},
    "gps":         {"label": "GPS receiver",        "expects": ["serial-cdc", "serial-usb"], "single": True},
    "storage":     {"label": "Data drive",          "expects": ["storage"], "single": True},
    "ignore":      {"label": "Ignore",              "expects": [], "single": False},
}

TYPE_LABELS = {
    "hub": "Hub", "hid-keyboard": "Keyboard (HID)", "hid": "HID device", "storage": "Storage",
    "serial-cdc": "Serial (CDC-ACM)", "serial-usb": "Serial adapter", "audio": "Audio",
    "video": "Camera", "wireless": "Wireless", "vendor": "Vendor-specific", "other": "Other",
}

SERIAL_DRIVERS = {"ftdi_sio", "cp210x", "ch341", "pl2303", "cdc_acm", "usbserial_generic"}


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def _driver(path):
    link = os.path.join(path, "driver")
    return os.path.basename(os.readlink(link)) if os.path.islink(link) else None


def _port_key(name):
    """Sort key so 1-1.10 sorts after 1-1.9 and children follow their hub."""
    m = DEVICE_NAME.match(name)
    if not m:
        return (999,)
    return (int(m.group(1)),) + tuple(int(x) for x in m.group(2).split("."))


def _classify(dev_class, interfaces):
    classes = {i["class"] for i in interfaces}
    drivers = {i["driver"] for i in interfaces if i["driver"]}
    if dev_class == "09" or "09" in classes:
        return "hub"
    if drivers & (SERIAL_DRIVERS - {"cdc_acm"}):
        return "serial-usb"
    if "cdc_acm" in drivers or "02" in classes:
        return "serial-cdc"
    if any(i["class"] == "03" and i["protocol"] == "01" for i in interfaces):
        return "hid-keyboard"
    if "03" in classes:
        return "hid"
    if "08" in classes:
        return "storage"
    for cls, kind in (("01", "audio"), ("0e", "video"), ("e0", "wireless"), ("ff", "vendor")):
        if cls in classes:
            return kind
    return "other"


def _device_nodes(sys_root):
    """Map USB device name -> ['/dev/...'] by resolving class symlinks back to the USB device
    they hang off (e.g. /sys/class/input/event5 -> .../1-1.3/1-1.3:1.0/.../event5)."""
    found = {}
    sources = [
        ("class/input", re.compile(r"^event\d+$"), "/dev/input/"),
        ("class/tty", re.compile(r"^tty(ACM|USB)\d+$"), "/dev/"),
        ("class/hidraw", re.compile(r"^hidraw\d+$"), "/dev/"),
        ("block", re.compile(r"^sd[a-z]+$"), "/dev/"),
    ]
    for rel, pattern, prefix in sources:
        base = os.path.join(sys_root, rel)
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for n in sorted(names):
            if not pattern.match(n):
                continue
            real = os.path.realpath(os.path.join(base, n))
            owner = None
            for part in real.split(os.sep):        # last plain device component wins (deepest hub)
                if DEVICE_NAME.match(part):
                    owner = part
            if owner:
                found.setdefault(owner, []).append(prefix + n)
    return found


def scan(sys_root="/sys"):
    """Return every USB device (hubs included, root hubs excluded), sorted by port path."""
    base = os.path.join(sys_root, "bus/usb/devices")
    try:
        entries = os.listdir(base)
    except OSError:
        return []
    nodes = _device_nodes(sys_root)
    devices = []
    for name in entries:
        if not DEVICE_NAME.match(name):
            continue
        path = os.path.join(base, name)
        interfaces = []
        for sub in sorted(os.listdir(path)):
            if sub.startswith(name + ":"):
                ip = os.path.join(path, sub)
                interfaces.append({
                    "name": sub,
                    "class": (_read(os.path.join(ip, "bInterfaceClass"), "") or "").lower(),
                    "subclass": (_read(os.path.join(ip, "bInterfaceSubClass"), "") or "").lower(),
                    "protocol": (_read(os.path.join(ip, "bInterfaceProtocol"), "") or "").lower(),
                    "driver": _driver(ip),
                })
        dev_class = (_read(os.path.join(path, "bDeviceClass"), "") or "").lower()
        kind = _classify(dev_class, interfaces)
        bus, port = DEVICE_NAME.match(name).groups()
        power = _read(os.path.join(path, "bMaxPower"), "0mA") or "0mA"
        devices.append({
            "port": name,
            "bus": int(bus),
            "depth": port.count("."),          # 0 = plugged straight into the reader
            "parent": name.rsplit(".", 1)[0] if "." in port else None,
            "vid": (_read(os.path.join(path, "idVendor"), "") or "").lower(),
            "pid": (_read(os.path.join(path, "idProduct"), "") or "").lower(),
            "manufacturer": _read(os.path.join(path, "manufacturer")),
            "product": _read(os.path.join(path, "product")),
            "serial": _read(os.path.join(path, "serial")),
            "speed_mbps": _read(os.path.join(path, "speed")),
            "max_power_ma": int(re.sub(r"\D", "", power) or 0),
            "type": kind,
            "type_label": TYPE_LABELS.get(kind, kind),
            "drivers": sorted({i["driver"] for i in interfaces if i["driver"]}),
            "nodes": nodes.get(name, []),
        })
    devices.sort(key=lambda d: _port_key(d["port"]))
    return devices


def validate_assignments(assignments):
    """Return a list of error strings (empty if valid). assignments: {port: {"role", "vid", "pid"}}."""
    errors = []
    if not isinstance(assignments, dict):
        return ["assignments must be an object"]
    seen = {}
    for port, a in assignments.items():
        if not isinstance(port, str) or not DEVICE_NAME.match(port):
            errors.append(f"bad port '{port}'")
            continue
        if not isinstance(a, dict) or a.get("role") not in ROLES:
            errors.append(f"{port}: unknown role")
            continue
        for k in ("vid", "pid"):
            v = a.get(k)
            if v is not None and not (isinstance(v, str) and re.fullmatch(r"[0-9a-f]{4}", v)):
                errors.append(f"{port}: bad {k}")
        role = a["role"]
        if ROLES[role]["single"]:
            if role in seen:
                errors.append(f"'{ROLES[role]['label']}' is assigned to both {seen[role]} and {port}")
            seen[role] = port
    return errors


def evaluate(devices, assignments):
    """Combine scanned devices with assignments. Each row gets a status:
       ok         assigned, present, right kind of device
       mismatch   assigned, present, wrong kind of device (e.g. a mouse where a badge reader belongs)
       changed    right kind, but a different VID:PID than when it was assigned
       missing    assigned, nothing in that port (row synthesized)
       unassigned present, no role
       ignored    role 'ignore'
       hub        hubs are infrastructure, not assigned
    """
    rows = []
    present = {d["port"] for d in devices}
    for d in devices:
        a = assignments.get(d["port"])
        row = dict(d, role=a["role"] if a else None)
        if d["type"] == "hub":
            row["status"] = "hub"
        elif not a:
            row["status"] = "unassigned"
        elif a["role"] == "ignore":
            row["status"] = "ignored"
        elif d["type"] not in ROLES[a["role"]]["expects"]:
            row["status"] = "mismatch"
            row["status_detail"] = f"Expected {' or '.join(TYPE_LABELS[t] for t in ROLES[a['role']]['expects'])}, found {d['type_label']}"
        elif a.get("vid") and (a.get("vid"), a.get("pid")) != (d["vid"], d["pid"]):
            row["status"] = "changed"
            row["status_detail"] = f"Assigned to {a['vid']}:{a['pid']}, now {d['vid']}:{d['pid']}"
        else:
            row["status"] = "ok"
        rows.append(row)
    for port, a in assignments.items():
        if port not in present and a["role"] != "ignore":
            m = DEVICE_NAME.match(port)
            bus, p = m.groups()
            rows.append({"port": port, "bus": int(bus), "depth": p.count("."),
                         "parent": port.rsplit(".", 1)[0] if "." in p else None,
                         "vid": a.get("vid"), "pid": a.get("pid"), "product": a.get("product"),
                         "manufacturer": None, "serial": None, "speed_mbps": None, "max_power_ma": 0,
                         "type": None, "type_label": None, "drivers": [], "nodes": [],
                         "role": a["role"], "status": "missing",
                         "status_detail": "Assigned device not detected"})
    rows.sort(key=lambda r: _port_key(r["port"]))
    return rows
