"""Startup diagnostic: which USB-serial drivers the kernel has, and a read/write test on each
USB serial port (/dev/ttyUSB*, /dev/ttyACM*). Standard library only.

For each port: open it as Meridian's user, set 115200 8N1 raw, send CR LF, and report what
comes back within ~1.5 s (e.g. a Linux "login:" prompt from a board's console). Sending a
newline is harmless for consoles and for Modbus devices (they ignore it)."""
import glob
import os
import select
import sys
import termios
import time


def drivers():
    out = {}
    for base, label in (("/sys/bus/usb-serial/drivers", "usb-serial"), ("/sys/bus/usb/drivers", "usb")):
        try:
            names = sorted(os.listdir(base))
        except OSError:
            names = []
        if label == "usb":
            names = [n for n in names if n in ("cdc_acm", "usbserial_generic", "ch341", "cp210x", "ftdi_sio", "pl2303")]
        out[label] = names
    return out


WATCH = ("cdc-acm", "cdc_acm", "ftdi_sio", "cp210x", "ch341", "pl2303", "usbserial")


def modules():
    """Serial driver modules on disk (loaded automatically when a matching device is plugged in)."""
    found = set()
    root = f"/lib/modules/{os.uname().release}"
    for dirpath, _, files in os.walk(root):
        for f in files:
            name = f.split(".ko")[0]
            if ".ko" in f and name in WATCH:
                found.add(name.replace("-", "_"))
    return sorted(found)


def port_driver(dev):
    try:
        return os.path.basename(os.readlink(f"/sys/class/tty/{os.path.basename(dev)}/device/driver"))
    except OSError:
        return "?"


def probe(dev, baud=termios.B115200, wait=1.5):
    fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0                                   # iflag: raw
        attrs[1] = 0                                   # oflag
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0                                   # lflag: no echo, non-canonical
        attrs[4] = attrs[5] = baud
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
        os.write(fd, b"\r\n")
        data, end = b"", time.time() + wait
        while time.time() < end and len(data) < 2048:
            r, _, _ = select.select([fd], [], [], max(0.0, end - time.time()))
            if r:
                chunk = os.read(fd, 512)
                if not chunk:
                    break
                data += chunk
        return data
    finally:
        os.close(fd)


def main():
    d, mods = drivers(), modules()
    print("  usb-serial drivers registered:", " ".join(d["usb-serial"]) or "(none)")
    print("  serial driver modules on disk:", " ".join(mods) or "(none)")
    acm = "registered" if "cdc_acm" in d["usb"] else "available as a module" if "cdc_acm" in mods else "NOT found"
    print("  cdc_acm (Pico/RP2040, Numato, many boards):", acm)
    ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    if not ports:
        print("  no USB serial ports to test")
    for dev in ports:
        label = f"  {dev} [{port_driver(dev)}]"
        try:
            reply = probe(dev)
        except OSError as e:
            print(f"{label}: can't open: {e}")
            continue
        text = reply.decode("utf-8", "replace").replace("\r", "").strip()
        if text:
            shown = " | ".join(line.strip() for line in text.splitlines() if line.strip())[-200:]
            print(f"{label}: opened, wrote CR LF, read {len(reply)} bytes: {shown}")
        else:
            print(f"{label}: opened and wrote OK; no reply in 1.5 s at 115200 (normal for devices that only answer commands)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
