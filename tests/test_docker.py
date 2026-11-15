"""The Docker adapter, against a stand-in daemon (fakedocker.py). Linux only, like the laws.

The daemon acts for the agent, so WAKE checks what the agent asks of it: its own containers, volumes and
networks it may destroy; yours it may not; when it is stopped, its containers stop with it.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakedocker import FakeDocker  # noqa: E402

LINUX = sys.platform.startswith("linux")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The agent's side: the HTTP code of every request goes to $OUT, one per line.
D = """D() { curl -s -o /dev/null -w '%{http_code}\\n' --unix-socket "${SOCK:-${DOCKER_HOST#unix://}}" "$@" >> "$OUT"; }
J='Content-Type: application/json'
"""


@unittest.skipUnless(LINUX and shutil.which("curl"), "WAKE runs on Linux; the agent here speaks with curl")
class Docker(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wake-dk-")
        self.home = os.path.join(self.root, "home")
        self.proj = os.path.join(self.home, "project")
        os.makedirs(self.proj)
        self.sock = os.path.join(self.root, "docker.sock")
        self.out = os.path.join(self.root, "codes")
        self.fake = FakeDocker(self.sock).__enter__()
        self.env = dict(os.environ, HOME=self.home, WAKE_HOME=os.path.join(self.root, "wakehome"),
                        PYTHONPATH=REPO, P=self.proj, OUT=self.out, REAL=self.sock,
                        DOCKER_HOST=f"unix://{self.sock}")

    def tearDown(self):
        self.fake.__exit__()
        shutil.rmtree(self.root, ignore_errors=True)

    def wake(self, *args, timeout=60):
        return subprocess.run([sys.executable, "-m", "wake", *args], env=self.env, capture_output=True, text=True,
                              timeout=timeout, cwd=self.proj)

    def agent(self, script):
        r = self.wake("run", "--quiet", "--", "bash", "-c", f"cd $P\n{D}{script}")
        return r

    def codes(self):
        with open(self.out) as f:
            return [int(x) for x in f.read().split()]

    def test_your_containers_and_volumes_are_refused_its_own_are_not(self):
        db = self.fake.person_container("db")
        self.fake.person_volume("pgdata")
        self.agent("""
D -X POST -H "$J" -d '{"Image":"alpine"}' 'http://d/v1.47/containers/create?name=scratch'
D -X POST http://d/v1.47/containers/scratch/start
D -X POST http://d/v1.47/containers/db/stop
D -X DELETE 'http://d/v1.47/containers/db?force=1'
D -X DELETE http://d/v1.47/volumes/pgdata
D -X POST -H "$J" -d '{}' http://d/v1.47/containers/db/exec
D -X POST http://d/v1.47/containers/scratch/stop
D -X POST http://d/v1.47/containers/prune
""")
        self.assertEqual(self.codes(), [201, 204, 403, 403, 403, 403, 204, 200])
        self.assertIn(db, self.fake.containers, "your container is still there")
        self.assertTrue(self.fake.containers[db]["State"]["Running"], "and still running")
        self.assertIn("pgdata", self.fake.volumes, "your volume is still there")
        self.assertIsNone(self.fake.find("scratch"), "the agent's own stopped container went with the prune")
        report = self.wake("report").stdout
        self.assertIn("asked the Docker daemon to stop container db", report)

    def test_a_direct_connection_to_the_daemon_goes_through_wake(self):
        db = self.fake.person_container("db")
        self.agent("""
SOCK="$REAL" D -X DELETE 'http://d/containers/db?force=1'
SOCK="$REAL" D -X POST http://d/containers/db/kill
""")
        self.assertEqual(self.codes(), [403, 403], "the request reached WAKE, not the daemon")
        self.assertTrue(self.fake.containers[db]["State"]["Running"])

    def test_your_folders_and_volumes_are_read_only_to_its_containers(self):
        self.fake.person_volume("pgdata")
        self.agent("""
mkdir -p build
BODY=$(printf '{"Image":"node","HostConfig":{"Binds":["%s:/w","%s/build:/b","pgdata:/data","fresh:/n"]}}' "$P" "$P")
D -X POST -H "$J" -d "$BODY" 'http://d/containers/create?name=dev'
D -X POST -H "$J" -d '{"Image":"alpine","HostConfig":{"Privileged":true}}' 'http://d/containers/create?name=root'
D -X POST -H "$J" -d '{"Image":"alpine","HostConfig":{"PidMode":"host"}}' 'http://d/containers/create?name=pid'
D -X POST -H "$J" -d "{\\"Image\\":\\"docker\\",\\"HostConfig\\":{\\"Binds\\":[\\"$REAL:/var/run/docker.sock\\"]}}" 'http://d/containers/create?name=dind'
D -X DELETE http://d/volumes/fresh
""")
        self.assertEqual(self.codes(), [201, 403, 403, 403, 204])
        binds = self.fake.find("dev")["HostConfig"]["Binds"]
        self.assertEqual(binds, [f"{self.proj}:/w:ro", f"{self.proj}/build:/b", "pgdata:/data:ro", "fresh:/n"],
                         "your folder and volume read-only; the agent's own folder and new volume as asked")
        self.assertIsNone(self.fake.find("root"))

    def test_stop_stops_its_containers_and_their_restart_policy(self):
        db = self.fake.person_container("db")
        script = f"cd $P\n{D}" + """
D -X POST -H "$J" -d '{"Image":"nginx","HostConfig":{"RestartPolicy":{"Name":"always"}}}' 'http://d/containers/create?name=web'
D -X POST http://d/containers/web/start
sleep 300
"""
        p = subprocess.Popen([sys.executable, "-m", "wake", "run", "--quiet", "--keep-running", "--", "bash", "-c", script],
                             env=self.env, cwd=self.proj, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                web = self.fake.find("web")
                if web and web["State"]["Running"]:
                    break
                time.sleep(0.1)
            self.assertTrue(self.fake.find("web")["State"]["Running"])
            r = self.wake("stop")
            self.assertIn("stopped 1 container it started (web)", r.stdout + r.stderr)
            p.wait(timeout=30)
        finally:
            if p.poll() is None:
                p.kill()
        web = self.fake.find("web")
        self.assertFalse(web["State"]["Running"], "the agent's container stopped with it")
        self.assertEqual(web["HostConfig"]["RestartPolicy"]["Name"], "no", "and will not come back")
        self.assertTrue(self.fake.containers[db]["State"]["Running"], "yours kept running")


if __name__ == "__main__":
    unittest.main()
