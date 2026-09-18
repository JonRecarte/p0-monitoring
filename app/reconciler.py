"""Keeps each machine's configuration in step with what is actually running on it.

The same loop runs on every machine. The only difference is where the configuration
comes from: the hub has it, a node fetches it. That is what makes a single machine the
N=1 case of the same product rather than a separate one.

It does NOT decide what to monitor — the rule does. It makes reality and the generated
configuration agree, and says out loud what it did.
"""
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime

import docker_api
import generator
import inventory
import k8s_install
import role
import rules as rules_mod
import state as state_mod

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
        self.config = None            # what this machine has been told to apply
        self.config_error = None      # a node that cannot reach its hub
        self._targets = None
        self._probe = None
        self._machines = None
        self._cluster_probes = {}
        self._forwards = {}
        self._thread = None

    def forget(self):
        """Drop every fingerprint, so the next pass treats everything as new.

        Needed after the state is wiped: the loop only acts when something differs from
        the last pass, and it would otherwise compare an empty configuration against
        what it remembers and conclude, correctly but uselessly, that nothing changed.
        """
        self._targets = self._probe = self._machines = None
        self._cluster_probes, self._forwards = {}, {}
        self._note("state reset: starting from nothing")

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if not self.enabled:
            self._note("disabled (RECONCILE_INTERVAL=0)")
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="reconciler")
        self._thread.start()
        self._note(f"{role.describe()} · watching Docker every {self.interval}s")

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

    # ------------------------------------------------------------- configuration
    def _fetch_config(self):
        """A node asks its hub. On failure it keeps the last configuration it had:
        losing contact must not silently stop the measuring."""
        try:
            with urllib.request.urlopen(f"{role.HUB_URL}/api/config", timeout=8) as r:
                cfg = json.load(r)
            if self.config_error:
                self._note(f"hub reachable again at {role.HUB_URL}")
            self.config_error = None
            return cfg
        except Exception as exc:
            if not self.config_error:
                self._note(f"cannot reach the hub at {role.HUB_URL}: {exc}")
            self.config_error = str(exc)
            return self.config          # keep applying what we already knew

    def current_config(self):
        if role.IS_HUB:
            s = state_mod.load()
            return {k: s.get(k) for k in ("rules", "exclusions", "probes")}
        return self._fetch_config()

    # ------------------------------------------------------------------- one pass
    def tick(self):
        self.ticks += 1
        self.last_run = datetime.now().strftime("%H:%M:%S")

        cfg = self.current_config()
        self.config = cfg
        if not cfg or not cfg.get("rules"):
            return          # nothing configured yet: generating now would measure nothing

        if role.IS_HUB:
            self._hub_side()

        running = [c for c in docker_api.containers() if c["state"] == "running"]
        matching = rules_mod.evaluate(cfg.get("rules"), cfg.get("exclusions"), running)
        probe = (cfg.get("probes") or [None])[0]

        # What the generated configuration depends on: who matches, under what name,
        # on which networks (the prober has to reach them), and the probe settings.
        targets = {c["id"]: (c["name"], tuple(c.get("networks") or [])) for c in matching}
        if targets == self._targets and probe == self._probe:
            return

        appeared = [targets[i][0] for i in targets if i not in (self._targets or {})]
        gone = [(self._targets or {})[i][0] for i in (self._targets or {}) if i not in targets]
        first = self._targets is None
        self._targets, self._probe = targets, probe

        if first:
            # First pass after start-up: reconcile anyway. Containers may have come or
            # gone while the app was down, and apply only restarts what needs it.
            self._reconcile(cfg, f"start-up check · {len(targets)} target(s)")
            return

        what = []
        if appeared:
            what.append("appeared: " + ", ".join(sorted(appeared)))
        if gone:
            what.append("gone: " + ", ".join(sorted(gone)))
        self._reconcile(cfg, " · ".join(what) or "target details changed")

    def _hub_side(self):
        """The scrape configuration only changes when the list of machines does."""
        s = state_mod.load()
        self._clusters_side(s)
        machines = json.dumps(s.get("machines") or [], sort_keys=True)
        if machines == self._machines:
            return
        first = self._machines is None
        self._machines = machines
        # The hub has to be on the networks of any machine that lives on one here. Doing
        # it every time the list changes, and on the first pass, is what survives a
        # rebuild — which drops every network but the compose one.
        for container in (generator.APP, generator.PROMETHEUS):
            for note in generator.attach(container, state_mod.networks(s)):
                self._note(note)
        result = generator.generate_hub(s)
        if result["changed"] or first:
            if not first:
                self._note("machine list changed: scrape configuration rewritten")
            if "prometheus" in result["changed"]:
                self._note("  " + generator.reload_prometheus())

    def _republish(self, s):
        """Ask again for the forwards that reach a cluster through a node.

        A forward lives in the node's process. Restart that node and it is gone, and the
        cluster behind it goes silent for a reason nobody could guess from the dashboard.
        Asking on every pass costs one request and is idempotent — the node returns the
        port it already has rather than opening another.
        """
        for m in s.get("machines") or []:
            if not m.get("via") or not m.get("via_address"):
                continue
            node = state_mod.find(s, m["via"])
            if not node:
                continue
            url = f"http://{node['address']}:{state_mod.ports(node)['app']}/api/expose"
            payload = json.dumps({"name": m["name"], "address": m["via_address"]}).encode()
            try:
                req = urllib.request.Request(
                    url, data=payload, method="POST",
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    answer = json.load(r)
            except Exception as exc:
                if self._forwards.get(m["name"]) != "down":
                    self._note(f"{m['name']}: {m['via']} is not answering about its "
                               f"forward: {exc}")
                    self._forwards[m["name"]] = "down"
                continue
            state_now = f"{answer.get('ok')}:{answer.get('port')}"
            if state_now != self._forwards.get(m["name"]):
                self._forwards[m["name"]] = state_now
                self._note(f"{m['name']}: reached through {m['via']} on port "
                           f"{answer.get('port')}" if answer.get("ok") else
                           f"{m['name']}: {m['via']} can no longer see it")

    def _clusters_side(self, s):
        """Keep each cluster's probes in step with the pods that match.

        The same job the loop does for this machine's cloudprober, once per cluster. A
        pod's address is its IP and a new pod gets a new one, so this is not a rare
        event: without it the prober keeps aiming at addresses nobody answers on, and
        QoS reads as an outage that is really a stale configuration.

        Only the probes are re-applied, not the collectors: the DaemonSets do not depend
        on which pods were picked, and re-applying them every fifteen seconds would be
        noise in somebody else's cluster.
        """
        self._republish(s)
        cfg = {k: s.get(k) for k in ("rules", "exclusions", "probes")}
        if not cfg.get("rules") or not cfg.get("probes"):
            return
        for m in s.get("machines") or []:
            if state_mod.kind(m) != "kubernetes":
                continue
            name = m["name"]
            try:
                pods = inventory.for_machine(m)
                matching = rules_mod.evaluate(cfg.get("rules"), cfg.get("exclusions"), pods)
                probes = generator.probes_for(cfg, matching)
            except Exception as exc:
                if self._cluster_probes.get(name) != "unreachable":
                    self._note(f"{name}: cannot list pods: {exc}")
                    self._cluster_probes[name] = "unreachable"
                continue
            fingerprint = json.dumps(probes, sort_keys=True)
            if fingerprint == self._cluster_probes.get(name):
                continue
            first = name not in self._cluster_probes
            self._cluster_probes[name] = fingerprint
            token = state_mod.read_token(name)
            ok, notes = k8s_install.install(m["address"], token, name, probes=probes,
                                            only=k8s_install.PROBE_OBJECTS)
            self._note(f"{name}: {len(probes)} probe(s) "
                       + ("installed" if first else "changed, reapplied")
                       + ("" if ok else " — WITH FAILURES"))
            for note in notes:
                if note.startswith("FAILED"):
                    self._note("  " + note)

    def _reconcile(self, cfg, reason):
        self._note(reason)
        result = generator.generate_local(cfg)
        for note in generator.apply_local(cfg, result["changed"]):
            self._note("  " + note)
        self.last_change = datetime.now().strftime("%H:%M:%S")

    # ---------------------------------------------------------------------- status
    def snapshot(self):
        return {
            "role": role.describe(),
            "enabled": self.enabled,
            "interval": self.interval,
            "ticks": self.ticks,
            "last_run": self.last_run,
            "last_change": self.last_change,
            "targets": len(self._targets or {}),
            "config_error": self.config_error,
            "activity": list(self.activity),
        }
