"""Generator: from config.yaml to the stack files.

Everything it produces is DERIVED. It can be deleted and regenerated without losing anything.
"""
import os, re, shutil, subprocess

from jinja2 import Environment, FileSystemLoader

import docker_api, rules

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STACK_TEMPLATES = os.path.join(APP_DIR, "stack")
# HOST paths (what the stack containers see) and the app container's own path
DATA_DIR_HOST = os.environ.get("DATA_DIR_HOST", "/opt/p0-monitoring")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
GENERATED_DIR = os.path.join(DATA_DIR, "generated")
TEXTFILE_DIR = os.path.join(DATA_DIR, "textfile")

_env = Environment(loader=FileSystemLoader(STACK_TEMPLATES), keep_trailing_newline=True,
                   trim_blocks=True, lstrip_blocks=True)

# Docker labels that travel into the metrics. Each one added here multiplies series.
BASE_LABELS = ["com.docker.compose.project", "com.docker.compose.service"]


def _write(path, content):
    """Write only if the content changed. Returns True when it did."""
    if os.path.exists(path):
        with open(path) as fh:
            if fh.read() == content:
                return False
    with open(path + ".tmp", "w") as fh:
        fh.write(content)
    os.replace(path + ".tmp", path)
    return True


def _probe_name(target):
    return re.sub(r"[^a-z0-9_]", "_", target.lower()) + "_health"


def probes_for(state, matching):
    """One probe per matching target.

    The wizard asks about QoS ONCE for the whole selection, so the config applies to
    every matching target. It used to be keyed by rule index, which silently left
    targets without a probe as soon as a selection produced more than one rule.
    """
    cfg = (state.get("probes") or [None])[0]
    if not cfg:
        return []
    return [{
        "name": _probe_name(c["target"]),
        "host": c["name"],            # the container name resolves on the Docker network
        "port": cfg.get("port", 80),
        "path": cfg.get("path", "/"),
    } for c in matching]


def target_info(matching):
    """The inventory series that resolves identity for energy.

    Without Kubernetes, Kepler does not know a container's name: it labels everything
    system_processes and only container_id tells them apart. This series closes that gap,
    and doubles as the filter (pseudo-containers are not listed here).
    """
    lines = ["# HELP target_info Identity of each monitored target",
             "# TYPE target_info gauge"]
    for c in matching:
        labels = [f'container_id="{c["id"]}"', f'pod="{c["target"]}"',
                  f'namespace="{c["project"]}"']
        for key in ("app", "role"):
            if c.get(key):
                labels.append(f'{key}="{c[key]}"')
        lines.append("target_info{" + ",".join(labels) + "} 1")
    return "\n".join(lines) + "\n"


def container_inventory(containers, monitored_ids):
    """The inventory series. In Kubernetes this is what kube-state-metrics gives you;
    on Docker nobody publishes it, so the app does — state, image and ip per container.
    Without it there is no way to count stopped containers or build an inventory table.
    """
    lines = ["# HELP container_info Inventory of every container on this host",
             "# TYPE container_info gauge"]
    for c in containers:
        labels = [f'container_id="{c["id"]}"', f'pod="{c["target"]}"',
                  f'namespace="{c["project"]}"', f'image="{c["image"]}"',
                  f'state="{c["state"]}"', f'ip="{c["ip"]}"',
                  f'monitored="{"1" if c["id"] in monitored_ids else "0"}"']
        lines.append("container_info{" + ",".join(labels) + "} 1")
    return "\n".join(lines) + "\n"


def generate(state):
    """Write the stack files. Returns a summary of what was done."""
    containers = docker_api.containers()
    matching = rules.evaluate(state.get("rules"), state.get("exclusions"), containers)
    probes = probes_for(state, matching)

    keys = list(BASE_LABELS)
    for r in state.get("rules") or []:
        if r.get("type") == "label" and r.get("key") and r["key"] not in keys:
            keys.append(r["key"])

    ctx = {"s": {**state, "probes": probes}, "probes": probes,
           "label_list": ",".join(keys), "data_dir": DATA_DIR_HOST}

    os.makedirs(os.path.join(GENERATED_DIR, "grafana", "datasources"), exist_ok=True)
    os.makedirs(os.path.join(GENERATED_DIR, "grafana", "dashboards"), exist_ok=True)
    os.makedirs(TEXTFILE_DIR, exist_ok=True)

    written, changed = [], set()
    for template, dest, tag in [
        ("docker-compose.yml.j2",     os.path.join(GENERATED_DIR, "docker-compose.yml"), "compose"),
        ("prometheus.yml.j2",         os.path.join(GENERATED_DIR, "prometheus.yml"), "prometheus"),
        ("grafana-datasource.yml.j2", os.path.join(GENERATED_DIR, "grafana/datasources/prometheus.yml"), "grafana"),
        ("grafana-provider.yml.j2",   os.path.join(GENERATED_DIR, "grafana/dashboards/provider.yml"), "grafana"),
    ]:
        if _write(dest, _env.get_template(template).render(**ctx)):
            changed.add(tag)
        written.append(dest)

    if probes:
        dest = os.path.join(GENERATED_DIR, "cloudprober.cfg")
        if _write(dest, _env.get_template("cloudprober.cfg.j2").render(**ctx)):
            changed.add("cloudprober")
        written.append(dest)

    # the dashboard is static: it carries Grafana's {{pod}}, which is not Jinja
    shutil.copy(os.path.join(STACK_TEMPLATES, "overview.json"),
                os.path.join(GENERATED_DIR, "grafana/dashboards/overview.json"))

    ti_path = os.path.join(TEXTFILE_DIR, "target_info.prom")
    if _write(ti_path, target_info(matching)):
        changed.add("target_info")
    os.chmod(ti_path, 0o644)
    written.append(ti_path)

    inv_path = os.path.join(TEXTFILE_DIR, "container_info.prom")
    everything = docker_api.containers(all_states=True)
    if _write(inv_path, container_inventory(everything, {c["id"] for c in matching})):
        changed.add("container_info")
    os.chmod(inv_path, 0o644)
    written.append(inv_path)

    return {"matching": matching, "probes": probes, "files": written, "changed": changed,
            "total_containers": len(containers)}


NETWORK = os.environ.get("NETWORK", "p0m-net")
PROBER = "p0m-cloudprober"


def launch(state=None):
    """Bring the stack up. Returns (ok, output).

    Three steps, in this order:
      1. make sure the app's own network exists (compose declares it external)
      2. docker compose up
      3. attach the prober to the networks the targets already live on, so the user
         does not have to touch their containers for QoS to work
    """
    notes = []
    _, created = docker_api.ensure_network(NETWORK)
    notes.append(f"network {NETWORK}: {'created' if created else 'already present'}")

    # the app joins its own network so it can talk to prometheus by name later
    mine = docker_api.self_container("p0m-app")
    if mine and NETWORK not in docker_api.networks_of(mine):
        docker_api.connect(NETWORK, mine)
        notes.append(f"app attached to network {NETWORK}")

    r = subprocess.run(
        ["docker", "compose", "-f", os.path.join(GENERATED_DIR, "docker-compose.yml"),
         "up", "-d", "--remove-orphans"],
        capture_output=True, text=True, timeout=300)
    output = (r.stdout + r.stderr)[-4000:]
    if r.returncode != 0:
        return False, "\n".join(notes) + "\n" + output

    notes.extend(apply(state, {"cloudprober", "prometheus"} if state else set()))
    return True, "\n".join(notes) + "\n" + output


def apply(state, changed):
    """React to what changed. Writing a file is not enough: someone has to read it.

      cloudprober  -> reads its config only at startup, so it must be restarted
      prometheus   -> supports a hot reload
      target_info  -> nothing: node-exporter re-reads the textfile on every scrape
      networks     -> the prober must reach any target that appeared on a new network
    """
    notes = []
    running = {c["name"]: c["id"] for c in docker_api.containers()}

    notes.extend(_attach_prober(state))

    # cloudprober only reads its config at startup, so what matters is not whether the
    # file changed on this pass but whether it is newer than the running process.
    cfg = os.path.join(GENERATED_DIR, "cloudprober.cfg")
    if PROBER in running and os.path.exists(cfg):
        started = docker_api.started_at(running[PROBER])
        stale = started is not None and os.path.getmtime(cfg) > started
        if "cloudprober" in changed or stale:
            docker_api.restart(running[PROBER])
            notes.append("cloudprober restarted: its config was newer than the process")
        else:
            notes.append("cloudprober already running the current config")
    elif os.path.exists(cfg):
        notes.append("cloudprober is not running: nothing to restart")

    if "prometheus" in changed:
        notes.append(_reload_prometheus())

    if not notes:
        notes.append("nothing to do: everything already current")
    return notes


def _reload_prometheus():
    """Hot reload. The app attaches itself to its own network so it can reach by name."""
    import urllib.error, urllib.request
    try:
        req = urllib.request.Request("http://prometheus:9090/-/reload", method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return f"prometheus reloaded (HTTP {r.status})"
    except Exception as exc:
        return f"could not reload prometheus: {exc}"


def _attach_prober(state):
    """Attach the prober to every network its targets are on."""
    notes = []
    if state is None:
        return notes
    try:
        containers = docker_api.containers()
        matching = rules.evaluate(state.get("rules"), state.get("exclusions"), containers)
        prober = next((c for c in containers if c["name"] == PROBER), None)
        if not prober:
            return ["prober not running: nothing to attach"]
        wanted = set()
        for c in matching:
            wanted.update(docker_api.networks_of(c["id"]))
        mine = set(docker_api.networks_of(prober["id"]))
        for net in sorted(wanted - mine):
            docker_api.connect(net, prober["id"])
            notes.append(f"prober attached to network {net}")
        if not (wanted - mine):
            notes.append("prober already reaches every target")
    except Exception as exc:
        notes.append(f"could not attach the prober: {exc}")
    return notes


def is_installed(state=None):
    """True once the user has actually configured and provisioned something.

    Checking only for generated files was wrong: they can exist with an empty state
    (a Regenerate with nothing configured writes them), and then the navigation
    offered a status page for a monitoring stack that watched nothing.
    """
    if state is None:
        import state as state_module
        state = state_module.load()
    return bool(state.get("rules")) and \
        os.path.exists(os.path.join(GENERATED_DIR, "docker-compose.yml"))


def stack_status():
    """Which stack pieces are running."""
    expected = {"p0m-cadvisor", "p0m-node-exporter", "p0m-kepler", "p0m-prometheus", "p0m-grafana",
                "p0m-cloudprober"}
    try:
        alive = {c["name"]: c["state"] for c in docker_api.containers()}
    except Exception:
        alive = {}
    return {n: alive.get(n, "absent") for n in sorted(expected)}
