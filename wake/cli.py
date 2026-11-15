"""wake: run an AI agent so it can only destroy what it made, and stops completely when you stop it."""
import argparse
import json
import os
import signal
import sys
import time

USAGE = """wake run [options] -- COMMAND...   run an agent under WAKE
wake stop [SESSION]                  stop it: everything it made stops with it
wake restore [SESSION] [PATH...]     put back your things it changed, moved or deleted
wake whose PATH|PID                  who made this, and how WAKE knows
wake mine                            what the agent has made (for the agent itself)
wake kill-mine PATTERN               stop only the agent's own processes that match
wake keep PATH|PID                   make something the agent made yours
wake report [SESSION]                what happened in a run
wake export [SESSION] [--out FILE]   the whole run as one Relate file
wake view [SESSION]                  watch the agent's body live in the browser
wake sessions                        list runs"""


def say(msg):
    print(f"wake: {msg}", file=sys.stderr, flush=True)


def plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


# ---------------------------------------------------------------- run
def cmd_run(argv):
    if "--" not in argv:
        raise SystemExit("usage: wake run [--keep-running] [--no-triggers] [--no-sweep] [--no-docker] -- COMMAND...")
    i = argv.index("--")
    opts, command = argv[:i], argv[i + 1:]
    if not command:
        raise SystemExit("wake run: give the agent's command after --")
    from .session import Session
    from .supervisor import Supervisor
    s = Session.create(command)
    env = dict(os.environ, WAKE_SESSION=s.id, WAKE_HOME=s.home)
    quiet = "--quiet" in opts
    if not quiet:
        say(f"session {s.id}. Stop it with `wake stop`; watch it with `wake view`.")
    sup = Supervisor(s, stop_on_exit="--keep-running" not in opts,
                     triggers="--no-triggers" not in opts, sweep="--no-sweep" not in opts,
                     docker="--no-docker" not in opts)
    printed = []

    def stopped(r):
        if not quiet:
            print_summary(r)
            if r.get("adopted_running"):
                say(f"{plural(r['adopted_running'], 'process', 'processes')} you adopted {'is' if r['adopted_running'] == 1 else 'are'} "
                    "still running. WAKE stays out of the way and returns when they end.")
        printed.append(True)

    sup.on_stopped = stopped
    summary = sup.run(command, env)
    if not quiet and not printed:
        print_summary(summary)
    code = summary.get("exit", 0)
    sys.exit(code if isinstance(code, int) and code >= 0 else 128 + abs(code or 0))


def print_summary(r):
    say(f"the agent ran {plural(r.get('processes') or 0, 'process', 'processes')} "
        f"and made {plural(r.get('made') or 0, 'file')}.")
    if r.get("kept"):
        say(f"it changed, moved or deleted {plural(r['kept'], 'thing')} of yours. "
            "Your originals are kept: `wake restore` puts them back.")
    if r.get("refused") or r.get("narrowed"):
        yours = "your processes or containers" if r.get("docker") is not None else "your processes"
        say(f"it reached for {yours} {plural((r.get('refused') or 0) + (r.get('narrowed') or 0), 'time')}. "
            "WAKE let none of it through.")
    if r.get("stopped"):
        left = len(r.get("swept") or []) + len(r.get("escaped") or [])
        bits = [f"stopped everything it started ({r.get('reason')})"]
        if r.get("containers"):
            bits.append(f"stopped {plural(len(r['containers']), 'container')} it started ({', '.join(r['containers'][:4])})")
        if r.get("removed_triggers"):
            bits.append(f"removed {plural(len(r['removed_triggers']), 'file')} it planted to run later")
        if r.get("restored_triggers"):
            bits.append(f"put back {plural(len(r['restored_triggers']), 'start-up file')} of yours it had changed")
        if left:
            bits.append(f"stopped {plural(left, 'process', 'processes')} running its code outside its body")
        say("; ".join(bits) + f" in {r.get('ms')} ms.")


# ---------------------------------------------------------------- stop
def cmd_stop(args):
    from .session import Session
    s = Session.find(args.session)
    if s.supervisor_alive():
        s.send({"op": "stop", "reason": "wake stop"})
        for _ in range(300):
            if s.meta().get("status") in ("stopped", "finished"):
                break
            time.sleep(0.05)
        r = s.meta().get("summary") or {}
        print_summary(r)
        return
    from .supervisor import offline_stop
    print_summary(offline_stop(s))


# ---------------------------------------------------------------- restore
def cmd_restore(args):
    from .body import Body
    from .restore import restore_all
    from .session import Session
    from .vault import Vault
    s = Session.find(args.session)
    if s.supervisor_alive() and not args.force:
        raise SystemExit("wake: the agent is still running. Stop it first (`wake stop`), or pass --force.")
    body = Body(s.store, s.id)
    try:
        acts = restore_all(body, Vault(s.vault), only=[os.path.abspath(p) for p in args.paths] or None,
                           dry=args.dry_run)
    finally:
        body.close()
    n = 0
    for what, path in acts:
        if what in ("put back", "moved back", "recreated"):
            n += 1
        if args.verbose or args.dry_run:
            print(f"{what:40s} {path}")
    say(f"{'would put back' if args.dry_run else 'put back'} {plural(n, 'thing')} of yours."
        + ("" if args.dry_run else " Anything that stood in the way is in the vault, not deleted."))


# ---------------------------------------------------------------- whose
def _open_read(s):
    from .body import Body
    return Body(s.store, s.id, read_only=True)


def when(t):
    return time.strftime("%H:%M:%S", time.localtime(t)) if t else "?"


def cmd_whose(args):
    from .body import WHOSE_FILE, WHOSE_KEPT, WHOSE_PROCESS
    from .ident import proc_key
    from .session import Session
    s = Session.find(args.session)
    b = _open_read(s)
    t = args.target
    if t.isdigit():
        key = proc_key(int(t))
        rows = b.q(WHOSE_PROCESS, {"key": key}) if key else []
        if not rows:
            rows = b.q("MATCH (p:Process {pid: $pid}) RETURN p.key AS key ORDER BY p.born DESC LIMIT 1", {"pid": int(t)})
            rows = b.q(WHOSE_PROCESS, {"key": rows[0]["key"]}) if rows else []
        if not rows:
            print(f"process {t} is not the agent's: WAKE has no record of the agent starting it.")
            return
        chain = rows[0]["chain"]
        print(f"process {t} is the agent's. WAKE recorded it being started:")
        for n in chain:
            if n.get("pid") is not None:
                print(f"  {n['pid']:>7}  {(n.get('cmd') or '')[:90]}  (started {when(n.get('born'))})")
        print("  ...by the WAKE session itself." + (" You adopted it: it is yours now." if any(n.get("adopted") for n in chain) else ""))
        return
    path = os.path.abspath(t)
    made = b.q(WHOSE_FILE, {"path": path})
    kept = b.q(WHOSE_KEPT, {"path": path})
    if not made and not kept:
        print(f"{path}: WAKE has no record of the agent making or touching it. It is yours.")
        return
    if made:
        chain = made[0]["chain"]
        f, procs = chain[0], chain[1:-1]
        state = " It is yours now: you adopted it." if f.get("adopted") else (" It has since been deleted." if f.get("gone") else "")
        print(f"{path} was made by the agent at {when(f.get('born'))}.{state}")
        for n in procs:
            print(f"  by {n['pid']:>7}  {(n.get('cmd') or '')[:90]}")
        print("  started by the WAKE session. The agent may destroy it; it is its own.")
    if kept:
        first = kept[0]["chain"][0]
        print(f"{path} is yours: it was there before the agent touched it.")
        verbs = {"overwritten": "wrote over", "deleted": "deleted", "replaced": "replaced", "moved": "moved"}
        for r in kept:
            p = r["chain"][1]
            print(f"  {when(r['at'])}  the agent {verbs.get(r['how'], r['how'])} it: {p['pid']} `{(p.get('cmd') or '')[:80]}`")
        print("  Your original is kept." + (" It has been put back." if first.get("restored") else
                                           " `wake restore " + path + "` puts it back."))


# ---------------------------------------------------------------- mine, kill-mine, keep
def cmd_mine(args):
    from .session import Session
    s = Session.find(args.session)
    b = _open_read(s)
    alive = b.alive()
    files = b.made_live_files()
    print(f"processes of mine still running: {len(alive)}")
    for r in alive:
        print(f"  {r['pid']:>7}  {(r.get('cmd') or '')[:100]}")
    print(f"files and folders I made that still exist: {len(files)}")
    tops = {}
    for f in files:
        d = os.path.dirname(f["path"])
        tops[d] = tops.get(d, 0) + 1
    for d, n in sorted(tops.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {n:>6}  in {d}")


def cmd_kill_mine(args):
    from .session import Session
    s = Session.find(args.session)
    b = _open_read(s)
    me = {os.getpid(), os.getppid()}
    hits = [r for r in b.alive() if args.pattern in (r.get("cmd") or "") and r["pid"] not in me]
    for r in hits:
        try:
            os.kill(r["pid"], args.signal)
            print(f"stopped {r['pid']}  {(r.get('cmd') or '')[:100]}")
        except OSError as e:
            print(f"could not stop {r['pid']}: {e.strerror}")
    if not hits:
        print(f"none of my running processes match {args.pattern!r}. Nothing of yours was touched.")


def cmd_keep(args):
    from .session import Session
    s = Session.find(args.session)
    if not s.supervisor_alive():
        raise SystemExit("wake: that session is not running; there is nothing to keep from it.")
    target = args.target if args.target.isdigit() else os.path.abspath(args.target)
    r = s.send({"op": "keep", "target": target})
    say(f"{r['adopted']} is yours now. The agent can no longer destroy it, and stopping the agent will not stop it."
        if r.get("ok") else r.get("error"))


# ---------------------------------------------------------------- report, sessions
def cmd_report(args):
    from .session import Session
    s = Session.find(args.session)
    b = _open_read(s)
    meta = s.meta()
    c = b.counts()
    if args.json:
        print(json.dumps({"session": s.id, "meta": meta, "counts": c, "kept": b.kept_all()}, indent=2, default=str))
        return
    print(f"session {s.id}: `{' '.join(meta.get('cmd') or [])}` ({meta.get('status')})")
    print(f"  processes {c.get('processes')}, files made {c.get('made')}, your things kept {c.get('kept')}, "
          f"reaches refused {c.get('refused')}" + (f", Docker things made {c['docker']}" if c.get("docker") else ""))
    kept = b.kept_all()
    if kept:
        print("  your things the agent changed, moved or deleted (originals kept):")
        for k in kept[:30]:
            print(f"    {'/'.join(k['whys'] or []):24s} {k['path']}" + ("  [put back]" if k.get("restored") else ""))
        if len(kept) > 30:
            print(f"    ...and {len(kept) - 30} more")
    tried = b.q("""MATCH (p:Process)-[t:TRIED]->(y:Process)
                   RETURN p.pid AS by, p.cmd AS bycmd, y.pid AS pid, y.cmd AS cmd, t.detail AS what ORDER BY t.at""")
    for r in tried:
        print(f"  refused: {r['by']} `{(r['bycmd'] or '')[:50]}` sent {r['what']} to your process {r['pid']} `{(r['cmd'] or '')[:50]}`")
    for r in b.q("""MATCH (p:Process)-[t:TRIED]->(y:Docker)
                     RETURN p.pid AS by, p.cmd AS bycmd, t.detail AS what ORDER BY t.at"""):
        print(f"  refused: {r['by']} `{(r['bycmd'] or '')[:50]}` asked the Docker daemon to {r['what']}, which is yours")
    for e in b.q("MATCH (:Session)-[:LOGGED]->(e:Event) WHERE e.kind IN ['narrowed','swept','adopted','docker'] RETURN e.kind AS k, e.detail AS d ORDER BY e.at"):
        print(f"  {e['k']}: {e['d']}")
    if meta.get("summary"):
        print_summary(meta["summary"])


def cmd_export(args):
    """One file per run: Relate's own single-file bundle of the body graph, made with `relate clone`."""
    import shutil
    import subprocess
    from .session import Session
    s = Session.find(args.session)
    if s.supervisor_alive():
        raise SystemExit("wake: the agent is still running. Export it after `wake stop`.")
    out = os.path.abspath(args.out or f"wake-{s.id}.rl")
    relate = shutil.which("relate")
    if not relate:
        raise SystemExit("wake: export needs the `relate` command (curl -sSfL https://relatedb.ai/install.sh | bash)")
    if os.path.exists(out):
        raise SystemExit(f"wake: {out} already exists; choose another name")
    from .viewer import quiet_env
    r = subprocess.run([relate, "clone", s.store, out], capture_output=True, text=True, env=quiet_env())
    if r.returncode != 0:
        raise SystemExit(f"wake: relate clone failed: {r.stderr.strip()[:300]}")
    say(f"the whole run is in {out}. Query it: relate -d {out} --read-only \"MATCH (k:Kept) RETURN k.path, k.whys\"")


def cmd_sessions(_args):
    from .session import Session
    for sid in Session.all():
        m = Session(Session.find(sid).home, sid).meta()
        print(f"{sid}  {m.get('status', '?'):9s}  {' '.join(m.get('cmd') or [])[:70]}")


def cmd_view(args):
    from .session import Session
    from .viewer import serve, stop
    s = Session.find(args.session)
    if args.stop:
        say(f"stopped {stop(s)} viewer process(es) for session {s.id}.")
        return
    serve(s, args.port, open_browser=not args.no_browser, bind=args.bind)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return
    if argv[0] == "run":
        return cmd_run(argv[1:])
    p = argparse.ArgumentParser(prog="wake", usage=USAGE)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("stop")
    a.add_argument("session", nargs="?")
    a = sub.add_parser("restore")
    a.add_argument("paths", nargs="*")
    a.add_argument("--session")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--force", action="store_true")
    a.add_argument("-v", "--verbose", action="store_true")
    a = sub.add_parser("whose")
    a.add_argument("target")
    a.add_argument("--session")
    a = sub.add_parser("mine")
    a.add_argument("--session")
    a = sub.add_parser("kill-mine")
    a.add_argument("pattern")
    a.add_argument("--signal", type=int, default=signal.SIGTERM)
    a.add_argument("--session")
    a = sub.add_parser("keep")
    a.add_argument("target")
    a.add_argument("--session")
    a = sub.add_parser("report")
    a.add_argument("session", nargs="?")
    a.add_argument("--json", action="store_true")
    sub.add_parser("sessions")
    a = sub.add_parser("export")
    a.add_argument("session", nargs="?")
    a.add_argument("--out")
    a = sub.add_parser("view")
    a.add_argument("session", nargs="?")
    a.add_argument("--port", type=int, default=7700)
    a.add_argument("--bind", default="127.0.0.1")
    a.add_argument("--no-browser", action="store_true")
    a.add_argument("--stop", action="store_true")
    args = p.parse_args(argv)
    {"stop": cmd_stop, "restore": cmd_restore, "whose": cmd_whose, "mine": cmd_mine, "kill-mine": cmd_kill_mine,
     "keep": cmd_keep, "report": cmd_report, "sessions": cmd_sessions, "view": cmd_view,
     "export": cmd_export}[args.cmd](args)


def entry():
    try:
        main()
    except BrokenPipeError:            # `wake sessions | head` closed the pipe early: not an error
        os._exit(0)


if __name__ == "__main__":
    entry()
