"""The kernel-side filter: only calls that could destroy something stop the agent.

Everything else (reads, writes to files already open, network, memory) runs at full speed.
An open() stops the agent only when its flags ask for writing, creating or truncating.
"""
import ctypes

from .arch import WRITE_FLAGS, Machine

LD_W_ABS, JEQ, JGE, JSET, RET = 0x20, 0x15, 0x35, 0x45, 0x06
RET_ALLOW, RET_TRACE, RET_ERRNO = 0x7FFF0000, 0x7FF00000, 0x00050000
ENOSYS, EPERM = 38, 1
X32_BIT = 0x40000000

TRACE_ALWAYS = ["creat", "openat2", "unlink", "rmdir", "unlinkat", "rename", "renameat", "renameat2",
                "truncate", "mkdir", "mkdirat", "symlink", "symlinkat",
                "kill", "tkill", "tgkill", "pidfd_send_signal", "ptrace", "process_vm_writev"]
TRACE_IF_WRITE = {"open": 1, "openat": 2}          # call -> which argument holds the flags
REFUSE_ENOSYS = ["io_uring_setup", "io_uring_enter", "io_uring_register"]
REFUSE_EPERM = ["bpf", "open_by_handle_at", "mount", "umount2", "pivot_root", "kexec_load",
                "init_module", "finit_module", "delete_module", "reboot"]


class SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]


class SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(SockFilter))]


def program(m: Machine):
    """Return the filter as a list of (code, jt, jf, k)."""
    ins, labels = [], {}

    def op(code, k=0, jt=None, jf=None):
        ins.append([code, jt, jf, k])

    def label(name):
        labels[name] = len(ins)

    op(LD_W_ABS, 4)                                   # seccomp_data.arch
    op(JEQ, m.audit, jf="enosys")                     # another ABI on this machine: refuse
    op(LD_W_ABS, 0)                                   # seccomp_data.nr
    if m.name == "x86_64":
        op(JGE, X32_BIT, jt="enosys")                 # the x32 ABI: refuse
    for n in REFUSE_ENOSYS:
        if n in m.nr:
            op(JEQ, m.nr[n], jt="enosys")
    for n in REFUSE_EPERM:
        if n in m.nr:
            op(JEQ, m.nr[n], jt="eperm")
    for n in TRACE_ALWAYS:
        if n in m.nr:
            op(JEQ, m.nr[n], jt="trace")
    for n, arg in TRACE_IF_WRITE.items():
        if n in m.nr:
            op(JEQ, m.nr[n], jt=f"flags{arg}")
    op(RET, RET_ALLOW)
    for arg in sorted(set(TRACE_IF_WRITE.values())):
        label(f"flags{arg}")
        op(LD_W_ABS, 16 + 8 * arg)                    # low 32 bits of args[arg]
        op(JSET, WRITE_FLAGS, jt="trace", jf="allow")
    label("trace")
    op(RET, RET_TRACE)
    label("enosys")
    op(RET, RET_ERRNO | ENOSYS)
    label("eperm")
    op(RET, RET_ERRNO | EPERM)
    label("allow")
    op(RET, RET_ALLOW)

    out = []
    for i, (code, jt, jf, k) in enumerate(ins):
        jt = 0 if jt is None else labels[jt] - (i + 1)
        jf = 0 if jf is None else labels[jf] - (i + 1)
        assert 0 <= jt < 256 and 0 <= jf < 256, "filter jump out of range"
        out.append((code, jt, jf, k))
    return out


def install(m: Machine):
    """Install the filter on the calling process. It is inherited by every child and kept across exec."""
    ins = program(m)
    arr = (SockFilter * len(ins))(*[SockFilter(*x) for x in ins])
    prog = SockFprog(len(ins), arr)
    libc = ctypes.CDLL(None, use_errno=True)
    z = ctypes.c_ulong(0)
    if libc.prctl(38, ctypes.c_ulong(1), z, z, z) != 0:                       # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS)")
    if libc.prctl(22, ctypes.c_ulong(2), ctypes.byref(prog), z, z) != 0:      # PR_SET_SECCOMP, FILTER
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_SECCOMP)")
