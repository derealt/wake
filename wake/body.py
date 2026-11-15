"""The agent's body: a Relate graph of everything it started, made and touched.

One Session node per run. Every process the agent starts hangs off it by SPAWNED, every file
or directory a process creates hangs off that process by MADE, every one of your things it
changes or deletes is a Kept node reached by CHANGED, and every reach into your processes that
WAKE refused is a TRIED edge. Every decision WAKE makes is a Cypher query on this graph.
"""
import os
import time

os.environ.setdefault("RELATE_DURABILITY_MODE", "everysecond")   # each statement commits; fsync once a second
import relate  # noqa: E402

SCHEMA = [
    "CREATE INDEX process_key IF NOT EXISTS FOR (p:Process) ON (p.key)",
    "CREATE INDEX file_key IF NOT EXISTS FOR (f:File) ON (f.key)",
    "CREATE INDEX file_path IF NOT EXISTS FOR (f:File) ON (f.path)",
    "CREATE INDEX kept_key IF NOT EXISTS FOR (k:Kept) ON (k.key)",
    "CREATE INDEX kept_path IF NOT EXISTS FOR (k:Kept) ON (k.path)",
    "CREATE INDEX docker_key IF NOT EXISTS FOR (d:Docker) ON (d.key)",
]

# ---- the laws, as queries. These strings are shown in the README and the live view.

# Law 1 for files, on every destructive call: did a process of this session make it?
# Adoption (`wake keep`) is pushed down the tree when it happens, so this is one step.
MINE_FILE = """
MATCH (p:Process)-[:MADE]->(f:File {key: $key})
WHERE f.gone IS NULL AND f.adopted IS NULL
RETURN p.key AS by LIMIT 1"""

# Law 1 for processes: is this process descended from the session, through nothing you adopted?
MINE_PROCESS = """
MATCH path = (p:Process {key: $key})<-[:SPAWNED]-+(:Session)
WHERE none(n IN nodes(path) WHERE coalesce(n.adopted, false))
RETURN length(path) AS depth LIMIT 1"""

# Law 1 for what the Docker daemon holds (containers, volumes, networks, images): did the agent create it?
MINE_DOCKER = """
MATCH (p:Process)-[:MADE]->(d:Docker {key: $key})
WHERE d.gone IS NULL AND d.adopted IS NULL
RETURN p.key AS by LIMIT 1"""

# Law 2: the whole body that is still alive, however far it has wandered from its parent.
BODY_ALIVE = """
MATCH (:Session)-[:SPAWNED]->+(p:Process)
WHERE p.alive AND p.adopted IS NULL
RETURN DISTINCT p.key AS key, p.pid AS pid, p.cmd AS cmd"""

# Explanations: the chain from a thing back to the session.
WHOSE_FILE = """
MATCH path = (f:File)<-[:MADE]-(:Process)<-[:SPAWNED]-+(:Session)
WHERE f.path = $path
RETURN [n IN nodes(path) | n {.key, .path, .pid, .cmd, .born, .gone, .adopted}] AS chain
ORDER BY f.born DESC LIMIT 1"""
WHOSE_KEPT = """
MATCH path = (k:Kept)<-[c:CHANGED]-(:Process)<-[:SPAWNED]-+(:Session)
WHERE k.path = $path OR k.now = $path
RETURN [n IN nodes(path) | n {.key, .path, .pid, .cmd, .first, .whys, .store, .restored}] AS chain,
       c.how AS how, c.at AS at
ORDER BY c.at LIMIT 20"""
WHOSE_PROCESS = """
MATCH path = (p:Process {key: $key})<-[:SPAWNED]-+(:Session)
RETURN [n IN nodes(path) | n {.key, .pid, .cmd, .born, .alive, .adopted}] AS chain LIMIT 1"""


def now():
    return round(time.time(), 6)


class Body:
    """Writes that happen thousands of times a second (made, gone, renamed, died) wait in a short queue
    and land together in one UNWIND, at most 25 ms later. Reads look at the queue before the graph,
    so a decision never misses a write that is still on its way."""

    FLUSH_EVERY = 0.025
    FLUSH_AT = 400

    def __init__(self, path, session_id, read_only=False, meta=None):
        self.path = path
        self.sid = session_id
        self.queue = {"made": {}, "gone": {}, "renamed": {}, "died": {}}
        self.queued = 0
        self.last_flush = time.monotonic()
        self.db = relate.connect(path, read_only=read_only)
        if not read_only:
            for s in SCHEMA:
                self.db.execute(s)
            self.q("MERGE (s:Session {id: $sid}) ON CREATE SET s += $meta, s.started = $t",
                   {"sid": session_id, "meta": meta or {}, "t": now()})

    def q(self, s, p=None):
        return self.db.execute(s, p or {}).fetchall()

    # ------------------------------------------------------------ the write queue
    def _enqueue(self, kind, key, value):
        self.queue[kind][key] = value
        self.queued += 1
        if self.queued >= self.FLUSH_AT:
            self.flush()

    def tick(self):
        if self.queued and time.monotonic() - self.last_flush >= self.FLUSH_EVERY:
            self.flush()

    def flush(self):
        q = self.queue
        if q["made"]:
            self.q("""UNWIND $rows AS r
                      MATCH (p:Process {key: r.p})
                      CREATE (p)-[:MADE {at: r.t}]->(:File {key: r.k, path: r.path, kind: r.kind, born: r.t,
                                                            adopted: p.adopted})""",
                   {"rows": list(q["made"].values())})
        if q["renamed"]:
            self.q("UNWIND $rows AS r MATCH (f:File {key: r.k}) SET f.path = r.path",
                   {"rows": list(q["renamed"].values())})
        if q["gone"]:
            self.q("UNWIND $rows AS r MATCH (f:File {key: r.k}) SET f.gone = r.t", {"rows": list(q["gone"].values())})
        if q["died"]:
            self.q("UNWIND $rows AS r MATCH (p:Process {key: r.k}) SET p.alive = false, p.died = r.t",
                   {"rows": list(q["died"].values())})
        self.queue = {"made": {}, "gone": {}, "renamed": {}, "died": {}}
        self.queued = 0
        self.last_flush = time.monotonic()

    def close(self):
        try:
            if self.queued:
                self.flush()
            self.db.close()
        except Exception:
            pass

    # ------------------------------------------------------------ writes
    def root(self, key, pid, cmd, cwd):
        self.q("""MATCH (s:Session {id: $sid})
                  CREATE (p:Process {key: $key, pid: $pid, cmd: $cmd, cwd: $cwd, born: $t, alive: true})
                  CREATE (s)-[:SPAWNED {at: $t}]->(p)""",
               {"sid": self.sid, "key": key, "pid": pid, "cmd": cmd, "cwd": cwd, "t": now()})

    def child(self, parent_key, key, pid):
        if self.queued:
            self.flush()
        # called once per process (the supervisor guarantees it), so CREATE: no scan of the parent's edges
        self.q("""MATCH (a:Process {key: $a})
                  CREATE (a)-[:SPAWNED {at: $t}]->(:Process {key: $key, pid: $pid, born: $t, alive: true,
                                                            cmd: a.cmd, adopted: a.adopted})""",
               {"a": parent_key, "key": key, "pid": pid, "t": now()})

    def executed(self, key, cmd, exe):
        if self.queued:
            self.flush()
        self.q("MATCH (p:Process {key: $key}) SET p.cmd = $cmd, p.exe = $exe", {"key": key, "cmd": cmd, "exe": exe})

    def died(self, key):
        self._enqueue("died", key, {"k": key, "t": now()})

    def made(self, pkey, fkey, path, kind):
        self.queue["gone"].pop(fkey, None)
        self._enqueue("made", fkey, {"p": pkey, "k": fkey, "path": path, "kind": kind, "t": now()})

    def gone(self, fkey):
        self._enqueue("gone", fkey, {"k": fkey, "t": now()})

    def renamed(self, fkey, path):
        self._enqueue("renamed", fkey, {"k": fkey, "path": path})

    def kept(self, pkey, fkey, path, kind, how, store, store_kind, size, mode):
        if self.queued:
            self.flush()
        self.q("""MATCH (p:Process {key: $p})
                  MERGE (k:Kept {key: $k})
                  ON CREATE SET k.path = $path, k.kind = $kind, k.first = $t, k.store = $store,
                                k.store_kind = $sk, k.size = $size, k.mode = $mode, k.whys = [$how]
                  ON MATCH SET k.whys = coalesce(k.whys, []) + [$how],
                               k.store = coalesce(k.store, $store), k.store_kind = coalesce(k.store_kind, $sk)
                  CREATE (p)-[:CHANGED {how: $how, at: $t, path: $path}]->(k)""",
               {"p": pkey, "k": fkey, "path": path, "kind": kind, "how": how, "store": store, "sk": store_kind,
                "size": size, "mode": mode, "t": now()})

    def kept_entry(self, fkey):
        r = self.q("MATCH (k:Kept {key: $k}) RETURN k.store AS store, k.store_kind AS sk, k.whys AS whys LIMIT 1",
                   {"k": fkey})
        return r[0] if r else None

    def kept_store(self, fkey, store, store_kind):
        self.q("MATCH (k:Kept {key: $k}) SET k.store = $s, k.store_kind = $sk", {"k": fkey, "s": store, "sk": store_kind})

    def kept_now(self, fkey, path):
        self.q("MATCH (k:Kept {key: $k}) SET k.now = $p", {"k": fkey, "p": path})

    def tried(self, pkey, call, target_key, target_pid, target_cmd, detail):
        if self.queued:
            self.flush()
        self.q("""MATCH (p:Process {key: $p})
                  MERGE (y:Process {key: $tk})
                  ON CREATE SET y.pid = $tp, y.cmd = $tc, y.yours = true
                  CREATE (p)-[:TRIED {call: $call, detail: $d, at: $t, outcome: 'refused'}]->(y)""",
               {"p": pkey, "tk": target_key, "tp": target_pid, "tc": target_cmd, "call": call, "d": detail, "t": now()})

    def event(self, kind, detail, pkey=None):
        self.q("""MATCH (s:Session {id: $sid})
                  CREATE (e:Event {kind: $kind, detail: $d, at: $t, by: $p})
                  CREATE (s)-[:LOGGED]->(e)""",
               {"sid": self.sid, "kind": kind, "d": detail, "t": now(), "p": pkey})

    def adopt_file(self, fkey):
        if self.queued:
            self.flush()
        self.q("MATCH (f:File {key: $k}) SET f.adopted = $t", {"k": fkey, "t": now()})

    def adopt_process(self, pkey):
        if self.queued:
            self.flush()
        t = now()
        self.q("""MATCH (a:Process {key: $k})-[:SPAWNED]->*(d:Process) SET d.adopted = $t""", {"k": pkey, "t": t})
        self.q("""MATCH (a:Process {key: $k})-[:SPAWNED]->*(d:Process)-[:MADE]->(f:File)
                  WHERE f.adopted IS NULL SET f.adopted = $t""", {"k": pkey, "t": t})

    def session_set(self, **props):
        self.q("MATCH (s:Session {id: $sid}) SET s += $p", {"sid": self.sid, "p": props})

    # ------------------------------------------------------------ what the Docker daemon holds for the agent
    def made_docker(self, pkey, kind, key, oid, name, detail=""):
        self.flush()
        self.q("""MATCH (p:Process {key: $p})
                  CREATE (p)-[:MADE {at: $t}]->(:Docker {key: $k, kind: $kind, oid: $oid, name: $name,
                                                         detail: $d, born: $t, adopted: p.adopted})""",
               {"p": pkey, "k": key, "kind": kind, "oid": oid, "name": name, "d": detail, "t": now()})

    def docker_gone(self, key):
        self.q("MATCH (d:Docker {key: $k}) WHERE d.gone IS NULL SET d.gone = $t", {"k": key, "t": now()})

    def tried_docker(self, pkey, call, key, kind, name, detail):
        self.flush()
        self.q("""MATCH (p:Process {key: $p})
                  MERGE (y:Docker {key: $k})
                  ON CREATE SET y.kind = $kind, y.name = $name, y.yours = true
                  CREATE (p)-[:TRIED {call: $call, detail: $d, at: $t, outcome: 'refused'}]->(y)""",
               {"p": pkey, "k": key, "kind": kind, "name": name, "call": call, "d": detail, "t": now()})

    def mine_docker(self, key):
        return bool(self.q(MINE_DOCKER, {"key": key}))

    def docker_live(self, kind=None):
        return self.q("""MATCH (:Process)-[:MADE]->(d:Docker)
                         WHERE d.gone IS NULL AND d.adopted IS NULL AND ($kind IS NULL OR d.kind = $kind)
                         RETURN d.key AS key, d.kind AS kind, d.oid AS oid, d.name AS name""", {"kind": kind})

    def mark_restored(self, fkey):
        self.q("MATCH (k:Kept {key: $k}) SET k.restored = $t", {"k": fkey, "t": now()})

    # ------------------------------------------------------------ reads (the laws)
    def mine_file(self, fkey):
        if fkey in self.queue["gone"]:
            return False
        if fkey in self.queue["made"]:
            return True
        return bool(self.q(MINE_FILE, {"key": fkey}))

    def mine_process(self, pkey):
        self.flush()
        return bool(self.q(MINE_PROCESS, {"key": pkey}))

    def alive(self):
        self.flush()
        return self.q(BODY_ALIVE)

    def made_live_files(self):
        self.flush()
        return self.q("""MATCH (p:Process)-[:MADE]->(f:File)
                         WHERE f.gone IS NULL
                         RETURN f.key AS key, f.path AS path, f.kind AS kind, f.adopted AS adopted, p.cmd AS by""")

    def kept_all(self):
        self.flush()
        return self.q("""MATCH (k:Kept)
                         RETURN k.key AS key, k.path AS path, k.now AS now, k.kind AS kind, k.whys AS whys,
                                k.store AS store, k.store_kind AS sk, k.mode AS mode, k.size AS size,
                                k.restored AS restored, k.first AS first
                         ORDER BY size(k.path), k.path""")

    def counts(self):
        self.flush()
        r = self.q("""MATCH (s:Session {id: $sid})
                      OPTIONAL MATCH (s)-[:SPAWNED]->+(p:Process)
                      WITH s, count(DISTINCT p) AS processes
                      OPTIONAL MATCH (:Process)-[:MADE]->(f:File)
                      WITH s, processes, count(DISTINCT f) AS made
                      OPTIONAL MATCH (k:Kept)
                      WITH s, processes, made, count(DISTINCT k) AS kept
                      OPTIONAL MATCH (:Process)-[t:TRIED]->()
                      WITH s, processes, made, kept, count(t) AS refused
                      OPTIONAL MATCH (:Process)-[:MADE]->(d:Docker)
                      RETURN processes, made, kept, refused, count(DISTINCT d) AS docker""", {"sid": self.sid})
        return r[0] if r else {}
