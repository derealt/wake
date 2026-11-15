"""A small stand-in for the Docker daemon: the parts of the Engine API the adapter speaks, held in memory.

The tests run it in their own process (the person's side) and point WAKE at it with DOCKER_HOST, so the
Docker adapter is tested without a real daemon. trials/docker.py runs the same checks on a real one.
"""
import http.server
import json
import re
import secrets
import socket
import socketserver
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

VERSION = re.compile(r"^/v\d+(\.\d+)?(?=/)")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "fake", 0


class FakeDocker:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.containers, self.volumes, self.networks, self.images = {}, {}, {}, {}
        self.log = []
        handler = type("H", (Handler,), {"fake": self})
        self.server = Server(path, handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    # what the person has before the agent starts
    def person_container(self, name, running=True, image="postgres:16"):
        cid = secrets.token_hex(32)
        self.containers[cid] = {"Id": cid, "Name": "/" + name, "Image": image, "State": {"Running": running},
                                "HostConfig": {"RestartPolicy": {"Name": "always"}}}
        return cid

    def person_volume(self, name):
        self.volumes[name] = {"Name": name, "CreatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "Driver": "local"}

    def find(self, ref):
        if ref in self.containers:
            return self.containers[ref]
        for c in self.containers.values():
            if c["Name"] == "/" + ref or c["Id"].startswith(ref):
                return c
        return None

    def by_name(self, name):
        return self.find(name)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    fake = None

    def log_message(self, *a):
        pass

    def send(self, status, obj=None):
        body = b"" if obj is None else (obj if isinstance(obj, bytes) else json.dumps(obj).encode())
        self.send_response(status)
        if obj is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def route(self, method):
        f = self.fake
        parts = urlsplit(self.path)
        path = VERSION.sub("", parts.path)
        q = parse_qs(parts.query)
        seg = [unquote(s) for s in path.split("/") if s]
        data = self.body() if method in ("POST", "PUT") else {}
        with f.lock:
            f.log.append((method, path, q, data))
            if path == "/_ping":
                return self.send(200, b"OK")
            if path == "/version":
                return self.send(200, {"ApiVersion": "1.47", "Version": "fake"})
            if seg[:1] == ["containers"]:
                if seg[1:2] == ["create"]:
                    cid = secrets.token_hex(32)
                    name = (q.get("name") or [cid[:12]])[0]
                    hc = data.get("HostConfig") or {}
                    for b in hc.get("Binds") or []:
                        src = b.split(":")[0]
                        if not src.startswith("/") and src not in f.volumes:
                            f.person_volume(src)
                    f.containers[cid] = {"Id": cid, "Name": "/" + name, "Image": data.get("Image"),
                                         "State": {"Running": False}, "HostConfig": hc, "Config": data}
                    return self.send(201, {"Id": cid, "Warnings": []})
                if seg[1:2] == ["json"]:
                    out = [{"Id": c["Id"], "Names": [c["Name"]], "State": "running" if c["State"]["Running"] else "exited"}
                           for c in f.containers.values()
                           if (q.get("all") or ["0"])[0] in ("1", "true") or c["State"]["Running"]]
                    if "filters" in q and "exited" in q["filters"][0]:
                        out = [c for c in out if c["State"] != "running"]
                    return self.send(200, out)
                c = f.find(seg[1]) if len(seg) > 1 else None
                if not c:
                    return self.send(404, {"message": f"No such container: {seg[1] if len(seg) > 1 else ''}"})
                op = seg[2] if len(seg) > 2 else ""
                if method == "GET" and op == "json":
                    return self.send(200, c)
                if method == "DELETE":
                    if c["State"]["Running"] and (q.get("force") or ["0"])[0] not in ("1", "true"):
                        return self.send(409, {"message": "container is running"})
                    del f.containers[c["Id"]]
                    return self.send(204)
                if op in ("start", "restart", "unpause"):
                    c["State"]["Running"] = True
                    return self.send(204)
                if op in ("stop", "kill", "pause"):
                    c["State"]["Running"] = False
                    return self.send(204)
                if op == "update":
                    if "RestartPolicy" in data:
                        c["HostConfig"]["RestartPolicy"] = data["RestartPolicy"]
                    return self.send(200, {"Warnings": []})
                if op == "exec":
                    return self.send(201, {"Id": secrets.token_hex(32)})
                return self.send(200, {})
            if seg[:1] == ["volumes"]:
                if len(seg) == 1:
                    return self.send(200, {"Volumes": list(f.volumes.values()), "Warnings": []})
                if seg[1] == "create":
                    name = data.get("Name") or secrets.token_hex(32)
                    if name not in f.volumes:
                        f.person_volume(name)
                    return self.send(201, f.volumes[name])
                v = f.volumes.get(seg[1])
                if not v:
                    return self.send(404, {"message": "no such volume"})
                if method == "DELETE":
                    del f.volumes[seg[1]]
                    return self.send(204)
                return self.send(200, v)
            if seg[:1] == ["networks"]:
                if len(seg) > 1 and seg[1] == "create":
                    nid = secrets.token_hex(32)
                    f.networks[nid] = {"Id": nid, "Name": data.get("Name")}
                    return self.send(201, {"Id": nid, "Warning": ""})
                n = next((x for x in f.networks.values() if len(seg) > 1 and seg[1] in (x["Id"], x["Name"])), None)
                if not n:
                    return self.send(404, {"message": "no such network"})
                if method == "DELETE":
                    del f.networks[n["Id"]]
                    return self.send(204)
                return self.send(200, n)
            if seg[:1] == ["images"] and len(seg) > 1:
                ref = "/".join(seg[1:-1]) if seg[-1] == "json" else "/".join(seg[1:])
                img = f.images.get(ref) or next((i for i in f.images.values() if i["Id"] == ref), None)
                if not img:
                    return self.send(404, {"message": "no such image"})
                if method == "DELETE":
                    f.images = {k: v for k, v in f.images.items() if v["Id"] != img["Id"]}
                    return self.send(200, [{"Deleted": img["Id"]}])
                return self.send(200, img)
        return self.send(404, {"message": f"page not found: {path}"})

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")

    def do_PUT(self):
        self.route("PUT")

    def do_HEAD(self):
        self.route("HEAD")
