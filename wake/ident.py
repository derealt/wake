"""What a thing is, independent of its name.

A file is known by device, inode and birth time. The name can change (moves) and an inode
number can be reused after a delete, but device + inode + birth time names one file for its
whole life. A process is known by pid and start time, for the same reason.
"""
import ctypes
import os
from collections import namedtuple

_libc = ctypes.CDLL(None, use_errno=True)
_HAS_STATX = hasattr(_libc, "statx")
AT_SYMLINK_NOFOLLOW = 0x100
STATX_BTIME = 0x800
STATX_ALL = 0x7FF | STATX_BTIME

Ident = namedtuple("Ident", "key mode nlink size")


def identity(path, follow=False):
    """Return the file's lifelong identity, or None if nothing is there."""
    if _HAS_STATX:
        buf = ctypes.create_string_buffer(256)
        flags = 0 if follow else AT_SYMLINK_NOFOLLOW
        if _libc.statx(-100, os.fsencode(path), flags, STATX_ALL, buf) == 0:
            raw = buf.raw
            mask = int.from_bytes(raw[0:4], "little")
            key = "{}:{}:{}".format(int.from_bytes(raw[136:140], "little"), int.from_bytes(raw[140:144], "little"),
                                    int.from_bytes(raw[32:40], "little"))
            if mask & STATX_BTIME:
                key += ":{}.{:09d}".format(int.from_bytes(raw[80:88], "little", signed=True),
                                           int.from_bytes(raw[88:92], "little"))
            return Ident(key, int.from_bytes(raw[28:30], "little"), int.from_bytes(raw[16:20], "little"),
                         int.from_bytes(raw[40:48], "little"))
        return None
    try:
        st = os.stat(path, follow_symlinks=follow)
    except OSError:
        return None
    return Ident(f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}:{st.st_ino}", st.st_mode, st.st_nlink, st.st_size)


def start_time(pid):
    """The process's start time in clock ticks since boot (field 22 of /proc/<pid>/stat)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            s = f.read()
        return int(s[s.rindex(b")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def proc_key(pid):
    t = start_time(pid)
    return None if t is None else f"{pid}:{t}"


def tgid_of(tid):
    try:
        with open(f"/proc/{tid}/status") as f:
            for line in f:
                if line.startswith("Tgid:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return tid


def cmdline(pid, limit=300):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            parts = [p.decode("utf-8", "replace") for p in f.read().split(b"\0") if p]
        return " ".join(parts)[:limit]
    except OSError:
        return ""


def cmd_args(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [p.decode("utf-8", "replace") for p in f.read().split(b"\0") if p]
    except OSError:
        return []


def uid_of(pid):
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None
