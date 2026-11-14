# A real run, as Relate stored it

`demo-run/` is the Relate directory store from one run of the demo (`demo/demo.sh`): the stand-in agent built
things of its own, then ran commands real agents have run (`rm -rf src`, moves into a folder that does not exist,
`pkill -f http.server`, `kill -9 -1`...), was stopped, and the person's things were put back.

Open it read-only with the `relate` command (`curl -sSfL https://relatedb.ai/install.sh | bash`). Work on a copy,
so the example in the repository stays as it is:

```bash
cp -r examples/demo-run /tmp/demo-run
relate -d /tmp/demo-run --read-only
```

Questions to ask it:

```cypher
// What ran, and how it ended
MATCH (s:Session) RETURN s.cmd, s.status;

// Everything the agent started, however far each process wandered from its parent
MATCH (:Session)-[:SPAWNED]->+(p:Process) RETURN p.pid, p.cmd ORDER BY p.born;

// The person's things it touched: kept before the call went through, then put back
MATCH (p:Process)-[c:CHANGED]->(k:Kept)
RETURN k.path, collect(DISTINCT c.how) AS how, k.restored IS NOT NULL AS put_back ORDER BY k.path;

// What it reached for and was refused
MATCH (p:Process)-[t:TRIED]->(y:Process {yours: true}) RETURN p.cmd, t.detail, y.cmd;

// The chain behind one file the agent made
MATCH path = (f:File)<-[:MADE]-(:Process)<-[:SPAWNED]-+(:Session)
RETURN [n IN nodes(path) | coalesce(n.path, n.cmd)] AS chain LIMIT 5;

// The stop: how long it took and what it pulled
MATCH (:Session)-[:LOGGED]->(e:Event {kind: 'stopped'}) RETURN e.detail;
```

Paths and process ids are from the demo container (`/tmp/p/person/...`). Everything in the store is generated:
no real person's data.
