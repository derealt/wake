"""The kernel filter, checked without a kernel: a small classic-BPF interpreter runs the program
WAKE installs and we ask it what it would do with each call, on both machines."""
import struct
import unittest

from wake.arch import O_APPEND, O_CREAT, O_RDWR, O_TRUNC, O_WRONLY, Machine
from wake.seccomp import ENOSYS, EPERM, RET_ALLOW, RET_ERRNO, RET_TRACE, program


def run_bpf(prog, nr, arch, args=()):
    args = (tuple(args) + (0,) * 6)[:6]
    data = struct.pack("<iIQ6Q", nr, arch, 0, *args)
    a, pc = 0, 0
    while True:
        code, jt, jf, k = prog[pc]
        if code == 0x20:                       # ld [k]
            a = struct.unpack_from("<I", data, k)[0]
            pc += 1
        elif code == 0x15:                     # jeq
            pc += 1 + (jt if a == k else jf)
        elif code == 0x35:                     # jge
            pc += 1 + (jt if a >= k else jf)
        elif code == 0x45:                     # jset
            pc += 1 + (jt if a & k else jf)
        elif code == 0x06:                     # ret
            return k
        else:
            raise AssertionError(f"unknown instruction {code:#x}")


class FilterTest(unittest.TestCase):
    def check(self, mname):
        m = Machine(mname)
        prog = program(m)
        nr = m.nr

        def verdict(name, args=(0, 0, 0, 0, 0, 0), arch=m.audit):
            return run_bpf(prog, nr[name], arch, args)

        # reads go straight through; opening to write, create or truncate stops the agent
        self.assertEqual(verdict("openat", (0, 0, 0)), RET_ALLOW)
        for flags in (O_WRONLY, O_RDWR, O_CREAT | O_WRONLY, O_TRUNC, O_WRONLY | O_APPEND):
            self.assertEqual(verdict("openat", (0, 0, flags)), RET_TRACE, oct(flags))
        for name in ("unlinkat", "renameat2", "truncate", "mkdirat", "kill", "tgkill", "ptrace", "pidfd_send_signal"):
            self.assertEqual(verdict(name), RET_TRACE, name)
        for name in ("io_uring_setup", "io_uring_enter"):
            self.assertEqual(verdict(name), RET_ERRNO | ENOSYS, name)
        for name in ("mount", "bpf", "reboot", "init_module"):
            self.assertEqual(verdict(name), RET_ERRNO | EPERM, name)
        # anything else (here: getpid) is allowed
        getpid = 172 if mname == "aarch64" else 39
        self.assertEqual(run_bpf(prog, getpid, m.audit), RET_ALLOW)
        # another ABI on the same machine is refused, so it cannot slip past the checks
        self.assertEqual(run_bpf(prog, nr["unlinkat"], 0x40000003), RET_ERRNO | ENOSYS)
        if mname == "x86_64":
            self.assertEqual(verdict("open", (0, 0)), RET_ALLOW)
            self.assertEqual(verdict("open", (0, O_WRONLY | O_CREAT)), RET_TRACE)
            self.assertEqual(verdict("unlink"), RET_TRACE)
            self.assertEqual(run_bpf(prog, 0x40000000 + 87, m.audit), RET_ERRNO | ENOSYS)   # x32 unlink

    def test_aarch64(self):
        self.check("aarch64")

    def test_x86_64(self):
        self.check("x86_64")


if __name__ == "__main__":
    unittest.main()
