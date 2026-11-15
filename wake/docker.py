"""The Docker adapter: the daemon acts for the agent, so the agent's requests to it pass through WAKE.

The Docker daemon runs as root, outside the agent's body. Whatever the agent asks of it, the daemon does,
and none of the agent's own system calls shows it. So WAKE stands between them. The agent is given a socket
of WAKE's (DOCKER_HOST), any connect() it makes to the daemon's own socket is turned towards WAKE's
(supervisor.h_connect), and every request is checked against the same body graph as files and processes.

Law 1. Containers, volumes, networks and images the agent creates are things it made. Only those can it
       remove, stop, kill, change or enter. A prune reaches only its own things. A container it creates sees
       your folders and volumes read-only, and cannot be privileged, share the machine's processes, reach raw
       devices or reach the daemon itself.
Law 2. When you stop the agent, its containers stop with it, and their restart policies are switched off so
       the daemon does not bring them back.
"""
import http.client
import json
import os
import queue
import re
import secrets
import select
import socket
import stat
import struct
import threading
from urllib.parse import parse_qs, quote, urlsplit

from .ident import identity

VERSION = re.compile(r"^/v\d+(\.\d+)?(?=/)")
GUARDED = {"kill", "stop", "restart", "pause", "update", "rename", "exec", "attach"}
SWARM = {"services", "secrets", "configs", "nodes", "swarm", "plugins", "tasks"}
CAPS = {"ALL", "SYS_ADMIN", "SYS_MODULE", "SYS_RAWIO", "SYS_BOOT", "SYS_PTRACE"}
CAPTURE = 8 << 20
SO_PEERCRED = 17
REASONS = {200: "OK", 201: "Created", 204: "No Content", 403: "Forbidden", 404: "Not Found", 409: "Conflict"}


def is_sock(path):
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def daemon_socket(env):
    """The Docker daemon's socket for this person, or None (a TCP daemon or no daemon: the adapter stays off)."""
    host = env.get("DOCKER_HOST", "")
    if host:
        path = host[len("unix://"):] if host.startswith("unix://") else None
    else:
        runtime = env.get("XDG_RUNTIME_DIR")
        path = next((p for p in ("/var/run/docker.sock", runtime and os.path.join(runtime, "docker.sock"))
                     if p and is_sock(p)), None)
    return path if path and is_sock(path) else None


def hget(headers, name):
    name = name.lower()
    for k, v in headers:
        if k.lower() == name:
            return v
    return ""


def hset(headers, name, value):
    out = [(k, v) for k, v in headers if k.lower() not in (name.lower(), "transfer-encoding")]
    out.append((name, value))
    return out


def head_bytes(start, headers):
    return (start + "\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers) + "\r\n").encode("latin-1")


def unchunk(raw, headers):
    if "chunked" not in hget(headers, "transfer-encoding").lower():
        return raw
    out, i = bytearray(), 0
    while True:
        j = raw.index(b"\r\n", i)
        size = int(raw[i:j].split(b";")[0].strip() or b"0", 16)
        i = j + 2
        if size == 0:
            return bytes(out)
        out += raw[i:i + size]
        i += size + 2


class Stream:
    """HTTP/1.1 framing over a socket: heads, then bodies passed on exactly as they arrive."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def _fill(self):
        d = self.sock.recv(65536)
        if not d:
            raise EOFError
        self.buf += d

    def head(self):
        while True:
            i = self.buf.find(b"\r\n\r\n")
            if i >= 0:
                break
            if len(self.buf) > 1 << 20:
                raise ValueError("header too large")
            self._fill()
        raw = bytes(self.buf[:i])
        del self.buf[:i + 4]
        lines = raw.decode("latin-1").split("\r\n")
        headers = []
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers.append((k.strip(), v.strip()))
        return lines[0], headers

    def line(self):
        while True:
            i = self.buf.find(b"\r\n")
            if i >= 0:
                out = bytes(self.buf[:i + 2])
                del self.buf[:i + 2]
                return out
            self._fill()

    def exact(self, n):
        while n > 0:
            if not self.buf:
                self._fill()
            piece = bytes(self.buf[:n])
            del self.buf[:len(piece)]
            n -= len(piece)
            yield piece

    def body(self, headers, until_close=False):
        if "chunked" in hget(headers, "transfer-encoding").lower():
            while True:
                line = self.line()
                yield line
                size = int(line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while True:
                        t = self.line()
                        yield t
                        if t == b"\r\n":
                            return
                yield from self.exact(size + 2)
        elif hget(headers, "content-length"):
            yield from self.exact(int(hget(headers, "content-length")))
        elif until_close:
            if self.buf:
                yield self.take()
            while True:
                d = self.sock.recv(65536)
                if not d:
                    return
                yield d

    def take(self):
        out = bytes(self.buf)
        self.buf.clear()
        return out


def relay(a, b):
    """Both directions, raw, until both sides have closed (an attached container or exec)."""
    open_ = {a: b, b: a}
    while open_:
        ready, _, _ = select.select(list(open_), [], [], 300)
        if not ready:
            return
        for s in ready:
            try:
                d = s.recv(65536)
            except OSError:
                d = b""
            if d:
                open_[s].sendall(d)
            else:
                try:
                    open_[s].shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                del open_[s]


class MainThread:
    """The body graph, asked from the adapter's threads. A Relate connection belongs to the thread that opened
    it, so every question and record goes to the supervisor's own thread, which answers between stops."""

    def __init__(self, adapter):
        self.a = adapter

    def __getattr__(self, name):
        return lambda *args: self.a.call(lambda: getattr(self.a.sup.body, name)(*args))


class Refused(Exception):
    def __init__(self, msg, drained=False):
        super().__init__(msg)
        self.drained = drained


class Adapter:
    def __init__(self, sup, upstream, sock_path):
        self.sup = sup
        self.upstream = upstream
        self.upstream_real = os.path.realpath(upstream)
        self.sock_path = sock_path
        self.short = None
        self.server = None
        self.thread = None
        self.closing = False
        self.counts = {"requests": 0, "refused": 0, "readonly": 0}
        self.body = MainThread(self)
        self.home_thread = threading.current_thread()
        self.calls = queue.Queue()
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        os.set_blocking(self.wake_w, False)

    # ---------------------------------------------------------------- the supervisor's thread does graph work
    def call(self, fn):
        if threading.current_thread() is self.home_thread:
            return fn()
        box = {"done": threading.Event()}
        self.calls.put((fn, box))
        try:
            os.write(self.wake_w, b"x")
        except BlockingIOError:
            pass
        if not box["done"].wait(15):
            raise OSError("WAKE's supervisor did not answer")
        if "error" in box:
            raise box["error"]
        return box["result"]

    def run_calls(self):
        """Called by the supervisor's loop: answer every waiting question from the adapter's threads."""
        try:
            while os.read(self.wake_r, 512):
                pass
        except (BlockingIOError, OSError):
            pass
        while True:
            try:
                fn, box = self.calls.get_nowait()
            except queue.Empty:
                return
            try:
                box["result"] = fn()
            except BaseException as e:   # noqa: BLE001 - handed back to the thread that asked
                box["error"] = e
            box["done"].set()

    # ---------------------------------------------------------------- setting up
    @classmethod
    def prepare(cls, sup, env):
        upstream = daemon_socket(env)
        if not upstream:
            return None
        a = cls(sup, upstream, os.path.join(sup.s.dir, "docker.sock"))
        a.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        a.server.bind(a.sock_path)
        os.chmod(a.sock_path, 0o600)
        a.server.listen(64)
        for _ in range(20):                       # a short name for it: connect() redirection rewrites the
            short = f"/tmp/.wk-{secrets.token_hex(2)}"   # daemon's path in place, so ours must not be longer
            try:
                os.symlink(a.sock_path, short)
                a.short = short
                break
            except FileExistsError:
                continue
            except OSError:
                break
        return a

    def start(self):
        self.thread = threading.Thread(target=self.accept_loop, name="wake-docker", daemon=True)
        self.thread.start()

    def close(self):
        self.closing = True
        for p in (self.short, self.sock_path):
            try:
                if p:
                    os.unlink(p)
            except OSError:
                pass
        try:
            self.server.close()
        except (OSError, AttributeError):
            pass

    def accept_loop(self):
        while not self.closing:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    # ---------------------------------------------------------------- the daemon, for WAKE's own questions
    def api(self, method, path, body=None):
        c = http.client.HTTPConnection("docker", timeout=60)
        c.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.sock.settimeout(60)
        c.sock.connect(self.upstream)
        try:
            data = json.dumps(body).encode() if body is not None else None
            c.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
            r = c.getresponse()
            raw = r.read()
            try:
                return r.status, json.loads(raw) if raw else None
            except ValueError:
                return r.status, None
        finally:
            c.close()

    def container(self, ref):
        st, info = self.api("GET", f"/containers/{quote(ref, safe='')}/json")
        return info if st == 200 and info else None

    def volumes(self):
        st, info = self.api("GET", "/volumes")
        return {v["Name"]: v.get("CreatedAt", "") for v in (info or {}).get("Volumes") or []} if st == 200 else {}

    def volume_key(self, name):
        st, info = self.api("GET", f"/volumes/{quote(name, safe='')}")
        return f"volume:{name}@{info.get('CreatedAt', '')}" if st == 200 and info else None

    def image_id(self, ref):
        st, info = self.api("GET", f"/images/{quote(ref, safe='')}/json")
        return info.get("Id") if st == 200 and info else None

    def network_id(self, ref):
        st, info = self.api("GET", f"/networks/{quote(ref, safe='')}")
        return (info.get("Id"), info.get("Name")) if st == 200 and info else (None, None)

    # ---------------------------------------------------------------- one connection from the agent
    def who(self, conn):
        try:
            pid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, SO_PEERCRED, 12))[0]
        except OSError:
            pid = 0
        if pid in self.sup.adopted:
            return pid, None                           # yours now: passed on unchecked
        return pid, self.sup.keys.get(pid) or self.sup.keys.get(self.sup.root)

    def serve(self, client):
        try:
            up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            up.connect(self.upstream)
        except OSError:
            client.close()
            return
        pid, me = self.who(client)
        c, u = Stream(client), Stream(up)
        try:
            while not self.closing:
                try:
                    start, headers = c.head()
                except (EOFError, ValueError):
                    return
                if self.exchange(client, up, c, u, start, headers, me):
                    return
        except (OSError, EOFError, ValueError):
            pass
        except Exception as e:   # noqa: BLE001 - one bad request must not take the adapter down
            self.call(lambda: self.sup.note("error", docker=repr(e)))
        finally:
            for s in (client, up):
                try:
                    s.close()
                except OSError:
                    pass

    def exchange(self, client, up, c, u, start, headers, me):
        """One request and its answer. Returns True when the connection has been handed over raw."""
        method, target, _ = (start.split(" ", 2) + ["", ""])[:3]
        parts = urlsplit(target)
        path = VERSION.sub("", parts.path)
        query = parse_qs(parts.query)
        self.counts["requests"] += 1
        if me is None or method in ("GET", "HEAD"):
            return self.forward(client, up, c, u, start, headers, method)[3]
        try:
            if self.sup.stopping:
                raise Refused("the agent has been stopped")
            return self.decide(client, up, c, u, start, headers, method, path, query, me)
        except Refused as r:
            if not r.drained:
                for _ in c.body(headers):
                    pass
            self.counts["refused"] += 1
            self.reply(client, 403, {"message": f"wake: {r}"})
            return False

    def decide(self, client, up, c, u, start, headers, method, path, query, me):
        seg = [p for p in path.split("/") if p]
        top = seg[0] if seg else ""
        if top in SWARM:
            raise Refused(f"WAKE does not let an agent change {top}; ask the person")
        if top == "containers" and len(seg) >= 2:
            if seg[1] == "create" and method == "POST":
                return self.create_container(client, up, c, u, start, headers, query, me)
            if seg[1] == "prune":
                return self.prune(client, c, headers, me, "container")
            ref, op = seg[1], (seg[2] if len(seg) > 2 else "")
            if method == "DELETE" or op in GUARDED or (method == "PUT" and op == "archive"):
                info = self.container(ref)
                if info:
                    key = f"container:{info['Id']}"
                    name = (info.get("Name") or ref).lstrip("/")
                    self.guard(me, key, "container", name, "remove" if method == "DELETE" else op or method)
                    done = self.forward(client, up, c, u, start, headers, method)
                    if method == "DELETE" and 200 <= done[0] < 300:
                        self.body.docker_gone(key)
                    return done[3]
        elif top == "volumes" and len(seg) >= 2:
            if seg[1] == "create" and method == "POST":
                return self.create_volume(client, up, c, u, start, headers, me)
            if seg[1] == "prune":
                return self.prune(client, c, headers, me, "volume")
            if method == "DELETE":
                key = self.volume_key(seg[1])
                if key:
                    self.guard(me, key, "volume", seg[1], "remove")
                    done = self.forward(client, up, c, u, start, headers, method)
                    if 200 <= done[0] < 300:
                        self.body.docker_gone(key)
                    return done[3]
        elif top == "networks" and len(seg) >= 2:
            if seg[1] == "create" and method == "POST":
                return self.record_created(client, up, c, u, start, headers, me, "network")
            if seg[1] == "prune":
                return self.prune(client, c, headers, me, "network")
            if method == "DELETE" or (len(seg) > 2 and seg[2] == "disconnect"):
                nid, name = self.network_id(seg[1])
                if nid:
                    key = f"network:{nid}"
                    self.guard(me, key, "network", name, "remove" if method == "DELETE" else "disconnect")
                    done = self.forward(client, up, c, u, start, headers, method)
                    if method == "DELETE" and 200 <= done[0] < 300:
                        self.body.docker_gone(key)
                    return done[3]
        elif top == "images":
            if len(seg) >= 2 and seg[1] == "create" and method == "POST":
                return self.pull(client, up, c, u, start, headers, query, me)
            if len(seg) >= 2 and seg[1] == "prune":
                return self.prune(client, c, headers, me, "image", query)
            if method == "DELETE" and len(seg) >= 2:
                ref = "/".join(seg[1:])
                iid = self.image_id(ref)
                if iid:
                    key = f"image:{iid}"
                    self.guard(me, key, "image", ref, "remove")
                    done = self.forward(client, up, c, u, start, headers, method)
                    if 200 <= done[0] < 300 and not self.image_id(iid):
                        self.body.docker_gone(key)
                    return done[3]
        elif top == "build":
            if len(seg) >= 2 and seg[1] == "prune":
                return self.prune(client, c, headers, me, "build")
            return self.build(client, up, c, u, start, headers, query, me)
        elif top == "commit" and method == "POST":
            return self.record_created(client, up, c, u, start, headers, me, "image")
        return self.forward(client, up, c, u, start, headers, method)[3]

    def guard(self, me, key, kind, name, call):
        if self.body.mine_docker(key):
            return
        detail = f"{call} {kind} {name}"

        def record():
            self.sup.body.tried_docker(me, call, key, kind, name, detail)
            self.sup.counts["refused"] += 1
            self.sup.note("refused", call=f"docker {call}", target=f"{kind} {name}", by=me)
        self.call(record)
        raise Refused(f"the {kind} {name} is yours, not the agent's: an agent can only destroy what it made")

    def made(self, me, kind, key, oid, name, detail=""):
        def record():
            self.sup.body.made_docker(me, kind, key, oid, name, detail)
            self.sup.body.event("docker", f"the agent made {kind} {name}", me)
            self.sup.note("docker-made", kind=kind, name=name, id=oid, by=me)
        self.call(record)

    # ---------------------------------------------------------------- passing a request on
    def forward(self, client, up, c, u, start, headers, method, body=None, capture=False, hold=False):
        """Send the request upstream and its answer back. Returns (status, headers, captured body, raw)."""
        if body is not None:
            up.sendall(head_bytes(start, hset(headers, "Content-Length", str(len(body)))) + body)
        else:
            up.sendall(head_bytes(start, headers))
            for piece in c.body(headers):
                up.sendall(piece)
        rstart, rh = u.head()
        status = int(rstart.split(" ")[1])
        framed = hget(rh, "content-length") or "chunked" in hget(rh, "transfer-encoding").lower()
        if hold:                                   # the caller changes the answer before the agent sees it
            raw = b"".join(u.body(rh, until_close=not framed))
            return status, rh, unchunk(raw, rh), rstart
        client.sendall(head_bytes(rstart, rh))
        if status == 101 or (not framed and status == 200 and hget(rh, "content-type").startswith("application/vnd.docker")):
            client.sendall(u.take())
            up.sendall(c.take())
            relay(client, up)
            return status, rh, None, True
        if method == "HEAD" or status in (204, 304) or status < 200:
            return status, rh, None, False
        data = bytearray() if capture else None
        for piece in u.body(rh, until_close=not framed):
            client.sendall(piece)
            if data is not None and len(data) < CAPTURE:
                data += piece
        return status, rh, (unchunk(bytes(data), rh) if data is not None else None), not framed

    def reply(self, client, status, obj, headers=None):
        body = json.dumps(obj).encode()
        hs = [(k, v) for k, v in (headers or []) if k.lower() not in ("content-length", "transfer-encoding")]
        if not any(k.lower() == "content-type" for k, _ in hs):
            hs.append(("Content-Type", "application/json"))
        hs.append(("Content-Length", str(len(body))))
        client.sendall(head_bytes(f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}", hs) + body)

    def read_json(self, c, headers):
        raw = unchunk(b"".join(c.body(headers)), headers)
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return None

    # ---------------------------------------------------------------- Law 1 at creation
    def create_container(self, client, up, c, u, start, headers, query, me):
        cfg = self.read_json(c, headers)
        if cfg is None:
            raise Refused("WAKE could not read this container's settings", drained=True)
        try:
            return self._create_container(client, up, c, u, start, headers, query, me, cfg)
        except Refused as r:
            r.drained = True
            raise

    def _create_container(self, client, up, c, u, start, headers, query, me, cfg):
        hc = cfg.setdefault("HostConfig", {}) or {}
        cfg["HostConfig"] = hc
        if hc.get("Privileged"):
            raise Refused("a privileged container could reach anything on this machine")
        for mode in ("PidMode", "IpcMode", "UsernsMode", "CgroupnsMode"):
            if str(hc.get(mode) or "").lower() == "host":
                raise Refused(f"{mode} host would let the container reach the machine's own processes")
        caps = {str(x).upper().replace("CAP_", "") for x in hc.get("CapAdd") or []}
        if caps & CAPS:
            raise Refused(f"the capability {sorted(caps & CAPS)[0]} would let the container reach the machine")
        if hc.get("Devices") or hc.get("DeviceCgroupRules"):
            raise Refused("raw devices would let the container write past every check")
        notes = []
        binds = []
        for b in hc.get("Binds") or []:
            binds.append(self.bind(b, notes))
        if binds:
            hc["Binds"] = binds
        for m in hc.get("Mounts") or []:
            self.mount(m, notes)
        vf = []
        for ref in hc.get("VolumesFrom") or []:
            name, _, mode = ref.partition(":")
            info = self.container(name)
            if info and not self.body.mine_docker(f"container:{info['Id']}") and mode != "ro":
                notes.append(f"the volumes of your container {name} are read-only to it")
                ref = f"{name}:ro"
            vf.append(ref)
        if vf:
            hc["VolumesFrom"] = vf
        before = self.volumes()
        status, rh, data, rstart = self.forward(client, up, c, u, start, headers, "POST",
                                                body=json.dumps(cfg).encode(), hold=True)
        try:
            answer = json.loads(data) if data else {}
        except ValueError:
            answer = None
        if 200 <= status < 300 and isinstance(answer, dict) and answer.get("Id"):
            name = (query.get("name") or [""])[0] or answer["Id"][:12]
            self.made(me, "container", f"container:{answer['Id']}", answer["Id"], name, str(cfg.get("Image", "")))
            for vname, created in self.volumes().items():
                if vname not in before:
                    self.made(me, "volume", f"volume:{vname}@{created}", vname, vname, f"made with {name}")
            if notes:
                self.counts["readonly"] += len(notes)
                answer["Warnings"] = list(answer.get("Warnings") or []) + [f"wake: {n}" for n in notes]
                for n in notes:
                    self.body.event("docker", n, me)
        if isinstance(answer, dict):
            self.reply(client, status, answer, rh)
        else:
            client.sendall(head_bytes(rstart, hset(rh, "Content-Length", str(len(data or b"")))) + (data or b""))
        return False

    def bind(self, spec, notes):
        parts = spec.split(":")
        if len(parts) < 2:
            return spec
        src, dst, opts = parts[0], parts[1], (parts[2].split(",") if len(parts) > 2 else [])
        if src.startswith("/"):
            real = os.path.realpath(src)
            if real in (self.upstream_real, os.path.dirname(self.upstream_real)):
                raise Refused("a container holding the Docker daemon's socket could do anything WAKE refuses")
            if not os.path.exists(src):
                return spec                                  # the daemon makes it: nothing of yours is there yet
            t = identity(src)
            if t and self.body.mine_file(t.key):
                return spec
            what = f"your folder {src}"
        else:
            key = self.volume_key(src)
            if key is None or self.body.mine_docker(key):
                return spec                                  # new (it becomes the agent's) or the agent's own
            what = f"your volume {src}"
        if "ro" in opts:
            return spec
        notes.append(f"{what} is read-only to this container")
        opts = [o for o in opts if o != "rw"] + ["ro"]
        return f"{src}:{dst}:{','.join(opts)}"

    def mount(self, m, notes):
        kind, src = m.get("Type"), m.get("Source") or ""
        if kind == "bind" and src:
            real = os.path.realpath(src)
            if real == self.upstream_real or real == os.path.dirname(self.upstream_real):
                raise Refused("a container holding the Docker daemon's socket could do anything WAKE refuses")
            if not os.path.exists(src) or m.get("ReadOnly"):
                return
            t = identity(src)
            if t and self.body.mine_file(t.key):
                return
            m["ReadOnly"] = True
            notes.append(f"your folder {src} is read-only to this container")
        elif kind == "volume" and src and not m.get("ReadOnly"):
            key = self.volume_key(src)
            if key and not self.body.mine_docker(key):
                m["ReadOnly"] = True
                notes.append(f"your volume {src} is read-only to this container")

    def create_volume(self, client, up, c, u, start, headers, me):
        cfg = self.read_json(c, headers) or {}
        name = cfg.get("Name") or ""
        existed = bool(name) and self.volume_key(name) is not None
        status, rh, data, _ = self.forward(client, up, c, u, start, headers, "POST",
                                           body=json.dumps(cfg).encode(), capture=True)
        try:
            v = json.loads(data) if data else {}
        except ValueError:
            v = {}
        if 200 <= status < 300 and v.get("Name") and not existed:
            self.made(me, "volume", f"volume:{v['Name']}@{v.get('CreatedAt', '')}", v["Name"], v["Name"])
        return False

    def record_created(self, client, up, c, u, start, headers, me, kind):
        status, rh, data, raw = self.forward(client, up, c, u, start, headers, "POST", capture=True)
        try:
            v = json.loads(data) if data else {}
        except ValueError:
            v = {}
        oid = v.get("Id") or v.get("ID")
        if 200 <= status < 300 and oid:
            name = oid[:19] if kind == "image" else (self.network_id(oid)[1] or oid[:12])
            self.made(me, kind, f"{kind}:{oid}", oid, name)
        return raw

    def pull(self, client, up, c, u, start, headers, query, me):
        ref = (query.get("fromImage") or [""])[0]
        tag = (query.get("tag") or [""])[0]
        full = f"{ref}:{tag}" if tag and "@" not in ref and ":" not in ref.rsplit("/", 1)[-1] else ref
        had = self.image_id(full) if full else None
        raw = self.forward(client, up, c, u, start, headers, "POST")[3]
        if full and not had:
            iid = self.image_id(full)
            if iid:
                self.made(me, "image", f"image:{iid}", iid, full)
        return raw

    def build(self, client, up, c, u, start, headers, query, me):
        status, rh, data, raw = self.forward(client, up, c, u, start, headers, "POST", capture=True)
        iid = None
        for line in (data or b"").splitlines():
            try:
                aux = json.loads(line).get("aux") or {}
            except (ValueError, AttributeError):
                continue
            if isinstance(aux, dict) and str(aux.get("ID", "")).startswith("sha256:"):
                iid = aux["ID"]
        if 200 <= status < 300 and iid and not self.body.mine_docker(f"image:{iid}"):
            tags = query.get("t") or []
            self.made(me, "image", f"image:{iid}", iid, tags[0] if tags else iid[:19])
        return raw

    # ---------------------------------------------------------------- prunes reach only the agent's things
    def prune(self, client, c, headers, me, kind, query=None):
        for _ in c.body(headers):
            pass
        gone, spared = [], 0
        if kind == "container":
            stopped_filter = quote(json.dumps({"status": ["created", "exited", "dead"]}))
            st, everyone = self.api("GET", f"/containers/json?all=1&filters={stopped_filter}")
            for r in self.body.docker_live("container"):
                info = self.container(r["oid"])
                if info and not (info.get("State") or {}).get("Running"):
                    if self.api("DELETE", f"/containers/{r['oid']}")[0] < 300:
                        self.body.docker_gone(r["key"])
                        gone.append(r["oid"])
            spared = max(0, len(everyone or []) - len(gone))
            answer = {"ContainersDeleted": gone, "SpaceReclaimed": 0}
        elif kind == "volume":
            for r in self.body.docker_live("volume"):
                if self.api("DELETE", f"/volumes/{quote(r['oid'], safe='')}")[0] < 300:
                    self.body.docker_gone(r["key"])
                    gone.append(r["oid"])
            spared = max(0, len(self.volumes()))
            answer = {"VolumesDeleted": gone, "SpaceReclaimed": 0}
        elif kind == "network":
            for r in self.body.docker_live("network"):
                if self.api("DELETE", f"/networks/{r['oid']}")[0] < 300:
                    self.body.docker_gone(r["key"])
                    gone.append(r["name"])
            answer = {"NetworksDeleted": gone}
        elif kind == "image":
            for r in self.body.docker_live("image"):
                if self.api("DELETE", f"/images/{r['oid']}")[0] < 300:
                    self.body.docker_gone(r["key"])
                    gone.append({"Deleted": r["oid"]})
            answer = {"ImagesDeleted": gone, "SpaceReclaimed": 0}
        else:
            answer = {"CachesDeleted": [], "SpaceReclaimed": 0}
        self.body.event("docker", f"{kind} prune: removed {len(gone)} of the agent's; "
                                      f"yours were left alone{f' ({spared})' if spared else ''}", me)
        self.call(lambda: self.sup.note("docker-prune", kind=kind, removed=len(gone), spared=spared, by=me))
        self.reply(client, 200, answer)
        return False

    # ---------------------------------------------------------------- Law 2
    def stop_all(self):
        """Every container the agent made stops, and will not be started again by the daemon."""
        stopped = []
        for r in self.body.docker_live("container"):
            try:
                info = self.container(r["oid"])
                if not info:
                    continue
                self.api("POST", f"/containers/{r['oid']}/update", {"RestartPolicy": {"Name": "no"}})
                if (info.get("State") or {}).get("Running"):
                    if self.api("POST", f"/containers/{r['oid']}/kill")[0] < 300:
                        stopped.append(r["name"])
                        self.body.event("docker", f"stopped container {r['name']}, which the agent made")
            except OSError:
                continue
        return stopped
