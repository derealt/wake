"""`wake view`: watch the agent's body live in the browser.

The page asks the body graph its questions in Cypher. When the `relate` command is installed,
WAKE starts Relate's own HTTP server on the session's store in follow mode
(`relate serve --read-only=live`), so the page reads every write the moment it lands, straight
from Relate. Without it, this server answers the same queries from a fresh read-only open.
"""
import http.server
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from importlib import resources


def _port_open(port):
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def stop(session):
    """Stop a running `wake view` for this session by the process ids it recorded, never by a name."""
    pidfile = os.path.join(session.dir, "view.pids")
    try:
        pids = [int(x) for x in open(pidfile).read().split()]
    except (OSError, ValueError):
        return 0
    n = 0
    for pid in pids:
        try:
            os.kill(pid, 15)
            n += 1
        except OSError:
            pass
    os.unlink(pidfile)
    return n


def serve(session, port=7700, open_browser=True, bind="127.0.0.1"):
    """bind 0.0.0.0 is for containers that publish the ports to the host's loopback only."""
    stop(session)
    relate_bin = shutil.which("relate")
    rport, proc, endpoint = port + 1, None, "/query"
    if relate_bin and os.path.isdir(session.store):
        proc = subprocess.Popen([relate_bin, "serve", "--database", session.store, "--read-only=live",
                                 "--http", str(rport), "--auth", "none"] + (["--host", bind] if bind != "127.0.0.1" else []),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if _port_open(rport):
                endpoint = f"http://127.0.0.1:{rport}/db/neo4j/query/v2"
                break
            time.sleep(0.1)
    page = resources.files("wake").joinpath("view/index.html").read_text()
    config = json.dumps({"session": session.id, "endpoint": endpoint,
                         "source": "Relate HTTP Query API, following the store live" if endpoint != "/query"
                         else "a read-only open of the store, refreshed on every question"})
    page = page.replace("/*WAKE_CONFIG*/", f"window.WAKE = {config};")
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/query":
                self.send_error(404)
                return
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            from .body import Body
            try:
                with lock:
                    b = Body(session.store, session.id, read_only=True)
                    try:
                        rows = b.q(req.get("statement", ""), req.get("parameters") or {})
                    finally:
                        b.close()
                fields = list(rows[0].keys()) if rows else []
                out = {"data": {"fields": fields, "values": [[r[f] for f in fields] for r in rows]}}
            except Exception as e:   # noqa: BLE001
                out = {"errors": [{"message": str(e)}]}
            body = json.dumps(out, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer((bind, port), Handler)
    with open(os.path.join(session.dir, "view.pids"), "w") as f:
        f.write(" ".join(str(p) for p in [os.getpid()] + ([proc.pid] if proc else [])))
    url = f"http://127.0.0.1:{port}"
    print(f"wake: watching session {session.id} at {url}  (questions answered by "
          f"{'Relate serve, following the store' if proc and endpoint != '/query' else 'WAKE'}). Ctrl-C to stop watching.",
          flush=True)
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:   # noqa: BLE001
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        if proc:
            proc.terminate()
