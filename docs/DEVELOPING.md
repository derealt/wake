# WAKE

## 1. Purpose

An AI agent can only destroy what it made; when you stop it, everything it made stops with it. WAKE supervises
an agent on a real Linux machine, records everything it starts and makes in an embedded Relate graph (the
agent's body), keeps the person's things before any call that would destroy them, refuses reaches into the
person's processes, and stops the whole body on request.

## 2. Stack

| Part | Choice | Why |
|---|---|---|
| Interception | ptrace + seccomp (`SECCOMP_RET_TRACE`) | Sees every program including static binaries; only destructive calls stop the agent |
| Store | Relate (`relate-db`), embedded, one directory store per session | Answers on the system-call path in microseconds; follow mode for the live view |
| Language | Python 3.10+, standard library only besides `relate-db` | One file per concern, readable in an afternoon |
| Live view | `relate serve --read-only=live` + one static HTML page | The page asks Relate in Cypher over HTTP |
| Machines | Linux aarch64 and x86_64, kernel 5.3+ | `PTRACE_GET_SYSCALL_INFO` |

## 3. Architecture

```
agent ── kernel (seccomp filter) ──stop──▶ supervisor (ptrace loop) ──Cypher──▶ Relate body graph (embedded)
                                                │                                      │
                                                ├── vault (keep before destroy)        └── relate serve --read-only=live ── wake view
                                                └── control socket ◀── wake stop / wake keep
```

One call: the filter stops the agent on a destructive call; the supervisor reads the call and the path, takes the
target's identity, asks the graph whether the agent made it, keeps it in the vault if not, and lets it through.
Signals to processes outside the body are cancelled (EPERM); broadcasts are narrowed to the body. The stop kills
the traced body, sweeps processes running the agent's files, then cleans start-up places.

## 4. Key files

| File | Role |
|---|---|
| `wake/supervisor.py` | ptrace loop, Law 1 handlers per call, Law 2 stop, control socket |
| `wake/body.py` | Relate schema, the laws as Cypher strings, the write queue |
| `wake/seccomp.py` | The BPF filter (which calls stop the agent) |
| `wake/arch.py` | Syscall numbers and registers for aarch64 and x86_64 |
| `wake/ident.py` | File identity (dev:ino:birth via statx) and process identity (pid:starttime) |
| `wake/vault.py`, `wake/restore.py` | Keeping and putting back; restore never destroys |
| `wake/triggers.py` | Start-up places (MITRE ATT&CK Linux persistence locations) |
| `wake/viewer.py`, `wake/view/index.html` | Live view |
| `wake/cli.py` | Command line |
| `trials/` | law1, law2, incidents (46 real reports), bench; results in `trials/results/` |
| `tests/` | Filter interpreter tests and real-kernel law tests |

## 5. Domain model

`(:Session)-[:SPAWNED]->(:Process)-[:MADE]->(:File)`, `(:Process)-[:CHANGED {how}]->(:Kept)`,
`(:Process)-[:TRIED]->(:Process {yours})`, `(:Session)-[:LOGGED]->(:Event)`. Keys: process `pid:starttime`;
file `major:minor:inode:birth`. `adopted` on Process and File marks things the person took over.

## 6. Command line

`wake run [--keep-running] [--no-triggers] [--no-sweep] [--quiet] -- CMD`, `wake stop [SESSION]`,
`wake restore [PATH...] [--dry-run] [--force]`, `wake whose PATH|PID`, `wake keep PATH|PID`, `wake view [--bind]
[--stop]`, `wake report [--json]`, `wake export [--out FILE]`, `wake mine`, `wake kill-mine PATTERN`, `wake sessions`.
`wake run` exits with the agent's exit code.

## 7. Conventions

- Every decision is a Cypher query on the body graph; strings for the laws live at the top of `body.py`.
- `MADE` and `SPAWNED` are created exactly once per thing: use `CREATE`, never `MERGE`, for those edges.
- Writes that happen per file go through the queue (`Body._enqueue`); reads check the queue first.
- Never destroy, even when cleaning up: move into the vault (`kept`, `removed`, `displaced`).
- Stop processes by PID, never by name pattern.
- Plain words in every message a person reads. No em-dashes.

## 8. Domain structure

`interception` (seccomp, arch) → `decisions` (supervisor handlers) → `record` (body) → `keeping` (vault, restore)
→ `stopping` (supervisor stop, triggers) → `seeing` (viewer, cli whose/report).

## 9. Navigation

| If you need | Look in |
|---|---|
| Which calls stop the agent | `wake/seccomp.py` `TRACE_ALWAYS`, `TRACE_IF_WRITE` |
| What happens on a delete | `Supervisor.h_unlink`, `Supervisor.keep` |
| Why a signal was refused | `Supervisor.h_signal`, `body.MINE_PROCESS` |
| What the stop does | `Supervisor.begin_stop`, `complete_stop`, `sweep`, `clean_triggers` |
| How restore decides | `wake/restore.py` `restore_one` |
| The live page's queries | `wake/view/index.html` `Q` |

## 10. Environment rules

- Tests and trials run on Linux only. The trials expect root inside a disposable container and play the person
  and the agent as uid 10001; never run them on a machine you care about.
- `WAKE_HOME` (default `~/.local/state/wake`) holds sessions; the agent can never write there.
- `RELATE_DURABILITY_MODE=everysecond` is set by `body.py` unless already set.
- Every call to the `relate` command line runs with `DO_NOT_TRACK=1` and `RELATE_TELEMETRY=0` (`viewer.quiet_env`).

## The expedition

- BUILT: both laws, the filter on aarch64 and x86_64 (x86_64 checked by interpreter), restore, adoption, live view,
  incident replays, benchmark, tests with sabotage record.
- CHARTERED: adapters that route a service through WAKE (Docker socket, PostgreSQL wire protocol); x86_64 full
  trial run; a recorded run with a real agent.

## Honest classification

A working supervisor with measured results on aarch64. Not a sandbox: it governs destruction, not reading or
network.

## Honest risks

- ptrace semantics differ in corners (job control, debuggers, setuid) and some agents may hit them.
- Services that act for the agent (Docker, databases, systemd) are outside its reach until adapters exist.
