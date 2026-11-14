"""The supervisor: runs an agent under WAKE's two laws.

Law 1. An agent can only destroy what it made. Before any call that would delete, replace,
       move or overwrite one of your things, WAKE keeps it. A signal or a debugger reaching
       one of your processes is refused; a broadcast signal reaches only the agent's own.
Law 2. When you stop the agent, everything it made stops with it: every process it started,
       wherever it has wandered, and every file it planted that would make something run later.

The kernel stops the agent only on calls that could destroy something (see seccomp.py).
At each stop WAKE asks the agent's body graph (body.py) and answers in microseconds.
"""
import ctypes
import errno
import json
import os
import select
import signal
import socket
import stat
import time
from collections import Counter

from . import seccomp
from .arch import (AT_FDCWD, AT_REMOVEDIR, O_CREAT, O_EXCL, O_TRUNC, O_WRONLY, RENAME_EXCHANGE,
                   RENAME_NOREPLACE, WRITE_FLAGS, Machine, s32, s64)
from .body import Body
from .ident import cmd_args, cmdline, identity, proc_key, tgid_of, uid_of
from .restore import restore_one
from .triggers import is_trigger
from .vault import Vault

libc = ctypes.CDLL(None, use_errno=True)
libc.ptrace.restype = ctypes.c_long
libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]


class IOV(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("len", ctypes.c_size_t)]


libc.process_vm_readv.restype = ctypes.c_ssize_t
libc.process_vm_readv.argtypes = [ctypes.c_int, ctypes.POINTER(IOV), ctypes.c_ulong,
                                  ctypes.POINTER(IOV), ctypes.c_ulong, ctypes.c_ulong]

PTRACE_TRACEME, PTRACE_CONT, PTRACE_SYSCALL = 0, 7, 24
PTRACE_SETOPTIONS, PTRACE_GETEVENTMSG = 0x4200, 0x4201
PTRACE_GET_SYSCALL_INFO = 0x420E
OPTIONS = 0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x80 | 0x100000   # sysgood fork vfork clone exec seccomp exitkill
EV_FORK, EV_VFORK, EV_CLONE, EV_EXEC, EV_SECCOMP = 1, 2, 3, 4, 7
WALL = 0x40000000
SKIP = ("/proc/", "/sys/", "/dev/")
CONT, SYSCALL = PTRACE_CONT, PTRACE_SYSCALL


def ptrace(req, pid, addr=0, data=0):
    ctypes.set_errno(0)
    r = libc.ptrace(req, pid, addr, data)
    if r == -1:
        e = ctypes.get_errno()
        if e:
            raise OSError(e, os.strerror(e))
    return r


def parent_of(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            s = f.read()
        return int(s[s.rindex(b")") + 2:].split()[1])
    except (OSError, ValueError):
        return None


class Supervisor:
    def __init__(self, session, stop_on_exit=True, triggers=True, sweep=True):
        self.s = session
        self.m = Machine()
        self.stop_on_exit = stop_on_exit
        self.do_triggers = triggers
        self.do_sweep = sweep
        self.protect = os.path.realpath(session.home) + "/"
        self.body = None
        self.vault = Vault(session.vault)
        self.tracees = {}          # tid -> tgid, every thread we trace
        self.keys = {}             # tgid -> process key, every live process of the body
        self.pending = {}          # tid -> what to do when its current call returns
        self.fresh = set()         # new tids announced by a fork event, first stop not yet seen
        self.held = set()          # new tids whose first stop came before their parent's fork event
        self.root = None
        self.root_status = None
        self.stopping = False
        self.stop_reason = None
        self.stop_asked = None
        self.counts = Counter()
        self.log = open(session.events, "a", buffering=1)
        self.handlers = {}
        for n in ("open", "creat", "openat", "openat2"):
            self.handlers[n] = self.h_open
        for n in ("unlink", "rmdir", "unlinkat"):
            self.handlers[n] = self.h_unlink
        for n in ("rename", "renameat", "renameat2"):
            self.handlers[n] = self.h_rename
        for n in ("mkdir", "mkdirat", "symlink", "symlinkat"):
            self.handlers[n] = self.h_make
        for n in ("kill", "tkill", "tgkill", "pidfd_send_signal"):
            self.handlers[n] = self.h_signal
        self.handlers["truncate"] = self.h_truncate
        self.handlers["ptrace"] = self.h_reach
        self.handlers["process_vm_writev"] = self.h_reach

    # ================================================================ start and loop
    def run(self, argv, env):
        libc.prctl(36, ctypes.c_ulong(1), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))  # child subreaper
        pid = os.fork()
        if pid == 0:
            try:
                ptrace(PTRACE_TRACEME, 0)
                os.kill(os.getpid(), signal.SIGSTOP)
                seccomp.install(self.m)
                os.execvpe(argv[0], argv, env)
            except BaseException as e:   # noqa: BLE001
                try:
                    os.write(2, f"wake: cannot start {argv[0]}: {e}\n".encode())
                finally:
                    os._exit(127)
        self.root = pid
        os.waitpid(pid, WALL)
        ptrace(PTRACE_SETOPTIONS, pid, 0, OPTIONS)
        self.body = Body(self.s.store, self.s.id, meta={"cmd": " ".join(argv)[:300], "cwd": os.getcwd(),
                                                       "machine": self.m.name, "uid": os.getuid(),
                                                       "supervisor": os.getpid()})
        key = proc_key(pid)
        self.tracees[pid] = pid
        self.keys[pid] = key
        self.body.root(key, pid, " ".join(argv)[:300], os.getcwd())
        self.note("start", pid=pid, cmd=argv)
        self.s.update(status="running", supervisor=os.getpid(), agent=pid)
        ptrace(PTRACE_CONT, pid, 0, 0)
        return self.loop()

    def loop(self):
        rfd, wfd = os.pipe()
        os.set_blocking(rfd, False)
        os.set_blocking(wfd, False)
        signal.set_wakeup_fd(wfd)
        signal.signal(signal.SIGCHLD, lambda *_: None)
        signal.signal(signal.SIGINT, signal.SIG_IGN)       # Ctrl-C belongs to the agent
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGUSR1):
            signal.signal(sig, lambda s, _f: setattr(self, "stop_asked", f"signal {s}"))
        ctl = self.open_control()
        while self.tracees:
            if self.stop_asked and not self.stopping:
                self.begin_stop(self.stop_asked)
            busy = self.reap()
            if not self.tracees:
                break
            if not busy:
                ready, _, _ = select.select([rfd] + ([ctl] if ctl else []), [], [], 0.1)
                if rfd in ready:
                    try:
                        while os.read(rfd, 512):
                            pass
                    except BlockingIOError:
                        pass
                if ctl in ready:
                    self.serve_control(ctl)
        summary = self.finish()
        if ctl:
            ctl.close()
            try:
                os.unlink(self.s.control)
            except OSError:
                pass
        return summary

    def reap(self):
        busy = False
        while True:
            try:
                tid, status = os.waitpid(-1, os.WNOHANG | WALL)
            except ChildProcessError:
                self.tracees.clear()
                return busy
            if tid == 0:
                return busy
            busy = True
            self.handle(tid, status)

    def handle(self, tid, status):
        if os.WIFEXITED(status) or os.WIFSIGNALED(status):
            self.exited(tid, status)
            return
        if not os.WIFSTOPPED(status):
            return
        sig, event = os.WSTOPSIG(status), status >> 16
        resume, inject = CONT, 0
        try:
            if sig == signal.SIGTRAP and event == EV_SECCOMP:
                resume = self.on_seccomp(tid)
            elif sig == signal.SIGTRAP | 0x80:
                self.on_exit(tid)
            elif sig == signal.SIGTRAP and event in (EV_FORK, EV_VFORK, EV_CLONE):
                self.on_fork(tid)
            elif sig == signal.SIGTRAP and event == EV_EXEC:
                self.on_exec(tid)
            elif sig == signal.SIGTRAP and event:
                pass
            elif sig == signal.SIGSTOP and tid in self.fresh:
                self.fresh.discard(tid)                       # first stop of a new thread: not a real signal
            elif sig == signal.SIGSTOP and tid not in self.tracees:
                self.tracees[tid] = tgid_of(tid)              # its parent's fork event has not arrived yet:
                self.held.add(tid)                            # hold it until the body knows who made it
                return
            else:
                inject = sig
        except OSError:
            pass
        if self.stopping:
            self.kill_tid(tid)
        if tid in self.pending and resume == CONT:
            resume = SYSCALL
        try:
            ptrace(resume, tid, 0, inject)
        except OSError:
            pass

    def exited(self, tid, status):
        tg = self.tracees.pop(tid, None)
        self.pending.pop(tid, None)
        self.fresh.discard(tid)
        self.held.discard(tid)
        if tid == self.root:
            self.root_status = status
            if self.stop_on_exit and not self.stopping:
                self.begin_stop("the agent exited")
        if tg is not None and tid == tg and not any(v == tg for v in self.tracees.values()):
            key = self.keys.pop(tg, None)
            if key:
                self.body.died(key)

    # ================================================================ processes
    def on_fork(self, tid):
        new = ctypes.c_ulong()
        ptrace(PTRACE_GETEVENTMSG, tid, 0, ctypes.addressof(new))
        child = new.value
        ptg = self.tracees.get(tid) or tgid_of(tid)
        ctg = tgid_of(child)
        if ctg != ptg and ctg not in self.keys:
            key = proc_key(ctg)
            if key:
                self.keys[ctg] = key
                self.body.child(self.ensure(ptg), key, ctg)
        if child in self.held:
            self.held.discard(child)
            if self.stopping:
                self.kill_tid(child)
            try:
                ptrace(CONT, child, 0, 0)
            except OSError:
                pass
        elif child not in self.tracees:
            self.tracees[child] = ctg
            self.fresh.add(child)

    def on_exec(self, tid):
        tg = self.tracees.get(tid) or tgid_of(tid)
        key = self.ensure(tg)
        try:
            exe = os.readlink(f"/proc/{tg}/exe")
        except OSError:
            exe = None
        self.body.executed(key, cmdline(tg), exe)

    def ensure(self, pid):
        """The body key of a live process of the agent, recording it if the fork event is late."""
        key = self.keys.get(pid)
        if key:
            return key
        key = proc_key(pid)
        parent = self.keys.get(parent_of(pid)) or self.keys.get(self.root)
        self.keys[pid] = key
        self.body.child(parent, key, pid)
        return key

    def kill_tid(self, tid):
        try:
            os.kill(tid, signal.SIGKILL)
        except OSError:
            pass

    # ================================================================ Law 1, at each stop
    def on_seccomp(self, tid):
        try:
            _op, nr, a = self.syscall_info(tid)
        except OSError:
            return CONT
        name = self.m.name_of.get(nr)
        handler = self.handlers.get(name)
        if not handler:
            return CONT
        pid = self.tracees.get(tid) or tgid_of(tid)
        me = self.ensure(pid)
        self.counts["checked"] += 1
        try:
            return handler(tid, me, name, a)
        except OSError as e:
            self.note("error", call=name, error=str(e))
            return CONT

    def syscall_info(self, tid):
        buf = (ctypes.c_uint8 * 88)()
        ptrace(PTRACE_GET_SYSCALL_INFO, tid, ctypes.sizeof(buf), ctypes.addressof(buf))
        words = (ctypes.c_uint64 * 8).from_buffer(buf, 24)
        return buf[0], words[0], list(words[1:7])

    def read_bytes(self, tid, addr, size):
        if not addr:
            return None
        buf = ctypes.create_string_buffer(size)
        first = min(size, 4096 - (addr % 4096))
        local = IOV(ctypes.addressof(buf), size)
        remote = (IOV * 2)(IOV(addr, first), IOV(addr + first, size - first))
        n = libc.process_vm_readv(tid, ctypes.byref(local), 1, remote, 2 if size > first else 1, 0)
        return buf.raw[:n] if n > 0 else None

    def read_str(self, tid, addr):
        raw = self.read_bytes(tid, addr, 4096)
        if not raw:
            return None
        i = raw.find(b"\0")
        return os.fsdecode(raw[:i]) if i >= 0 else None

    def resolve(self, tid, dirfd, addr, follow=False):
        p = self.read_str(tid, addr)
        if not p:
            return None
        if not p.startswith("/"):
            fd = s32(dirfd)
            try:
                base = os.readlink(f"/proc/{tid}/cwd" if fd == AT_FDCWD else f"/proc/{tid}/fd/{fd}")
            except OSError:
                return None
            p = os.path.join(base, p)
        p = p.rstrip("/") or "/"
        head, tail = os.path.split(p)
        if tail in ("", ".", ".."):
            return os.path.realpath(p)
        full = os.path.join(os.path.realpath(head), tail)
        return os.path.realpath(full) if follow else full

    def protected(self, path):
        return path.startswith(self.protect) or path + "/" == self.protect

    def h_open(self, tid, me, name, a):
        if name == "open":
            dirfd, addr, flags = AT_FDCWD, a[0], a[1]
        elif name == "creat":
            dirfd, addr, flags = AT_FDCWD, a[0], O_WRONLY | O_CREAT | O_TRUNC
        elif name == "openat":
            dirfd, addr, flags = a[0], a[1], a[2]
        else:
            dirfd, addr = a[0], a[1]
            raw = self.read_bytes(tid, a[2], 8)
            flags = int.from_bytes(raw, "little") if raw else 0
        if not flags & WRITE_FLAGS:
            return CONT
        path = self.resolve(tid, dirfd, addr, follow=True)
        if not path or path.startswith(SKIP):
            return CONT
        if self.protected(path):
            return self.refuse(tid, me, name, path, "WAKE's own records")
        t = identity(path, follow=True)
        if t is None:
            if flags & O_CREAT:
                self.pending[tid] = ("made", me, path, "file")
                return SYSCALL
            return CONT
        if flags & O_CREAT and flags & O_EXCL:
            return CONT
        if stat.S_ISREG(t.mode) and not self.body.mine_file(t.key):
            self.keep(me, path, t, "overwritten")
        return CONT

    def h_truncate(self, tid, me, name, a):
        path = self.resolve(tid, AT_FDCWD, a[0], follow=True)
        if not path or path.startswith(SKIP):
            return CONT
        if self.protected(path):
            return self.refuse(tid, me, name, path, "WAKE's own records")
        t = identity(path, follow=True)
        if t and stat.S_ISREG(t.mode) and not self.body.mine_file(t.key):
            self.keep(me, path, t, "overwritten")
        return CONT

    def h_unlink(self, tid, me, name, a):
        if name == "unlink":
            dirfd, addr, isdir = AT_FDCWD, a[0], False
        elif name == "rmdir":
            dirfd, addr, isdir = AT_FDCWD, a[0], True
        else:
            dirfd, addr, isdir = a[0], a[1], bool(a[2] & AT_REMOVEDIR)
        path = self.resolve(tid, dirfd, addr)
        if not path or path.startswith(SKIP):
            return CONT
        if self.protected(path):
            return self.refuse(tid, me, name, path, "WAKE's own records")
        t = identity(path)
        if t is None:
            return CONT
        if self.body.mine_file(t.key):
            self.counts["own_destroyed"] += 1
            if isdir or t.nlink <= 1:
                self.pending[tid] = ("gone", t.key)
                return SYSCALL
            return CONT
        if isdir or stat.S_ISDIR(t.mode):
            self.keep_dir(me, path, t, "deleted")
        else:
            self.keep(me, path, t, "deleted")
        return CONT

    def h_rename(self, tid, me, name, a):
        if name == "rename":
            od, oa, nd, na, fl = AT_FDCWD, a[0], AT_FDCWD, a[1], 0
        elif name == "renameat":
            od, oa, nd, na, fl = a[0], a[1], a[2], a[3], 0
        else:
            od, oa, nd, na, fl = a[0], a[1], a[2], a[3], s32(a[4])
        old, new = self.resolve(tid, od, oa), self.resolve(tid, nd, na)
        if not old or not new or old.startswith(SKIP):
            return CONT
        if self.protected(old) or self.protected(new):
            return self.refuse(tid, me, name, f"{old} -> {new}", "WAKE's own records")
        o = identity(old)
        if o is None:
            return CONT
        n = identity(new)
        exchange = bool(fl & RENAME_EXCHANGE)
        o_mine = self.body.mine_file(o.key)
        n_mine = n is not None and self.body.mine_file(n.key)
        replaced_own = None
        if n is not None and not exchange and not fl & RENAME_NOREPLACE and n.key != o.key:
            if n_mine:
                if stat.S_ISDIR(n.mode) or n.nlink <= 1:
                    replaced_own = n.key
            elif stat.S_ISDIR(n.mode):
                self.keep_dir(me, new, n, "replaced")
            else:
                self.keep(me, new, n, "replaced")
        if not o_mine:
            self.note_move(me, old, o)
        if exchange and n is not None and not n_mine:
            self.note_move(me, new, n)
        self.pending[tid] = ("moved", o.key, o_mine, new, old, n.key if (exchange and n) else None, n_mine,
                             replaced_own)
        return SYSCALL

    def h_make(self, tid, me, name, a):
        if name == "mkdir":
            d, addr, kind = AT_FDCWD, a[0], "dir"
        elif name == "mkdirat":
            d, addr, kind = a[0], a[1], "dir"
        elif name == "symlink":
            d, addr, kind = AT_FDCWD, a[1], "link"
        else:
            d, addr, kind = a[1], a[2], "link"
        path = self.resolve(tid, d, addr)
        if not path or path.startswith(SKIP):
            return CONT
        if self.protected(path):
            return self.refuse(tid, me, name, path, "WAKE's own records")
        if identity(path) is not None:
            return CONT
        self.pending[tid] = ("made", me, path, kind)
        return SYSCALL

    def h_signal(self, tid, me, name, a):
        caller = self.tracees.get(tid) or tgid_of(tid)
        if name == "kill":
            target, sig = s32(a[0]), s32(a[1])
        elif name == "tkill":
            target, sig = tgid_of(s32(a[0])), s32(a[1])
        elif name == "tgkill":
            target, sig = s32(a[0]), s32(a[2])
        else:
            sig, target = s32(a[1]), self.pidfd_pid(tid, s32(a[0]))
            if target is None:
                return CONT
        if sig == 0:
            return CONT
        if target > 0:
            if target == caller or self.mine_pid(target) is not False:
                return CONT
            return self.refuse_reach(tid, me, name, target, f"signal {sig}")
        if target == -1:
            members = [r["pid"] for r in self.body.alive() if r["pid"] != caller]
            outsiders = ["everything else this user runs"]
            inside = False
        else:
            group = os.getpgid(caller) if target == 0 else -target
            pids = self.pids_in_group(group)
            inside = caller in pids
            members = [p for p in pids if p != caller and self.mine_pid(p)]
            outsiders = [p for p in pids if p != caller and not self.mine_pid(p)]
            if not outsiders:
                return CONT
        for p in members:
            try:
                os.kill(p, sig)
            except OSError:
                pass
        self.counts["narrowed"] += 1
        detail = f"{name}({target}, signal {sig}) reached {len(members)} of the agent's processes and none of yours"
        self.body.event("narrowed", detail, me)
        self.note("narrowed", detail=detail, by=me)
        self.pending[tid] = ("ret", 0)
        self.m.cancel(ptrace, tid)
        if inside:
            try:
                os.kill(caller, sig)
            except OSError:
                pass
        return SYSCALL

    def h_reach(self, tid, me, name, a):
        if name == "ptrace":
            if s32(a[0]) == PTRACE_TRACEME:
                return CONT
            target = s32(a[1])
        else:
            target = s32(a[0])
        if target <= 0 or self.mine_pid(target) is not False:
            return CONT
        return self.refuse_reach(tid, me, name, target, name)

    # ---------------------------------------------------------------- helpers for Law 1
    def mine_pid(self, pid):
        """True if the process is the agent's, False if it is not, None if it does not exist."""
        key = proc_key(pid)
        if key is None:
            return None
        return self.body.mine_process(key)

    def pids_in_group(self, group):
        out = []
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    if os.getpgid(int(d)) == group:
                        out.append(int(d))
                except OSError:
                    pass
        return out

    def pidfd_pid(self, tid, fd):
        try:
            with open(f"/proc/{tid}/fdinfo/{fd}") as f:
                for line in f:
                    if line.startswith("Pid:"):
                        return int(line.split()[1])
        except (OSError, ValueError):
            pass
        return None

    def keep(self, me, path, t, how):
        entry = self.body.kept_entry(t.key)
        want_link = how in ("deleted", "replaced")
        kind = "link" if stat.S_ISLNK(t.mode) else "file"
        if entry is None:
            store, sk = self.vault.keep(path, prefer_link=want_link)
            self.counts["kept"] += 1
            self.note("kept", path=path, how=how, by=me)
        elif entry["store"] is None:
            store, sk = self.vault.keep(path, prefer_link=want_link)
        elif entry["sk"] == "link" and how == "overwritten":
            store, sk = self.vault.to_copy(entry["store"])
            self.body.kept_store(t.key, store, sk)
            store = sk = None
        else:
            store = sk = None
        self.body.kept(me, t.key, path, kind, how, store, sk, t.size, stat.S_IMODE(t.mode))

    def keep_dir(self, me, path, t, how):
        if self.body.kept_entry(t.key) is None:
            self.counts["kept"] += 1
        self.body.kept(me, t.key, path, "dir", how, None, None, 0, stat.S_IMODE(t.mode))

    def note_move(self, me, path, t):
        if self.body.kept_entry(t.key) is None:
            kind = "dir" if stat.S_ISDIR(t.mode) else "link" if stat.S_ISLNK(t.mode) else "file"
            self.counts["kept"] += 1
            self.body.kept(me, t.key, path, kind, "moved", None, None, t.size, stat.S_IMODE(t.mode))
            self.note("kept", path=path, how="moved", by=me)

    def refuse(self, tid, me, call, target, why):
        self.m.cancel(ptrace, tid)
        self.pending[tid] = ("ret", -errno.EPERM)
        self.counts["refused"] += 1
        self.body.event("refused", f"{call} on {target}: {why}", me)
        self.note("refused", call=call, target=target, why=why, by=me)
        return SYSCALL

    def refuse_reach(self, tid, me, call, target, what):
        key = proc_key(target)
        self.body.tried(me, call, key, target, cmdline(target), what)
        self.note("refused", call=call, target=target, what=what, by=me, target_cmd=cmdline(target))
        self.m.cancel(ptrace, tid)
        self.pending[tid] = ("ret", -errno.EPERM)
        self.counts["refused"] += 1
        return SYSCALL

    def on_exit(self, tid):
        todo = self.pending.pop(tid, None)
        if todo is None:
            return
        if todo[0] == "ret":
            self.m.set_return(ptrace, tid, todo[1])
            return
        try:
            _op, rval, _ = self.syscall_info(tid)
        except OSError:
            return
        if s64(rval) < 0:
            return
        kind = todo[0]
        if kind == "made":
            _, me, path, k = todo
            t = identity(path)
            if t:
                self.body.made(me, t.key, path, k)
                self.counts["made"] += 1
        elif kind == "gone":
            self.body.gone(todo[1])
        elif kind == "moved":
            _, okey, o_mine, new, old, nkey, n_mine, replaced_own = todo
            if replaced_own:
                self.body.gone(replaced_own)
            if o_mine:
                self.body.renamed(okey, new)
            else:
                self.body.kept_now(okey, new)
            if nkey:
                if n_mine:
                    self.body.renamed(nkey, old)
                else:
                    self.body.kept_now(nkey, old)

    # ================================================================ Law 2
    def begin_stop(self, reason):
        if self.stopping:
            return
        self.stopping = True
        self.stop_reason = reason
        self.stop_t0 = time.monotonic()
        self.note("stopping", reason=reason, tracees=len(self.tracees))
        for tid in list(self.tracees):
            self.kill_tid(tid)
        for tid in list(self.held):
            try:
                ptrace(CONT, tid, 0, 0)
            except OSError:
                pass

    def finish(self):
        removed, reverted, swept, escaped = [], [], [], []
        if self.stopping:
            if self.do_sweep:                 # first, while its planted files are still where it put them
                swept = self.sweep()
            if self.do_triggers:
                removed, reverted = self.clean_triggers()
        for r in self.body.alive():
            if proc_key(r["pid"]) == r["key"] and self.stopping:
                try:
                    os.kill(r["pid"], signal.SIGKILL)
                    escaped.append(r["pid"])
                except OSError:
                    pass
            self.body.died(r["key"])
        took = round((time.monotonic() - self.stop_t0) * 1000, 1) if self.stopping else None
        c = self.body.counts()
        summary = {"stopped": self.stopping, "reason": self.stop_reason, "ms": took,
                   "removed_triggers": removed, "restored_triggers": reverted, "swept": swept,
                   "escaped": escaped, "processes": c.get("processes"), "made": c.get("made"),
                   "kept": c.get("kept"), "refused": c.get("refused"), "narrowed": self.counts["narrowed"],
                   "checked": self.counts["checked"]}
        if self.root_status is not None:
            summary["exit"] = os.waitstatus_to_exitcode(self.root_status)
        self.body.event("stopped" if self.stopping else "finished", json.dumps(summary))
        self.body.session_set(status="stopped" if self.stopping else "finished", ended=time.time())
        self.note("finished", **summary)
        self.s.update(status="stopped" if self.stopping else "finished", summary=summary)
        self.body.close()
        return summary

    def clean_triggers(self):
        removed, reverted = [], []
        for k in self.body.kept_all():
            if k["kind"] != "dir" and k["store"] and not k["restored"] and is_trigger(k["path"]):
                if any(w in ("overwritten", "deleted", "replaced") for w in (k["whys"] or [])):
                    restore_one(self.body, self.vault, k)
                    reverted.append(k["path"])
        for f in self.body.made_live_files():
            if f["kind"] != "dir" and not f["adopted"] and is_trigger(f["path"]) and os.path.lexists(f["path"]):
                t = identity(f["path"])
                if t and t.key == f["key"]:
                    self.vault.move_in(f["path"], "removed")
                    self.body.gone(f["key"])
                    removed.append(f["path"])
        return removed, reverted

    def sweep(self):
        """Processes the agent did not start but that are running a file it made."""
        made = {f["key"]: f["path"] for f in self.body.made_live_files() if not f["adopted"] and f["kind"] != "dir"}
        found = []
        if not made:
            return found
        me, uid = os.getpid(), os.getuid()
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            pid = int(d)
            if pid in (1, me) or pid in self.tracees or (uid != 0 and uid_of(pid) != uid):
                continue
            hit = None
            t = identity(f"/proc/{pid}/exe", follow=True)
            if t and t.key in made:
                hit = made[t.key]
            if not hit:                        # a script an interpreter is reading holds an open file
                try:
                    fds = os.listdir(f"/proc/{pid}/fd")
                except OSError:
                    fds = []
                for fd in fds:
                    t = identity(f"/proc/{pid}/fd/{fd}", follow=True)
                    if t and t.key in made:
                        hit = made[t.key]
                        break
            if not hit:
                try:
                    cwd = os.readlink(f"/proc/{pid}/cwd")
                except OSError:
                    cwd = "/"
                for arg in cmd_args(pid)[1:4]:
                    if arg.startswith("-"):
                        continue
                    t = identity(arg if arg.startswith("/") else os.path.join(cwd, arg), follow=True)
                    if t and t.key in made:
                        hit = made[t.key]
                        break
            if hit:
                cmd = cmdline(pid)
                try:
                    os.kill(pid, signal.SIGKILL)
                    found.append({"pid": pid, "cmd": cmd, "file": hit})
                    self.body.event("swept", f"process {pid} `{cmd}` was running {hit}, which the agent made")
                except OSError:
                    pass
        return found

    # ================================================================ control socket
    def open_control(self):
        try:
            if os.path.exists(self.s.control):
                os.unlink(self.s.control)
            ctl = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            ctl.bind(self.s.control)
            os.chmod(self.s.control, 0o600)
            ctl.listen(8)
            ctl.setblocking(False)
            return ctl
        except OSError as e:
            self.note("error", control=str(e))
            return None

    def serve_control(self, ctl):
        try:
            conn, _ = ctl.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(2)
            try:
                req = json.loads(conn.makefile().readline() or "{}")
                reply = self.command(req)
            except Exception as e:   # noqa: BLE001
                reply = {"ok": False, "error": str(e)}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                pass

    def command(self, req):
        op = req.get("op")
        if op == "stop":
            self.stop_asked = req.get("reason") or "wake stop"
            return {"ok": True}
        if op == "status":
            return {"ok": True, "tracees": len(self.tracees), "processes": len(self.keys), "counts": dict(self.counts)}
        if op == "keep":
            target = req.get("target")
            if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
                key = proc_key(int(target))
                if not key or not self.body.mine_process(key):
                    return {"ok": False, "error": f"process {target} is not one the agent started"}
                self.body.adopt_process(key)
                self.body.event("adopted", f"process {target} `{cmdline(int(target))}` is yours now")
                return {"ok": True, "adopted": f"process {target}"}
            t = identity(target)
            if not t or not self.body.mine_file(t.key):
                return {"ok": False, "error": f"{target} is not something the agent made"}
            self.body.adopt_file(t.key)
            self.body.event("adopted", f"{target} is yours now")
            return {"ok": True, "adopted": target}
        return {"ok": False, "error": f"unknown command {op!r}"}

    def note(self, ev, **kw):
        kw.update(ev=ev, t=round(time.time(), 3))
        try:
            self.log.write(json.dumps(kw, default=str) + "\n")
        except OSError:
            pass


def offline_stop(session):
    """Law 2 when the supervisor itself is gone: stop what is left and pull what it planted."""
    sup = Supervisor(session)
    sup.body = Body(session.store, session.id)
    sup.stopping = True
    sup.stop_reason = "wake stop, after the supervisor had gone"
    sup.stop_t0 = time.monotonic()
    for r in sup.body.alive():
        if proc_key(r["pid"]) == r["key"]:
            try:
                os.kill(r["pid"], signal.SIGKILL)
            except OSError:
                pass
    return sup.finish()
