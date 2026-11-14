# Architecture

## The pieces

| Piece | File | What it does |
|---|---|---|
| Kernel filter | `wake/seccomp.py` | A seccomp program installed in the agent before it starts. Calls that could destroy something stop the agent; everything else runs at full speed. Inherited by every child, kept across exec. |
| Machine facts | `wake/arch.py` | System call numbers and register layout for aarch64 and x86_64; how to cancel a call and set its result. |
| Supervisor | `wake/supervisor.py` | The ptrace loop. Follows every process and thread the agent starts, decides each stopped call, runs the stop. |
| Body graph | `wake/body.py` | The Relate store for the run: schema, the laws as Cypher, the write queue. |
| Identity | `wake/ident.py` | What a file or process is, independent of its name. |
| Vault | `wake/vault.py` | Where your things are kept before a call changes them. |
| Restore | `wake/restore.py` | Puts your things back; moves whatever stands in the way into the vault. |
| Start-up places | `wake/triggers.py` | Paths that make something run later (shell start-up, cron, systemd, git hooks...). |
| Live view | `wake/viewer.py`, `wake/view/index.html` | `relate serve --read-only=live` on the store, and a page that asks it questions. |
| Command line | `wake/cli.py` | `run`, `stop`, `restore`, `whose`, `keep`, `view`, `report`, `mine`, `kill-mine`. |

## One call, start to finish

1. The agent calls `unlinkat(dirfd, "notes.md")`. The kernel runs WAKE's filter: `unlinkat` is on the list, so
   the agent stops and WAKE is woken.
2. WAKE reads the call and its arguments (`PTRACE_GET_SYSCALL_INFO`), reads the path out of the agent's memory
   (`process_vm_readv`) and resolves it against the agent's working directory or `dirfd`.
3. WAKE takes the file's identity: device, inode and birth time (`statx`).
4. WAKE asks the graph whether a process of this session made that file:
   `MATCH (p:Process)-[:MADE]->(f:File {key: $key}) WHERE f.gone IS NULL AND f.adopted IS NULL RETURN p.key LIMIT 1`.
5. Not the agent's: WAKE hard-links the file into the vault (the same file, no copy) and records
   `(:Process)-[:CHANGED {how: 'deleted'}]->(:Kept)`. Then it lets the call through. The agent sees success.
6. The agent's own: the call goes through and the file is marked gone.

A write to an existing file is the same, except the file is copied (or reflinked) rather than linked, because the
call will change its content in place. A signal to one of your processes is cancelled before it is delivered and
the agent gets "operation not permitted". `kill -1` and group signals are narrowed: delivered to the agent's own
processes, held back from yours, and the agent is told they succeeded.

## The graph

```
(:Session {id, cmd, status})
   -[:SPAWNED {at}]->(:Process {key: "pid:starttime", pid, cmd, exe, alive, adopted})
        -[:SPAWNED]->(:Process)...
        -[:MADE {at}]->(:File {key: "dev:ino:birth", path, kind, gone, adopted})
        -[:CHANGED {how, at, path}]->(:Kept {key, path, now, whys, store, store_kind, restored})
        -[:TRIED {call, detail, at}]->(:Process {yours: true})
   -[:LOGGED]->(:Event {kind, detail, at})
```

- **Process keys** are pid plus start time, so a recycled pid is a different process.
- **File keys** are device, inode and birth time, so a moved file keeps its owner and a new file that reuses an
  inode number does not inherit one.
- **Adoption** (`wake keep`) is pushed down the tree when it happens: the adopted process, every process below it
  and every file they made are marked. The ownership check stays one step; the process check walks the path and
  stops at anything adopted.

## Law 2, the stop

`wake stop` (or the agent exiting) runs, in order:

1. Kill every traced process and thread that is not adopted. New children cannot escape: a child created during
   the stop starts held by WAKE and is killed before it runs.
2. Sweep: any other process running a file the agent made (as its program, as a script argument, or holding the
   script open) is stopped. This catches what the agent left with your scheduler.
3. Start-up places: files the agent made there are moved into the vault; your own files there that it changed are
   put back.
4. Anything still alive in the graph's body that is not traced is killed and reported (expected: nothing).

## Speed

- The filter stops the agent only on calls that could destroy something. An `open()` stops it only when its flags
  ask for writing, creating or truncating.
- Relate runs in the supervisor's process: no socket, no serialization across a process boundary. The ownership
  check costs about 90 microseconds on a body of 60,000 files.
- Writes that happen thousands of times a second (made, gone, renamed, died) wait in a queue and land together in
  one `UNWIND`, at most 25 ms later. Reads check the queue first. `MADE` and `SPAWNED` edges are created exactly
  once per thing, so they are `CREATE`, not `MERGE`: a process that made 20,000 files does not pay for each new
  one by scanning the ones before.
- Directory paths are resolved once and remembered, re-checked against the directory's identity.
- Store durability is `everysecond`: each statement commits; the write-ahead log is synced once a second.

## Why ptrace and seccomp

- They see every program the agent runs, including static binaries and programs that make system calls
  directly, which library interposition (`LD_PRELOAD`) misses.
- They work on the real machine, in place. A copy-on-write overlay protects files by giving the agent a separate
  layer; WAKE lets the agent work on your real project, keeps only what it would destroy, and also governs
  processes and start-up files, which an overlay does not.
- No kernel module and no root are needed: the supervisor runs as you.

## A session on disk

```
$WAKE_HOME/sessions/<id>/
  body/          the Relate store: the agent's body graph
  vault/kept/    your things, kept before they were changed
  vault/removed/ files the agent planted in start-up places, moved out at the stop
  vault/displaced/ whatever stood where a restored thing went back
  events.jsonl   a plain log of every decision
  meta.json      the command, status and the stop's summary
  control.sock   how `wake stop` and `wake keep` reach the supervisor
```
