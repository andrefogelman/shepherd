"""Tests for the worker budget hard-kill backstop (#B).

Pure tree-walk / target selection is tested with a fake process table. The real
SIGKILL path is proven against a harmless throwaway process tree tagged with a
unique marker, so only the test's own tree is ever signaled. Runnable with:
    python -m unittest tests.test_worker_watchdog
"""

from __future__ import annotations

import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shepherd_dev.supervisor import _KILLTREE_PERL, _swap_perl_killtree  # noqa: E402
from shepherd_dev.worker_watchdog import (  # noqa: E402
    WorkerWatchdog, _descendants, _kill_subtree, find_worker_pids,
)


class TreeSelection(unittest.TestCase):
    def test_descendants_walks_full_subtree(self):
        # 100 -> 200 -> 300, and 100 -> 400
        table = {
            100: (1, "python shepherd"),
            200: (100, "perl -e ... runner.mjs payload.json"),
            300: (200, "claude -p"),
            400: (100, "ssh root@host"),
            999: (1, "unrelated"),
        }
        self.assertEqual(_descendants(100, table), {200, 300, 400})
        self.assertEqual(_descendants(200, table), {300})

    def test_find_worker_selects_runner_subtree_only(self):
        table = {
            100: (1, "python shepherd"),                     # us
            200: (100, "perl -e alarm runner.mjs payload"),  # worker wrapper
            300: (200, "node .../runner.mjs .../payload"),   # node runner
            350: (300, "claude -p --model x"),               # the worker model
            400: (100, "ssh root@5.6.7.8 bash -lc ..."),     # gate warmup ssh — NOT worker
            500: (1, "claude"),                              # user's own session — NOT descendant
        }
        pids = find_worker_pids(100, table)
        self.assertIn(200, pids)
        self.assertIn(300, pids)
        self.assertIn(350, pids)
        self.assertNotIn(400, pids)   # ssh spared
        self.assertNotIn(500, pids)   # user's claude spared
        self.assertNotIn(100, pids)   # never ourselves

    def test_find_worker_empty_when_no_runner(self):
        table = {100: (1, "python"), 200: (100, "ssh host"), 300: (100, "cp -al a b")}
        self.assertEqual(find_worker_pids(100, table), set())

    def test_find_worker_matches_claude_perl_wrapper(self):
        # the Claude worker: perl `alarm; exec @ARGV` wrapping `claude -p`
        table = {
            100: (1, "python shepherd"),
            200: (100, "/usr/bin/perl -e alarm shift @ARGV; exec @ARGV or die 900 /usr/bin/claude -p ..."),
            300: (200, "claude -p --permission-mode bypassPermissions"),
            310: (300, "mcp-server-filesystem"),
            400: (100, "ssh root@h bash -lc ..."),   # gate warmup — spared
        }
        pids = find_worker_pids(100, table)
        self.assertEqual(pids, {200, 300, 310})       # perl + claude + mcp; ssh out


class KilltreePerl(unittest.TestCase):
    def test_swap_replaces_only_the_script_slot(self):
        base = ["/usr/bin/perl", "-e", "alarm shift @ARGV; exec @ARGV or die",
                "900", "/usr/bin/claude", "-p", "PROMPT", "--permission-mode", "bypassPermissions"]
        out = _swap_perl_killtree(base)
        self.assertEqual(out[2], _KILLTREE_PERL)          # script swapped
        self.assertEqual(out[3:], base[3:])                # budget + claude cmd preserved
        self.assertEqual(out[:2], ["/usr/bin/perl", "-e"])
        self.assertEqual(base[2], "alarm shift @ARGV; exec @ARGV or die")  # input untouched

    def test_swap_noop_when_not_perl(self):
        argv = ["node", "runner.mjs", "payload.json"]
        self.assertEqual(_swap_perl_killtree(argv), argv)

    def test_killtree_perl_reaps_the_group(self):
        marker = f"KT_{uuid.uuid4().hex[:8]}"
        # perl(budget=1) forks -> setsid -> exec sh; on SIGALRM kills the group
        proc = subprocess.Popen(
            ["/usr/bin/perl", "-e", _KILLTREE_PERL, "1",
             "sh", "-c", f": {marker}; sleep 30 & sleep 30 & wait"],
        )
        rc = proc.wait(timeout=8)
        self.assertEqual(rc, 124, "perl should exit 124 after the alarm killpg")
        time.sleep(0.4)
        out = subprocess.run(["ps", "-Ao", "command="], capture_output=True, text=True)
        self.assertNotIn(marker, out.stdout, "group children survived the alarm killpg")


class RealKill(unittest.TestCase):
    def test_kill_subtree_reaps_tagged_tree(self):
        marker = f"WD_TEST_{uuid.uuid4().hex[:8]}"
        # a parent sh that spawns two long sleepers, all carrying the marker in argv
        proc = subprocess.Popen(
            ["sh", "-c", f": {marker}; sleep 60 & sleep 60 & wait"],
        )
        time.sleep(0.7)  # let the children spawn
        table = {}
        out = subprocess.run(["ps", "-Ao", "pid=,ppid=,command="], capture_output=True, text=True)
        for line in out.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) >= 2:
                try:
                    table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
                except ValueError:
                    pass
        tagged = {pid for pid, (_pp, cmd) in table.items() if marker in cmd}
        self.assertTrue(tagged, "test tree not found")
        _kill_subtree(tagged | {p for pid in tagged for p in _descendants(pid, table)}, grace=0.3)
        proc.wait(timeout=5)
        time.sleep(0.3)
        out2 = subprocess.run(["ps", "-Ao", "pid=,command="], capture_output=True, text=True)
        self.assertNotIn(marker, out2.stdout, "tagged processes survived the kill")


class WatchdogLifecycle(unittest.TestCase):
    def test_cancel_before_deadline_never_fires(self):
        wd = WorkerWatchdog(budget_seconds=1, grace=0).start()  # would fire at ~1s
        wd.cancel()                                             # cancel immediately
        time.sleep(1.4)
        self.assertFalse(wd.fired)

    def test_fires_and_kills_when_not_cancelled(self):
        marker = f"WD_LIVE_{uuid.uuid4().hex[:8]}"
        # spawn a fake "runner.mjs" worker as OUR child so find_worker_pids matches
        proc = subprocess.Popen(["sh", "-c", f": runner.mjs {marker}; sleep 30"])
        wd = WorkerWatchdog(budget_seconds=1, grace=0).start()  # fires ~1s
        proc.wait(timeout=8)                                    # killed by the watchdog
        wd.cancel()
        self.assertTrue(wd.fired)


if __name__ == "__main__":
    unittest.main()
