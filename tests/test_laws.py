"""The two laws on a real kernel. Linux only: each test runs a small agent under `wake run`.

    PYTHONPATH=. python3 -m unittest discover -s tests -v
"""
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

LINUX = sys.platform.startswith("linux")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


@unittest.skipUnless(LINUX, "WAKE runs on Linux")
class Laws(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wake-test-")
        self.home = os.path.join(self.root, "home")
        self.proj = os.path.join(self.home, "project")
        os.makedirs(self.proj)
        self.env = dict(os.environ, HOME=self.home, WAKE_HOME=os.path.join(self.root, "wakehome"),
                        PYTHONPATH=REPO, P=self.proj)
        self.people = []

    def tearDown(self):
        for p in self.people:
            if p.poll() is None:
                p.kill()
            p.wait()
        shutil.rmtree(self.root, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    def put(self, rel, data="the person's words\n" * 20):
        path = os.path.join(self.proj, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(data)
        return path

    def wake(self, *args, timeout=60):
        return subprocess.run([sys.executable, "-m", "wake", *args], env=self.env, capture_output=True, text=True,
                              timeout=timeout, cwd=self.proj)

    def agent(self, script, *opts):
        return self.wake("run", "--quiet", *opts, "--", "bash", "-c", f"cd $P\n{script}")

    def person_process(self, *cmd):
        p = subprocess.Popen(cmd or ["sleep", "300"], env=self.env, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.people.append(p)
        time.sleep(0.2)
        return p

    def restore(self):
        r = self.wake("restore")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    # ---------------------------------------------------------------- Law 1: files
    def test_delete_is_kept_and_put_back_as_the_same_file(self):
        f = self.put("notes.md")
        before, ino = digest(f), os.stat(f).st_ino
        self.agent("rm notes.md")
        self.assertFalse(os.path.exists(f))
        self.restore()
        self.assertEqual(digest(f), before)
        self.assertEqual(os.stat(f).st_ino, ino, "a deleted file comes back as the very same file")

    def test_overwrite_truncate_and_append_are_undone(self):
        a, b, c = self.put("a.txt"), self.put("b.txt"), self.put("c.txt")
        want = {p: digest(p) for p in (a, b, c)}
        self.agent("echo new > a.txt; : > b.txt; echo more >> c.txt; truncate -s 0 a.txt")
        self.restore()
        for p, d in want.items():
            self.assertEqual(digest(p), d, p)

    def test_move_then_delete_comes_back_where_it_was(self):
        f = self.put("docs/plan.md")
        d = digest(f)
        self.agent("mv docs/plan.md /tmp/plan-moved-$$.md && rm /tmp/plan-moved-$$.md")
        self.restore()
        self.assertEqual(digest(f), d)

    def test_move_into_a_folder_that_does_not_exist(self):
        """The shape of the July 2025 incident: every move lands on the same name, each one replacing the last."""
        photos = [self.put(f"photos/IMG_{i}.jpg", os.urandom(2000).hex()) for i in range(8)]
        want = {p: digest(p) for p in photos}
        self.agent("for f in photos/*.jpg; do mv \"$f\" sorted; done")
        self.assertEqual(len(os.listdir(os.path.join(self.proj, "photos"))), 0)
        self.restore()
        for p, d in want.items():
            self.assertEqual(digest(p), d, p)

    def test_files_in_a_moved_folder_come_back_to_the_original_folder(self):
        a, b = self.put("src/a.py"), self.put("src/b.py")
        da, db = digest(a), digest(b)
        self.agent("mv src old_src && rm old_src/a.py && echo changed > old_src/b.py")
        self.restore()
        self.assertEqual(digest(a), da)
        self.assertEqual(digest(b), db)
        self.assertFalse(os.path.exists(os.path.join(self.proj, "old_src")))

    def test_writing_into_a_moved_file_keeps_the_original_content(self):
        f = self.put("config.env", "SECRET=keep\n")
        self.agent("mv config.env c2.env && echo DEBUG=1 > c2.env")
        self.restore()
        self.assertEqual(open(f).read(), "SECRET=keep\n")

    def test_restore_never_destroys_what_stood_in_the_way(self):
        f = self.put("report.txt", "the person's report\n")
        self.agent("echo the agent version > report.txt.new && mv report.txt.new report.txt")
        self.restore()
        self.assertEqual(open(f).read(), "the person's report\n")
        vault = os.path.join(self.env["WAKE_HOME"], "sessions")
        found = subprocess.run(["grep", "-rl", "the agent version", vault], capture_output=True, text=True).stdout
        self.assertIn("displaced", found, "the agent's version is moved aside into the vault, not deleted")

    def test_hard_links_and_symlinks(self):
        f = self.put("data.bin", "x" * 5000)
        os.link(f, os.path.join(self.proj, "data-link.bin"))
        os.symlink("data.bin", os.path.join(self.proj, "latest"))
        self.agent("rm data-link.bin latest")
        self.restore()
        self.assertTrue(os.path.islink(os.path.join(self.proj, "latest")))
        self.assertEqual(os.readlink(os.path.join(self.proj, "latest")), "data.bin")
        self.assertEqual(open(os.path.join(self.proj, "data-link.bin")).read(), "x" * 5000)

    def test_the_agent_destroys_its_own_things_freely(self):
        r = self.agent("mkdir -p build && for i in $(seq 1 50); do echo $i > build/$i.o; done; rm -rf build; "
                       "echo tmp > t.txt; mv t.txt u.txt; rm u.txt")
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = self.wake("report", "--json").stdout
        self.assertIn('"kept": []', rep.replace(" ", "").replace('"kept":[]', '"kept": []'))

    def test_wake_records_cannot_be_touched(self):
        r = self.agent('rm -rf "$WAKE_HOME"; echo exit=$? >&2; ls "$WAKE_HOME/sessions" >/dev/null && echo still-there >&2')
        self.assertIn("still-there", r.stderr)
        self.assertEqual(self.wake("report").returncode, 0)

    def test_adopted_file_becomes_yours(self):
        script = "echo keep me > made.txt; sleep 1.5; rm made.txt"
        p = subprocess.Popen([sys.executable, "-m", "wake", "run", "--quiet", "--", "bash", "-c", f"cd $P\n{script}"],
                             env=self.env, cwd=self.proj, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.8)
        r = self.wake("keep", os.path.join(self.proj, "made.txt"))
        self.assertEqual(r.returncode, 0, r.stderr)
        p.wait(timeout=30)
        self.restore()
        self.assertEqual(open(os.path.join(self.proj, "made.txt")).read(), "keep me\n")

    def test_identity_survives_inode_reuse(self):
        """Inode numbers are reused at once. Birth time tells the files apart, except for files created within
        one kernel clock tick of each other (a few milliseconds), which WAKE treats as one file (docs/LIMITS.md)."""
        from wake.ident import identity
        path = os.path.join(self.proj, "reuse.txt")
        keys, inodes = set(), set()
        for _ in range(5):
            open(path, "w").write("x")
            keys.add(identity(path).key)
            inodes.add(os.stat(path).st_ino)
            os.unlink(path)
            time.sleep(0.03)
        self.assertEqual(len(keys), 5, f"five files are five identities (inode numbers used: {len(inodes)})")

    # ---------------------------------------------------------------- Law 1: processes
    def test_signal_to_your_process_is_refused(self):
        p = self.person_process()
        r = self.agent(f"kill -TERM {p.pid}; echo rc=$? >&2")
        self.assertIn("rc=1", r.stderr)
        time.sleep(0.2)
        self.assertIsNone(p.poll(), "your process is still running")

    def test_kill_everything_reaches_only_the_agent(self):
        p = self.person_process()
        r = self.agent("sleep 300 & mine=$!; sleep 0.3; kill -9 -1; sleep 0.3; "
                       "if kill -0 $mine 2>/dev/null; then echo mine-alive >&2; else echo mine-gone >&2; fi")
        self.assertIn("mine-gone", r.stderr)
        self.assertIsNone(p.poll())

    def test_pkill_by_pattern_spares_your_process(self):
        p = self.person_process("sleep", "301")
        r = self.agent("sleep 301 & mine=$!; sleep 0.3; pkill -f '^sleep 301$'; sleep 0.3; "
                       "if kill -0 $mine 2>/dev/null; then echo mine-alive >&2; else echo mine-gone >&2; fi")
        self.assertIsNone(p.poll(), "your process matched the pattern and is still running")
        self.assertIn("mine-gone", r.stderr, "the agent's own process that matched is gone")
        self.assertIn("Operation not permitted", r.stderr, "the agent was told no")

    # ---------------------------------------------------------------- Law 2
    def test_stop_ends_a_detached_double_forked_loop(self):
        beat = os.path.join(self.root, "beat")
        script = f"setsid bash -c '(while true; do touch {beat}.$RANDOM; sleep 0.2; done) &' </dev/null >/dev/null 2>&1; sleep 300"
        p = subprocess.Popen([sys.executable, "-m", "wake", "run", "--quiet", "--keep-running", "--", "bash", "-c", script],
                             env=self.env, cwd=self.proj, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        self.wake("stop")
        p.wait(timeout=30)
        time.sleep(0.5)
        for f in os.listdir(self.root):
            if f.startswith("beat."):
                os.unlink(os.path.join(self.root, f))
        time.sleep(1.5)
        self.assertFalse([f for f in os.listdir(self.root) if f.startswith("beat.")], "nothing of the agent runs again")

    def test_stop_puts_back_your_start_up_file_and_removes_planted_ones(self):
        rc = os.path.join(self.home, ".bashrc")
        open(rc, "w").write("alias ll='ls -la'\n")
        hooks = os.path.join(self.proj, ".git", "hooks")
        os.makedirs(hooks)
        self.agent(f"echo 'run-me &' >> {rc}; printf '#!/bin/sh\\nrun-me\\n' > {hooks}/post-commit")
        self.assertEqual(open(rc).read(), "alias ll='ls -la'\n")
        self.assertFalse(os.path.exists(os.path.join(hooks, "post-commit")))

    def test_two_agents_cannot_destroy_each_others_work(self):
        a = subprocess.Popen([sys.executable, "-m", "wake", "run", "--quiet", "--keep-running", "--", "bash", "-c",
                              "cd $P && mkdir -p a && for i in 1 2 3; do echo A$i > a/$i.txt; done; exec -a agentA-server sleep 300"],
                             env=self.env, cwd=self.proj, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(1.2)
            script = os.path.join(self.root, "b.sh")
            open(script, "w").write("cd $P\npkill -f agentA-server\nrm -rf a\n")
            self.wake("run", "--quiet", "--", "bash", script)
            server = subprocess.run(["pgrep", "-f", "^agentA-server"], capture_output=True, text=True).stdout.strip()
            self.assertTrue(server, "agent A's server survives agent B")
            self.restore()                     # the latest session is B's: put back what B destroyed
            self.assertEqual(open(os.path.join(self.proj, "a", "2.txt")).read(), "A2\n")
        finally:
            for sid in self.wake("sessions").stdout.split():
                if sid[:8].isdigit():
                    self.wake("stop", sid)
            a.wait(timeout=30)

    @unittest.skipUnless(shutil.which("node"), "Node.js not installed")
    def test_a_node_agent(self):
        self.put("person.txt", "the person wrote this\n")
        p = self.person_process()
        js = os.path.join(self.root, "agent.mjs")
        open(js, "w").write("""
import fs from "node:fs/promises";
import { spawn } from "node:child_process";
await Promise.all(Array.from({length: 50}, (_, i) => fs.writeFile(`out_${i}.txt`, "x")));
await fs.rm("person.txt");
spawn("sleep", ["300"], {detached: true, stdio: "ignore"}).unref();
try { process.kill(Number(process.env.PERSON)); console.log("allowed"); } catch (e) { console.log(e.code); }
""")
        env = dict(self.env, PERSON=str(p.pid))
        r = subprocess.run([sys.executable, "-m", "wake", "run", "--quiet", "--", "node", js], env=env, cwd=self.proj,
                           capture_output=True, text=True, timeout=60)
        self.assertIn("EPERM", r.stdout)
        self.assertIsNone(p.poll())
        self.restore()
        self.assertEqual(open(os.path.join(self.proj, "person.txt")).read(), "the person wrote this\n")

    def test_whose_explains_with_the_chain(self):
        self.put("mine.txt")
        self.agent("echo made > made.txt; echo edit >> mine.txt")
        out = self.wake("whose", os.path.join(self.proj, "made.txt")).stdout
        self.assertIn("was made by the agent", out)
        out = self.wake("whose", os.path.join(self.proj, "mine.txt")).stdout
        self.assertIn("is yours", out)
        self.assertIn("Your original is kept", out)


if __name__ == "__main__":
    unittest.main()
