"""Data-drive status: where a USB drive is mounted, filesystem type, capacity, what's using it.

Standard library only. Reads /proc/mounts and calls statvfs, so it works on the reader unchanged.
Mounting, ejecting and formatting are done by the engine (they need to coordinate with the
outbox writer so nothing is mid-write) and are not in this module.
"""
import os
import re

RECOMMENDED_FS = "ext4"


def find_mount(device_names, proc_mounts="/proc/mounts"):
    """Return {"device", "mount_point", "fs_type", "read_only"} for the first mount whose source
    is one of device_names (e.g. ["/dev/sda", "/dev/sda1"]) or a partition of them; else None."""
    try:
        with open(proc_mounts, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        src, mnt, fs, opts = parts[:4]
        for dev in device_names:
            if src == dev or (src.startswith(dev) and src[len(dev):].isdigit()):
                return {"device": src, "mount_point": _unescape(mnt), "fs_type": fs,
                        "read_only": "ro" in opts.split(",")}
    return None


def _unescape(path):
    # /proc/mounts escapes spaces etc. as octal (\040)
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), path)


def capacity(path):
    """Return {"size", "used", "free"} in bytes for the filesystem holding path, or None."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    size = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return {"size": size, "used": size - st.f_bfree * st.f_frsize, "free": free}


def dir_size(path):
    """Total bytes of regular files under path (symlinks not followed). 0 if missing."""
    total = 0
    if os.path.isfile(path):
        return os.path.getsize(path)
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            p = os.path.join(root, name)
            try:
                if not os.path.islink(p):
                    total += os.path.getsize(p)
            except OSError:
                pass
    return total


def write_test(mount_point, subdir="meridian"):
    """Try to create, write, fsync and delete a small file in <mount>/<subdir>.
    Returns None if that works, else the error text. Creates <subdir> (Meridian's folder) if needed."""
    d = os.path.join(mount_point, subdir)
    probe = os.path.join(d, ".write_test")
    try:
        os.makedirs(d, exist_ok=True)
        with open(probe, "w") as f:
            f.write("meridian write test\n")
            f.flush()
            os.fsync(f.fileno())
        os.remove(probe)
        return None
    except OSError as e:
        return f"{e.strerror} ({d})"


def warnings_for(mount, cap, alert_free_pct):
    out = []
    if mount and mount["fs_type"] != RECOMMENDED_FS:
        out.append(f"Filesystem is {mount['fs_type']}; {RECOMMENDED_FS} is recommended "
                   "(survives power loss better, no 4 GB file limit).")
    if mount and mount["read_only"]:
        out.append("Drive is mounted read-only — usually a sign of filesystem errors or a worn-out drive.")
    if cap and cap["size"] and cap["free"] * 100 / cap["size"] < alert_free_pct:
        out.append(f"Less than {alert_free_pct}% free space.")
    return out
