"""From configuration to the files the stack reads.

Split by who is responsible for what:

  every machine   its own cloudprober.cfg, because only it can see its containers
  the hub only    prometheus.yml (one scrape block per machine) and Grafana's provisioning

Nothing here launches containers any more. The compose file is static and the user runs
it; what the app owns is the configuration inside.
"""
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request

from jinja2 import Environment, FileSystemLoader

import docker_api
import rules as rules_mod

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STACK_TEMPLATES = os.path.join(APP_DIR, "stack")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
GENERATED_DIR = os.path.join(DATA_DIR, "generated")
PROBER = os.environ.get("PROBER_NAME", "p0m-cloudprober")
APP = os.environ.get("APP_NAME", "p0m-app")
PROMETHEUS = os.environ.get("PROMETHEUS_NAME", "p0m-prometheus")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")

_env = Environment(loader=FileSystemLoader(STACK_TEMPLATES), keep_trailing_newline=True,
                   trim_blocks=True, lstrip_blocks=True)

# The wizard and the reconciler write the same files. Everything that does takes this.
LOCK = threading.RLock()


def _write(path, content):
    """Write only if the content changed. Returns True when it did."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path) as fh:
            if fh.read() == content:
                return False
    with open(path + ".tmp", "w") as fh:
        fh.write(content)
    os.replace(path + ".tmp", path)
    return True


def _probe_name(name):
    return re.sub(r"[^a-z0-9_]", "_", name.lower()) + "_health"


# --------------------------------------------------------------- every machine
def probes_for(config, matching):
    """One probe per matching container on THIS machine.

    Named after the container, which Docker guarantees unique: naming after the target
    would collide for scaled replicas, which share a service on purpose.
    """
    cfg = (config.get("probes") or [None])[0]
    if not cfg:
        return []
    out, seen = [], set()
    for c in matching:
        name = _probe_name(c["name"])
        host = c.get("probe_host") or c["name"]
        if name in seen or not host:
            continue
        seen.add(name)
        out.append({"name": name, "host": host,
                    "port": cfg.get("port", 80), "path": cfg.get("path", "/")})
    return out


# Prometheus runs as nobody and Grafana as its own user, so a directory Docker created
# for them is a directory they cannot write to. The app runs first — Prometheus waits on
# its healthcheck — and it runs as root, so this is the one place that can fix it.
STORAGE_OWNERS = {"prometheus": (65534, 65534), "grafana": (472, 0)}


def ensure_storage():
    """Prepare the directories where metrics go — only for installs that asked for them.

    Nothing happens on a default install, where the metrics are in a Docker volume and
    these directories would be litter. Compose passes the chosen paths in, so the app
    knows which case it is in rather than guessing.
    """
    notes = []
    for name, (uid, gid) in STORAGE_OWNERS.items():
        chosen = os.environ.get(f"{name.upper()}_DATA", "")
        # A value without a separator is a Docker volume name, not a path: nothing to do.
        if not chosen or "/" not in chosen:
            continue
        path = os.path.join(DATA_DIR, os.path.basename(chosen.rstrip("/")))
        try:
            os.makedirs(path, exist_ok=True)
            if os.stat(path).st_uid != uid:
                os.chown(path, uid, gid)
                notes.append(f"prepared {path} so {name} can write to it")
        except Exception as exc:
            notes.append(f"could not prepare {path} for {name}: {exc}")
    return notes


def ensure_local():
    """Make sure this machine has a cloudprober configuration, even an empty one.

    Without it the container exits at start-up and Docker restarts it forever, which is
    what a node looks like between joining and the hub being configured. Nothing is
    wrong in that window — there is simply nothing to probe yet — but a container in a
    restart loop says the opposite, and that is the kind of lie this project keeps
    finding and removing.
    """
    with LOCK:
        dest = os.path.join(GENERATED_DIR, "cloudprober.cfg")
        if os.path.exists(dest):
            return False
        _write(dest, _env.get_template("cloudprober.cfg.j2").render(probes=[]))
        return True


def generate_local(config):
    """Write what this machine is responsible for. Returns a summary."""
    with LOCK:
        running = [c for c in docker_api.containers() if c["state"] == "running"]
        matching = rules_mod.evaluate(config.get("rules"), config.get("exclusions"), running)
        probes = probes_for(config, matching)
        changed = set()
        dest = os.path.join(GENERATED_DIR, "cloudprober.cfg")
        if _write(dest, _env.get_template("cloudprober.cfg.j2").render(probes=probes)):
            changed.add("cloudprober")
        return {"matching": matching, "probes": probes, "changed": changed,
                "total_containers": len(running)}


def apply_local(config, changed):
    """Make the pieces on this machine pick up what changed.

      cloudprober  reads its config only at startup, so it must be restarted
      the prober   must reach targets that may sit on networks of their own
    """
    with LOCK:
        notes = []
        try:
            running = {c["name"]: c for c in docker_api.containers()}
        except Exception as exc:
            return [f"could not talk to Docker: {exc}"]

        notes.extend(_attach_prober(config, running))

        # What matters is not whether the file changed on this pass, but whether it is
        # newer than the process that read it. That also heals drift after a restart.
        cfg = os.path.join(GENERATED_DIR, "cloudprober.cfg")
        if PROBER in running and os.path.exists(cfg):
            started = docker_api.started_at(running[PROBER]["id"])
            stale = started is not None and os.path.getmtime(cfg) > started
            if "cloudprober" in changed or stale:
                docker_api.restart(running[PROBER]["id"])
                notes.append("cloudprober restarted: its config was newer than the process")
            else:
                notes.append("cloudprober already running the current config")
        elif os.path.exists(cfg):
            notes.append("cloudprober is not running: nothing to restart")
        return notes


def attach(container_name, networks, running=None):
    """Put a container on every one of these networks. Idempotent, and says what it did.

    Used for two things that are the same problem: the prober has to reach what it
    probes, and the hub has to reach a cluster whose API server is a container here.
    Also heals a rebuild, which loses every network but the compose one.
    """
    notes = []
    if not networks:
        return notes
    try:
        if running is None:
            running = {c["name"]: c for c in docker_api.containers()}
        target = running.get(container_name)
        if not target:
            return [f"{container_name} is not running: cannot attach it to "
                    + ", ".join(sorted(networks))]
        mine = set(target.get("networks") or [])
        for net in sorted(set(networks) - mine):
            docker_api.connect(net, target["id"])
            notes.append(f"{container_name} attached to network {net}")
    except Exception as exc:
        notes.append(f"could not attach {container_name}: {exc}")
    return notes


def _attach_prober(config, running):
    """Attach the prober to every network its targets live on, so QoS works without
    the user having to touch their own containers."""
    notes = []
    prober = running.get(PROBER)
    if not prober:
        return ["prober not running: nothing to attach"]
    try:
        matching = rules_mod.evaluate(config.get("rules"), config.get("exclusions"),
                                      [c for c in running.values() if c["state"] == "running"])
        wanted = set()
        for c in matching:
            wanted.update(c.get("networks") or [])
        mine = set(prober.get("networks") or [])
        for net in sorted(wanted - mine):
            docker_api.connect(net, prober["id"])
            notes.append(f"prober attached to network {net}")
        if not (wanted - mine):
            notes.append("prober already reaches every target")
    except Exception as exc:
        notes.append(f"could not attach the prober: {exc}")
    return notes


# ------------------------------------------------------------------- hub only
def generate_hub(data):
    """Write what only the hub owns: the scrape configuration and the dashboards."""
    with LOCK:
        machines = data.get("machines") or []
        changed = set()
        written = []
        for template, dest, tag in [
            ("prometheus.yml.j2", os.path.join(GENERATED_DIR, "prometheus.yml"), "prometheus"),
            ("grafana-datasource.yml.j2",
             os.path.join(GENERATED_DIR, "grafana/datasources/prometheus.yml"), "grafana"),
            ("grafana-provider.yml.j2",
             os.path.join(GENERATED_DIR, "grafana/dashboards/provider.yml"), "grafana"),
        ]:
            if _write(dest, _env.get_template(template).render(machines=machines, s=data)):
                changed.add(tag)
            written.append(dest)

        # the dashboard carries Grafana's {{pod}}, which is not Jinja: copied, not rendered
        dash = os.path.join(GENERATED_DIR, "grafana/dashboards/overview.json")
        os.makedirs(os.path.dirname(dash), exist_ok=True)
        shutil.copy(os.path.join(STACK_TEMPLATES, "overview.json"), dash)
        written.append(dash)
        return {"changed": changed, "files": written}


def reload_prometheus():
    try:
        req = urllib.request.Request(f"{PROMETHEUS_URL}/-/reload", method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return f"prometheus reloaded (HTTP {r.status})"
    except Exception as exc:
        return f"could not reload prometheus: {exc}"


# ---------------------------------------------------------------------- status
def is_installed(data=None):
    """True once the user has configured something and declared at least one machine."""
    if data is None:
        import state as state_module
        data = state_module.load()
    return bool(data.get("rules")) and bool(data.get("machines"))


def stack_status():
    """Which pieces are running on THIS machine."""
    expected = {"p0m-cadvisor", "p0m-node-exporter", "p0m-kepler", "p0m-cloudprober"}
    hub_only = {"p0m-prometheus", "p0m-grafana"}
    try:
        alive = {c["name"]: c["state"] for c in docker_api.containers(all_states=True)}
    except Exception:
        alive = {}
    out = {n: alive.get(n, "absent") for n in sorted(expected)}
    for n in sorted(hub_only):
        if n in alive:
            out[n] = alive[n]
    return out
