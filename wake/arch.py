"""Machine facts: system call numbers, how arguments are laid out, how to cancel a call.

WAKE supports Linux on aarch64 and x86_64. Every number here is the kernel's own
(asm-generic unistd.h for aarch64, arch/x86/entry/syscalls/syscall_64.tbl for x86_64).
"""
import ctypes
import platform

AT_FDCWD = -100
AT_REMOVEDIR = 0x200
RENAME_NOREPLACE, RENAME_EXCHANGE = 1, 2
O_WRONLY, O_RDWR, O_CREAT, O_EXCL, O_TRUNC, O_APPEND = 0o1, 0o2, 0o100, 0o200, 0o1000, 0o2000
WRITE_FLAGS = O_WRONLY | O_RDWR | O_CREAT | O_TRUNC

# name: (aarch64, x86_64). None: the call does not exist on that machine.
CALLS = {
    # things that create, change or destroy files
    "open": (None, 2), "creat": (None, 85), "openat": (56, 257), "openat2": (437, 437),
    "unlink": (None, 87), "rmdir": (None, 84), "unlinkat": (35, 263),
    "rename": (None, 82), "renameat": (38, 264), "renameat2": (276, 316),
    "truncate": (45, 76),
    "mkdir": (None, 83), "mkdirat": (34, 258),
    "symlink": (None, 88), "symlinkat": (36, 266),
    # things that reach other processes
    "kill": (129, 62), "tkill": (130, 200), "tgkill": (131, 234), "pidfd_send_signal": (424, 424),
    "ptrace": (117, 101), "process_vm_writev": (271, 311),
    # traced only when the Docker adapter is on: a connect() to the daemon's socket is turned towards WAKE's
    "connect": (203, 42),
    # refused before any check. io_uring would let file operations bypass the checks;
    # callers fall back to ordinary calls when it reports "not supported".
    "io_uring_setup": (425, 425), "io_uring_enter": (426, 426), "io_uring_register": (427, 427),
    # refused before any check: they change the machine itself, not the agent's work
    "bpf": (280, 321), "open_by_handle_at": (265, 304), "mount": (40, 165), "umount2": (39, 166),
    "pivot_root": (41, 155), "kexec_load": (104, 246), "init_module": (105, 175),
    "finit_module": (273, 313), "delete_module": (106, 176), "reboot": (142, 169),
}

NT_PRSTATUS = 1
NT_ARM_SYSTEM_CALL = 0x404
PTRACE_GETREGSET, PTRACE_SETREGSET = 0x4204, 0x4205


class Iovec(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("len", ctypes.c_size_t)]


class Machine:
    """The running machine's numbers and register layout."""

    def __init__(self, name=None):
        name = name or platform.machine()
        if name in ("aarch64", "arm64"):
            self.name, self.audit, col = "aarch64", 0xC00000B7, 0
            self.nregs, self.ret_index = 34, 0          # x0..x30, sp, pc, pstate; return value in x0
        elif name in ("x86_64", "amd64"):
            self.name, self.audit, col = "x86_64", 0xC000003E, 1
            self.nregs, self.ret_index = 27, 10         # user_regs_struct; return value in rax
            self.orig_rax_index = 15
        else:
            raise SystemExit(f"wake: {name} is not supported (aarch64 and x86_64 are)")
        self.nr = {n: v[col] for n, v in CALLS.items() if v[col] is not None}
        self.name_of = {v: n for n, v in self.nr.items()}

    def _regs(self, ptrace, tid):
        regs = (ctypes.c_uint64 * self.nregs)()
        iov = Iovec(ctypes.addressof(regs), ctypes.sizeof(regs))
        ptrace(PTRACE_GETREGSET, tid, NT_PRSTATUS, ctypes.addressof(iov))
        return regs, iov

    def cancel(self, ptrace, tid):
        """Turn the call the thread is about to make into no call at all."""
        if self.name == "aarch64":
            v = ctypes.c_int(-1)
            iov = Iovec(ctypes.addressof(v), ctypes.sizeof(v))
            ptrace(PTRACE_SETREGSET, tid, NT_ARM_SYSTEM_CALL, ctypes.addressof(iov))
        else:
            regs, iov = self._regs(ptrace, tid)
            regs[self.orig_rax_index] = 0xFFFFFFFFFFFFFFFF
            ptrace(PTRACE_SETREGSET, tid, NT_PRSTATUS, ctypes.addressof(iov))

    def set_return(self, ptrace, tid, value):
        """Set what the call returns to the program (at the call's exit)."""
        regs, iov = self._regs(ptrace, tid)
        regs[self.ret_index] = value & 0xFFFFFFFFFFFFFFFF
        ptrace(PTRACE_SETREGSET, tid, NT_PRSTATUS, ctypes.addressof(iov))


def s32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v >= (1 << 31) else v


def s64(v):
    v &= 0xFFFFFFFFFFFFFFFF
    return v - (1 << 64) if v >= (1 << 63) else v
