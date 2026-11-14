"""Where a run lives: its body graph, its vault, its event log and its control socket."""
import json
import os
import secrets
import socket
import time


def wake_home():
    return os.environ.get("WAKE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state", "wake")


class Session:
    def __init__(self, home, sid):
        self.home = home
        self.id = sid
        self.dir = os.path.join(home, "sessions", sid)
        self.store = os.path.join(self.dir, "body")
        self.vault = os.path.join(self.dir, "vault")
        self.events = os.path.join(self.dir, "events.jsonl")
        self.meta_path = os.path.join(self.dir, "meta.json")
        self.control = os.path.join(self.dir, "control.sock")

    @classmethod
    def create(cls, cmd):
        home = wake_home()
        sid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        s = cls(home, sid)
        os.makedirs(s.dir, mode=0o700)
        s.update(id=sid, cmd=cmd, cwd=os.getcwd(), started=time.time(), status="starting")
        return s

    @classmethod
    def all(cls):
        base = os.path.join(wake_home(), "sessions")
        return sorted(os.listdir(base)) if os.path.isdir(base) else []

    @classmethod
    def find(cls, ref=None):
        ids = cls.all()
        if not ids:
            raise SystemExit("wake: there are no sessions yet. Start one with `wake run -- <agent>`.")
        ref = ref or os.environ.get("WAKE_SESSION")
        if not ref:
            return cls(wake_home(), ids[-1])
        hits = [i for i in ids if i == ref or i.startswith(ref) or i.endswith(ref)]
        if len(hits) != 1:
            raise SystemExit(f"wake: {'no' if not hits else 'more than one'} session matches {ref!r}")
        return cls(wake_home(), hits[0])

    def meta(self):
        try:
            with open(self.meta_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def update(self, **kw):
        m = self.meta()
        m.update(kw)
        tmp = self.meta_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(m, f, indent=2, default=str)
        os.replace(tmp, self.meta_path)

    def supervisor_alive(self):
        pid = self.meta().get("supervisor")
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return os.path.exists(self.control)

    def send(self, req, timeout=5):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(timeout)
            c.connect(self.control)
            c.sendall((json.dumps(req) + "\n").encode())
            return json.loads(c.makefile().readline() or "{}")
