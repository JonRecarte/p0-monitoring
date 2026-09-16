"""Keeps the generated configuration in step with what is actually running.

The wizard configures once. Containers, though, come and go: one that starts later and
matches an existing rule should be measured without anyone opening a browser.

It does NOT decide what to monitor — the rule does that. It only makes reality and the
generated configuration agree, and says out loud what it did.
"""
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime

import docker_api
import generator
import rules
import state

INTERVAL = int(os.environ.get("RECONCILE_INTERVAL", "15"))
HISTORY = 25

log = logging.getLogger("reconciler")


class Reconciler:
    def __init__(self, interval=INTERVAL):
        self.interval = interval
        self.activity = deque(maxlen=HISTORY)
        self.last_run = None
        self.last_change = None
        self.ticks = 0
        self.enabled = interval > 0
        self._targets = None          # what matched on the previous pass
        self._probe = None
        self._attached = False        # app on the stack network, so it can reload Prometheus
        self._thread = None

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if not self.enabled:
            self._note("disabled (RECONCILE_INTERVAL=0)")
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="reconciler")
        self._thread.start()
        self._note(f"watching Docker every {self.interval}s")

    def _loop(self):
        while True:
            try:
                self.tick()
            except Exception as exc:                      # never let the loop die
                self._note(f"error: {exc}")
                log.warning("reconcile failed: %s", exc)
            time.sleep(self.interval)

    def _note(self, text):
        self.activity.appendleft({"at": datetime.now().strftime("%H:%M:%S"), "text": text})
        log.info("reconciler: %s", text)

    # ------------------------------------------------------------------- one pass
    def tick(self):
        s = state.load()
        self.ticks += 1
        self.last_run = datetime.now().strftime("%H:%M:%S")

        # Before the wizard has been completed there is nothing to keep in step, and
        # generating from an empty state would produce a stack that measures nothing.
        if not generator.is_installed(s):
            return

        # Once there is a stack to talk to, make sure the app can reach it by name.
        # Rebuilding the app creates a new container that is not on the stack network,
        # and without this the Prometheus reload fails with a DNS error.
        if not self._attached:
            for note in generator.ensure_self_attached():
                self._note(note)
            self._attached = True

        containers = docker_api.containers()
        matching = rules.evaluate(s.get("rules"), s.get("exclusions"), containers)
        probe = (s.get("probes") or [None])[0]

        # What the generated configuration depends on: who matches, under what name and
        # project, on which networks (the prober has to reach them), and the probe config.
        targets = {c["id"]: (c["target"], c["project"], tuple(c.get("networks") or []))
                   for c in matching}

        if targets == self._targets and probe == self._probe:
            return

        appeared = [targets[i][0] for i in targets if i not in (self._targets or {})]
        gone = [(self._targets or {})[i][0] for i in (self._targets or {}) if i not in targets]
        first = self._targets is None
        self._targets, self._probe = targets, probe

        if first:
            # First pass after start-up. Reconcile anyway: containers may have come or
            # gone while the app was down, and apply() only restarts what actually needs it.
            self._reconcile(s, f"start-up check · {len(targets)} target(s)")
            return

        what = []
        if appeared:
            what.append("appeared: " + ", ".join(sorted(appeared)))
        if gone:
            what.append("gone: " + ", ".join(sorted(gone)))
        if not what:
            what.append("target details changed")
        self._reconcile(s, " · ".join(what))

    def _reconcile(self, s, reason):
        self._note(reason)
        result = generator.generate(s)
        for note in generator.apply(s, result["changed"]):
            self._note("  " + note)
        self.last_change = datetime.now().strftime("%H:%M:%S")

    # ---------------------------------------------------------------------- status
    def snapshot(self):
        return {
            "enabled": self.enabled,
            "interval": self.interval,
            "ticks": self.ticks,
            "last_run": self.last_run,
            "last_change": self.last_change,
            "targets": len(self._targets or {}),
            "activity": list(self.activity),
        }
