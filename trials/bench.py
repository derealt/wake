#!/usr/bin/env python3
"""What WAKE costs: the same agent work, with and without WAKE. Median of three runs.

The agent's own work runs as one session, the way an agent works, with each step timed
from inside the session. Destroying the person's files is measured on its own: that is
where WAKE does the most (it keeps every file before the call goes through).

Run as root inside the lab container: python3 trials/bench.py
"""
import json
import os
import shutil
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
LAB = "/tmp/bench"
UID = 10001
ENV = {"HOME": f"{LAB}/home", "PATH": os.path.dirname(sys.executable) + ":/usr/local/bin:/usr/bin:/bin",
       "PYTHONPATH": REPO, "WAKE_HOME": f"{LAB}/wakehome", "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1"}

OWN = [
    ("write 20,000 small files", "mkdir -p out && for d in $(seq 1 100); do mkdir -p out/$d; "
     "python3 -c \"import sys\nfor i in range(200): open(f'out/$d/f{i}.txt','w').write('x'*100)\"; done"),
    ("unpack a 2,000-file archive", "mkdir -p unpacked && tar -xf ../tree.tar -C unpacked"),
    ("compile 600 Python modules", "python3 -m compileall -q -j 1 unpacked/tree/pkg >/dev/null"),
    ("clone a 2,000-file git repository", "git clone -q ../repo.git clone"),
    ("run a test suite", "python3 -m unittest -q test_wake_bench 2>/dev/null || true"),
    ("delete all of it again (rm -rf)", "rm -rf out unpacked clone"),
]
PERSON = [
    ("delete 2,000 of the person's files", "rm -rf ../person_tree"),
    ("rewrite 300 of the person's files", "for f in $(ls ../person/*.txt | head -300); do echo changed >> $f; done"),
]

TEST = '''
import unittest, json, os, tempfile
class T(unittest.TestCase):
    def test_roundtrip(self):
        for i in range(3000):
            d = {"i": i, "s": "x" * (i % 50)}
            self.assertEqual(json.loads(json.dumps(d)), d)
    def test_files(self):
        with tempfile.TemporaryDirectory(dir=".") as t:
            for i in range(300):
                p = os.path.join(t, f"{i}.json")
                open(p, "w").write(json.dumps({"i": i}))
                self.assertEqual(json.load(open(p))["i"], i)
'''


def timed(steps):
    lines = ["cd " + f"{LAB}/work", "t() { date +%s.%N; }"]
    for i, (_, cmd) in enumerate(steps):
        lines.append(f"s=$(t); {cmd}; e=$(t); echo \"STEP {i} $s $e\"")
    return "\n".join(lines) + "\n"


def prepare():
    shutil.rmtree(LAB, ignore_errors=True)
    for d in ("home", "person", ENV["WAKE_HOME"]):
        os.makedirs(d if d.startswith("/") else f"{LAB}/{d}")
    tree = f"{LAB}/tree"
    for d in range(40):
        os.makedirs(f"{tree}/data/d{d}")
        for i in range(35):
            open(f"{tree}/data/d{d}/f{i}.txt", "w").write(f"line {i}\n" * 30)
    for d in range(20):
        os.makedirs(f"{tree}/pkg/m{d}")
        open(f"{tree}/pkg/m{d}/__init__.py", "w").write("")
        for i in range(30):
            open(f"{tree}/pkg/m{d}/mod{i}.py", "w").write(
                "".join(f"def f{j}(x):\n    return [x * {j} for _ in range(3)]\n\n" for j in range(25)))
    subprocess.run(["tar", "-cf", f"{LAB}/tree.tar", "-C", LAB, "tree"], check=True)
    src = f"{LAB}/reposrc"
    shutil.copytree(tree, src)
    g = ["git", "-c", "user.name=x", "-c", "user.email=x@x", "-C", src]
    subprocess.run(["git", "init", "-q", src], check=True)
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "clone", "-q", "--bare", src, f"{LAB}/repo.git"], check=True)


def reset_person():
    shutil.rmtree(f"{LAB}/person", ignore_errors=True)
    shutil.rmtree(f"{LAB}/person_tree", ignore_errors=True)
    os.makedirs(f"{LAB}/person")
    for i in range(400):
        open(f"{LAB}/person/doc_{i:03d}.txt", "w").write("the person's text\n" * 40)
    shutil.copytree(f"{LAB}/tree", f"{LAB}/person_tree")
    shutil.rmtree(f"{LAB}/work", ignore_errors=True)
    os.makedirs(f"{LAB}/work")
    open(f"{LAB}/work/test_wake_bench.py", "w").write(TEST)
    shutil.rmtree(ENV["WAKE_HOME"], ignore_errors=True)
    os.makedirs(ENV["WAKE_HOME"])
    subprocess.run(["chown", "-R", f"{UID}:{UID}", LAB], check=True)


def run(script, with_wake):
    path = f"{LAB}/script.sh"
    open(path, "w").write(script)
    os.chown(path, UID, UID)
    argv = ([sys.executable, "-m", "wake", "run", "--quiet", "--", "bash", path] if with_wake else ["bash", path])
    t = time.perf_counter()
    r = subprocess.run(argv, user=UID, group=UID, env=ENV, capture_output=True, text=True, timeout=1200)
    total = time.perf_counter() - t
    steps = {}
    for line in r.stdout.splitlines():
        if line.startswith("STEP "):
            _, i, s, e = line.split()
            steps[int(i)] = float(e) - float(s)
    return steps, total


if __name__ == "__main__":
    prepare()
    res = {"alone": [], "wake": []}
    startup = []
    for _ in range(3):
        for arm in ("alone", "wake"):
            reset_person()
            own, tot_own = run(timed(OWN), arm == "wake")
            person, tot_person = run(timed(PERSON), arm == "wake")
            res[arm].append({"own": own, "person": person, "total": tot_own})
        reset_person()
        startup.append(run("true\n", True)[1] - run("true\n", False)[1])
    rows = []
    print(f"{'agent work':44s} {'alone':>8s} {'WAKE':>8s} {'extra':>8s}")
    for group, steps in (("own", OWN), ("person", PERSON)):
        if group == "person":
            print("-- the person's files (WAKE keeps every one before the call goes through)")
        for i, (name, _) in enumerate(steps):
            a = statistics.median(r[group][i] for r in res["alone"])
            w = statistics.median(r[group][i] for r in res["wake"])
            rows.append({"work": name, "alone_s": round(a, 3), "wake_s": round(w, 3), "extra_s": round(w - a, 3)})
            print(f"{name:44s} {a:7.2f}s {w:7.2f}s {w - a:+7.2f}s")
    st = statistics.median(startup)
    print(f"{'starting WAKE and opening the body graph':44s} {'':8s} {'':8s} {st:+7.2f}s")
    os.makedirs(os.path.join(REPO, "trials", "results"), exist_ok=True)
    with open(os.path.join(REPO, "trials", "results", "bench.json"), "w") as f:
        json.dump({"rows": rows, "startup_s": round(st, 3), "machine": os.uname().machine,
                   "kernel": os.uname().release, "runs": 3, "statistic": "median"}, f, indent=1)
