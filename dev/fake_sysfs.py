"""Build a fake Linux sysfs tree of USB devices, laid out the way the kernel does it
(real directories under devices/, symlinks from bus/usb/devices, class/*, block/ and driver links).

Used by the mock server and the tests so the *real* scanner in app/core/usb.py is exercised
without a reader. Paths mirror an R700 (xhci host controller, bus 1).
"""
import os
import shutil

HOST = "devices/platform/soc/xhci-hcd.0.auto"

# Mirrors a real R700 v2 (verified on hardware, FW 10.4): the reader's three rear USB ports hang off an
# internal TI hub at 1-1 — left = 1-1.1, middle = 1-1.2, right = 1-1.3. A "7-port" hub is two 4-port
# chips chained (second chip on the first chip's port 4), so its sockets show up as 1-1.1.1..3 and
# 1-1.1.4.1..4. Setup: 7-port hub on the left port, data drive in the middle, GPS on the right.
HUB4 = {"vid": "0bda", "pid": "5411", "manufacturer": "Generic", "product": "4-Port USB 2.0 Hub",
        "speed": "480", "power": "0mA", "dev_class": "09",
        "interfaces": [{"class": "09", "sub": "00", "proto": "02", "driver": "hub"}]}
R700_SETUP = [
    {"port": "1-1", "vid": "0451", "pid": "8142", "manufacturer": None, "product": None, "serial": "EE0B0861696D",
     "speed": "480", "power": "0mA", "dev_class": "09",
     "interfaces": [{"class": "09", "sub": "00", "proto": "02", "driver": "hub"}]},
    dict(HUB4, port="1-1.1"),
    dict(HUB4, port="1-1.1.4"),
    {"port": "1-1.1.1", "vid": "0c27", "pid": "3bfa", "manufacturer": "RFIDeas", "product": "pcProx Plus RDR-80081AKU",
     "speed": "12", "power": "100mA",
     "interfaces": [{"class": "03", "sub": "01", "proto": "01", "driver": "usbhid", "nodes": [("input", "event2"), ("hidraw", "hidraw0")]}]},
    {"port": "1-1.1.2", "vid": "0c27", "pid": "3bfa", "manufacturer": "RFIDeas", "product": "pcProx Plus RDR-80081AKU",
     "speed": "12", "power": "100mA",
     "interfaces": [{"class": "03", "sub": "01", "proto": "01", "driver": "usbhid", "nodes": [("input", "event3"), ("hidraw", "hidraw1")]}]},
    {"port": "1-1.1.3", "vid": "2e8a", "pid": "000a", "manufacturer": "Meridian Sensor Group", "product": "Meridian I/O",
     "serial": "E6614C311B7A3F2C", "speed": "12", "power": "100mA", "dev_class": "ef",
     "interfaces": [{"class": "02", "sub": "02", "proto": "00", "driver": "cdc_acm", "nodes": [("tty", "ttyACM0")]},
                    {"class": "0a", "sub": "00", "proto": "00", "driver": "cdc_acm"}]},
    {"port": "1-1.1.4.1", "vid": "0c2e", "pid": "0b61", "manufacturer": "Honeywell Imaging & Mobility", "product": "1950g",
     "serial": "19310B4E52", "speed": "12", "power": "350mA",
     "interfaces": [{"class": "03", "sub": "01", "proto": "01", "driver": "usbhid", "nodes": [("input", "event4"), ("hidraw", "hidraw2")]}]},
    {"port": "1-1.1.4.2", "vid": "16c0", "pid": "05df", "manufacturer": "www.dcttech.com", "product": "USBRelay2",
     "speed": "1.5", "power": "100mA",
     "interfaces": [{"class": "03", "sub": "00", "proto": "00", "driver": "usbhid", "nodes": [("hidraw", "hidraw3")]}]},
    {"port": "1-1.1.4.3", "vid": "0403", "pid": "6001", "manufacturer": "FTDI", "product": "FT232R USB UART",
     "serial": "A10KZP45", "speed": "12", "power": "90mA",
     "interfaces": [{"class": "ff", "sub": "ff", "proto": "ff", "driver": "ftdi_sio", "nodes": [("ttyusb", "ttyUSB0")]}]},
    {"port": "1-1.2", "vid": "0781", "pid": "5581", "manufacturer": "SanDisk", "product": "Ultra",
     "serial": "4C530001230725117413", "speed": "480", "power": "224mA",
     "interfaces": [{"class": "08", "sub": "06", "proto": "50", "driver": "usb-storage", "nodes": [("block", "sda")]}]},
    {"port": "1-1.3", "vid": "1546", "pid": "01a7", "manufacturer": "u-blox AG - www.u-blox.com",
     "product": "u-blox 7 - GPS/GNSS Receiver", "speed": "12", "power": "100mA", "dev_class": "02",
     "interfaces": [{"class": "02", "sub": "02", "proto": "01", "driver": "cdc_acm", "nodes": [("tty", "ttyACM1")]},
                    {"class": "0a", "sub": "00", "proto": "00", "driver": "cdc_acm"}]},
]


def _w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text + "\n")


def _link(target, link):
    os.makedirs(os.path.dirname(link), exist_ok=True)
    os.symlink(os.path.relpath(target, os.path.dirname(link)), link)


def build(root, devices=R700_SETUP):
    """(Re)create a fake sysfs under `root` containing `devices`."""
    if os.path.exists(root):
        shutil.rmtree(root)
    usb1 = os.path.join(root, HOST, "usb1")
    _w(os.path.join(usb1, "idVendor"), "1d6b")          # root hub (scanner skips it)
    _link(usb1, os.path.join(root, "bus/usb/devices/usb1"))
    drivers = os.path.join(root, "bus/usb/drivers")
    hid_seq = 0
    for d in devices:
        port = d["port"]
        bus, chain = port.split("-")
        parts = chain.split(".")
        segs = [f"{bus}-{'.'.join(parts[:i + 1])}" for i in range(len(parts))]
        path = os.path.join(usb1, *segs)
        for k, v in (("idVendor", d["vid"]), ("idProduct", d["pid"]), ("manufacturer", d.get("manufacturer")),
                     ("product", d.get("product")), ("serial", d.get("serial")), ("speed", d["speed"]),
                     ("bMaxPower", d["power"]), ("bDeviceClass", d.get("dev_class", "00"))):
            if v is not None:
                _w(os.path.join(path, k), v)
        _link(path, os.path.join(root, "bus/usb/devices", port))
        for n, itf in enumerate(d["interfaces"]):
            iname = f"{port}:1.{n}"
            ip = os.path.join(path, iname)
            _w(os.path.join(ip, "bInterfaceClass"), itf["class"])
            _w(os.path.join(ip, "bInterfaceSubClass"), itf["sub"])
            _w(os.path.join(ip, "bInterfaceProtocol"), itf["proto"])
            os.makedirs(os.path.join(drivers, itf["driver"]), exist_ok=True)
            _link(os.path.join(drivers, itf["driver"]), os.path.join(ip, "driver"))
            _link(ip, os.path.join(root, "bus/usb/devices", iname))
            for kind, node in itf.get("nodes", []):
                if kind in ("input", "hidraw"):
                    hid_seq += 1
                    hid = os.path.join(ip, f"0003:{d['vid'].upper()}:{d['pid'].upper()}.{hid_seq:04X}")
                    if kind == "input":
                        target = os.path.join(hid, "input", f"input{node[5:]}", node)
                    else:
                        target = os.path.join(hid, "hidraw", node)
                    cls = "class/" + kind
                elif kind == "tty":
                    target, cls = os.path.join(ip, "tty", node), "class/tty"
                elif kind == "ttyusb":
                    target, cls = os.path.join(ip, node, "tty", node), "class/tty"
                elif kind == "block":
                    target, cls = os.path.join(ip, "host0/target0:0:0/0:0:0:0/block", node), "block"
                os.makedirs(target, exist_ok=True)
                _link(target, os.path.join(root, cls, node))
    return root


def without(ports, devices=R700_SETUP):
    """Device list with the given ports (and anything plugged into them) removed — simulates unplugging."""
    def gone(p):
        return any(p == u or p.startswith(u + ".") for u in ports)
    return [d for d in devices if not gone(d["port"])]
