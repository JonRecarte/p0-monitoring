"""The app: the wizard and the control plane on a hub, the local agent on a node.

The app is NOT in the data path. If it dies, the collectors keep measuring.
"""
import os

from flask import (Flask, Response, jsonify, redirect, render_template, request,
                   url_for)

import capabilities
import docker_api
import generator
import inventory
import k8s_api
import k8s_install
import reconciler
import role
import rules
import state

app = Flask(__name__, template_folder="templates", static_folder="static")

# Watches Docker and keeps this machine's configuration in step. Runs in both roles:
# the only difference is where the configuration comes from.
RECONCILER = reconciler.Reconciler()
RECONCILER.start()

# The hub writes its scrape configuration up front, so Prometheus has something valid
# to start with. Its healthcheck is what Prometheus waits on.
if role.IS_HUB:
    try:
        generator.generate_hub(state.load())
    except Exception:                                   # never block start-up on this
        pass


def _s():
    return state.load()


# What this machine publishes on the host. Compose passes these in, so the links the app
# hands out stay right when somebody moves a port to dodge a collision.
MY_PORTS = {name: os.environ.get(var, str(default))
            for name, var, default in [
                ("app", "APP_PORT", 8000), ("cadvisor", "CADVISOR_PORT", 8080),
                ("node", "NODE_EXPORTER_PORT", 9100), ("kepler", "KEPLER_PORT", 9102),
                ("cloudprober", "CLOUDPROBER_PORT", 9313),
                ("prometheus", "PROMETHEUS_PORT", 9090), ("grafana", "GRAFANA_PORT", 3000)]}


@app.context_processor
def _globals():
    return {"installed": role.IS_HUB and generator.is_installed(state.load()),
            "is_hub": role.IS_HUB, "role_text": role.describe(),
            "my_ports": MY_PORTS}


def hub_only(view):
    """A node has no wizard: it applies what the hub tells it."""
    def wrapped(*a, **kw):
        if role.IS_NODE:
            return render_template("node.html", hub=role.HUB_URL,
                                   snapshot=RECONCILER.snapshot()), 200
        return view(*a, **kw)
    wrapped.__name__ = view.__name__
    return wrapped


# --------------------------------------------------------------- step 1: this machine
# There used to be one global "environment" here, chosen once for the whole
# installation. It was wrong: machines can be different things, and the same host can be
# a Docker host AND run a cluster. What a machine is now belongs to the machine, and is
# asked on the Machines screen. The hub is always a Docker host — it runs as a compose.
@app.route("/", methods=["GET", "POST"])
@hub_only
def step1_environment():
    s = _s()
    if request.method == "POST":
        name = (request.form.get("machine") or "").strip() or "hub"
        hub = state.hub(s)
        if hub:
            hub["name"] = name
        else:
            s["machines"].insert(0, {"name": name, "address": "local",
                                     "role": "hub", "kind": "docker"})
        state.save(s)
        return redirect(url_for("step2_capabilities"))
    return render_template("step1.html", s=s, hub=state.hub(s), step=1)


# ------------------------------------------------------ step 2: what can be measured
@app.route("/capabilities", methods=["GET", "POST"])
@hub_only
def step2_capabilities():
    s = _s()
    if not state.hub(s):
        return redirect(url_for("step1_environment"))
    report = capabilities.report()
    if request.method == "POST":
        s["capabilities"] = {k: {"available": v["available"], "source": v.get("source"),
                                 "reason": v.get("reason")}
                             for k, v in report["signals"].items()}
        state.save(s)
        return redirect(url_for("step3_discover"))
    return render_template("step2.html", s=s, report=report, step=2)


# ------------------------------------------------------ step 3: containers and rules
@app.route("/discover", methods=["GET", "POST"])
@hub_only
def step3_discover():
    s = _s()
    everything, problems = inventory.all_containers(s)
    message = None
    if request.method == "POST":
        ids = set(request.form.getlist("sel"))
        picked = [c for c in everything if c["id"] in ids]
        if picked:
            s["rules"] = rules.propose(picked, everything)
            state.save(s)
            return redirect(url_for("step4_qos"))
        message = "Pick at least one container: with none selected there is nothing to monitor."

    matching = rules.evaluate(s.get("rules"), s.get("exclusions"), everything)
    return render_template("step3.html", s=s, everything=everything,
                           matching={c["id"] for c in matching},
                           excluded=rules.excluded_ids(s.get("exclusions"), everything),
                           unstable=rules.unstable(everything), problems=problems,
                           message=message, step=3)


# ------------------------------------------------------------------------ step 4: QoS
@app.route("/qos", methods=["GET", "POST"])
@hub_only
def step4_qos():
    s = _s()
    everything, _ = inventory.all_containers(s)
    matching = rules.evaluate(s.get("rules"), s.get("exclusions"), everything)
    if request.method == "POST":
        if request.form.get("skip"):
            s["probes"] = []
        else:
            s["probes"] = [{"type": "http",
                            "port": int(request.form.get("port") or 80),
                            "path": request.form.get("path") or "/"}]
        state.save(s)
        return redirect(url_for("step5_confirm"))
    saved = (s.get("probes") or [None])[0]
    suggested = next((c["ports"][0] for c in matching if c.get("ports")), None)
    return render_template("step4.html", s=s, matching=matching, suggested=suggested,
                           port=(saved or {}).get("port") or suggested or 80,
                           path=(saved or {}).get("path") or "/", step=4)


# -------------------------------------------------------------------- step 5: confirm
@app.route("/confirm")
@hub_only
def step5_confirm():
    s = _s()
    everything, _ = inventory.all_containers(s)
    matching = rules.evaluate(s.get("rules"), s.get("exclusions"), everything)
    return render_template("step5.html", s=s, matching=matching,
                           probes=generator.probes_for(s, matching), step=5)


# ------------------------------------------------------------------ step 6: provision
@app.route("/provision", methods=["POST"])
@hub_only
def step6_provision():
    s = _s()
    hub_result = generator.generate_hub(s)
    notes = []
    if "prometheus" in hub_result["changed"]:
        notes.append(generator.reload_prometheus())
    local = generator.generate_local(s)
    notes.extend(generator.apply_local(s, local["changed"]))
    everything, problems = inventory.all_containers(s)
    matching = rules.evaluate(s.get("rules"), s.get("exclusions"), everything)
    return render_template("step6.html", s=s, matching=matching, notes=notes,
                           files=hub_result["files"], problems=problems, step=6)


# ---------------------------------------------------------------------- machines
@app.route("/machines", methods=["GET", "POST"])
@hub_only
def machines():
    s = _s()
    message = added = None
    # What was typed, so a rejected form comes back filled in. Retyping an API server and
    # a token because the port was wrong is how somebody decides this is not worth it.
    form = {}
    if request.method == "POST":
        if request.form.get("remove"):
            name = request.form["remove"]
            gone = state.find(s, name) or {}
            tail = ("Stop the compose on that machine as well: the hub cannot do it "
                    "for you.")
            # What we installed, we take out. Leaving a privileged DaemonSet behind in
            # somebody's cluster because they clicked Remove here would be rude.
            if state.kind(gone) == "kubernetes":
                token = state.read_token(name)
                notes = k8s_install.uninstall(gone.get("address", ""), token, name)
                for note in notes:
                    app.logger.info("uninstall %s: %s", name, note)
                tail = ("The collectors were removed from it too."
                        if all(n.startswith("removed") for n in notes) else
                        "Some of its collectors could not be removed: " +
                        "; ".join(n for n in notes if not n.startswith("removed")))
            s["machines"] = [m for m in s["machines"] if m["name"] != name]
            state.drop_token(name)          # a cluster's credential goes with it
            state.save(s)
            generator.generate_hub(s)
            generator.reload_prometheus()
            message = f"{name} removed. {tail}"
        else:
            name = (request.form.get("name") or "").strip()
            address = (request.form.get("address") or "").strip()
            kind = request.form.get("kind", "docker")
            token = (request.form.get("token") or "").strip()
            form = {"name": name, "address": address, "kind": kind}
            if not name or not address:
                message = "A machine needs both a name and an address."
            elif state.find(s, name):
                message = f"There is already a machine called {name}."
            elif kind == "kubernetes" and not token:
                message = "A cluster needs a token: the hub reaches it through its API."
            else:
                # A cluster is checked BEFORE it is saved. A Docker node cannot be: it
                # may legitimately not be up yet, since the user still has to go and
                # start it. A cluster is already running or it is not a cluster.
                ok, detail, nets = True, None, []
                if kind == "kubernetes":
                    ok, address, nets, detail = _reach(address, token)
                    if ok:
                        f = k8s_api.survey(address, token)
                        detail = (f"{detail} · {len(f['nodes'])} node(s) · CPU and memory "
                                  f"from the kubelet")
                        # The same thing `docker compose up` does on a Docker machine:
                        # put the collectors beside what they measure. Measuring half a
                        # cluster and calling the rest a limitation was the wrong answer —
                        # a cluster is a machine like any other, and gets the four signals.
                        done, notes = k8s_install.install(address, token, name)
                        for note in notes:
                            app.logger.info("install %s: %s", name, note)
                        detail += (" · installed Kepler and node-exporter" if done else
                                   " · COULD NOT INSTALL the collectors: " +
                                   "; ".join(n for n in notes if n.startswith("FAILED")))
                if not ok:
                    message = f"Could not reach {address}: {detail}"
                else:
                    # Only what actually differs is stored, so the common case leaves no
                    # trace and a default that changes one day is not frozen into old state.
                    custom = {}
                    for key, default in state.DEFAULT_PORTS.items():
                        raw = (request.form.get(f"port_{key}") or "").strip()
                        if raw.isdigit() and int(raw) != default:
                            custom[key] = int(raw)
                    entry = {"name": name, "address": address,
                             "role": "node", "kind": kind}
                    if custom:
                        entry["ports"] = custom
                    if nets:
                        # Remembered so the reconciler can put the hub back on them:
                        # a rebuild drops every network but the compose one.
                        entry["networks"] = nets
                    if kind == "kubernetes":
                        state.save_token(name, token)
                    s["machines"].append(entry)
                    state.save(s)
                    generator.generate_hub(s)
                    generator.reload_prometheus()
                    added, message, form = name, detail, {}
    return render_template("machines.html", s=s, health=_machine_health(s),
                           message=message, added=added, form=form,
                           hub_address=request.host, defaults=state.DEFAULT_PORTS,
                           kinds=state.KINDS,
                           loopback=request.host.split(":")[0] in
                                    ("localhost", "127.0.0.1", "::1"), step=0)


LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _split(address):
    """host, port from what the user typed. Accepts a scheme and tolerates its absence."""
    bare = address.split("://", 1)[-1].rstrip("/")
    host, _, port = bare.partition(":")
    return host, (int(port) if port.isdigit() else None)


def _reach(address, token):
    """Find an address for this cluster that the HUB can actually use.

    The address a person reads off their own machine is the one their browser uses. Ours
    is a container, and the two are not the same network. Rather than ask people to know
    that, the app tries what they gave, and if that never gets there, looks for a local
    container publishing that port — then joins that container's network and talks to it
    directly. That is a fact about Docker, not about any one Kubernetes distribution, so
    it covers kind, k3d and minikube's docker driver without naming any of them.

    Returns (ok, address, networks, detail).
    """
    ok, detail, answered = k8s_api.reachable(address, token)
    if ok or answered:
        # Answered and refused is a token problem. Looking for another route would only
        # find a different door to the same building — or worse, a different building.
        return ok, address, [], detail

    host, port = _split(address)
    if not port:
        return False, address, [], detail + " (no port given: an API server needs one)"

    local = host in LOOPBACK or host.startswith("127.")
    found = docker_api.publisher(port) if local else None
    if not found:
        return False, address, [], detail

    # It is a container on this machine. Join its network and address it by name.
    for note in generator.attach(generator.APP, found["networks"]):
        app.logger.info("reach: %s", note)
    candidate = f"{found['name']}:{found['private_port']}"
    ok, detail2, _ = k8s_api.reachable(candidate, token)
    if ok:
        return True, candidate, found["networks"], (
            f"{detail2} \u00b7 reached as {candidate}. {address} is a port published by "
            f"the container {found['name']} on this machine, which the hub cannot use "
            f"from inside its own container")
    return False, address, [], f"{detail} \u00b7 also tried {candidate}: {detail2}"


def _machine_health(s):
    """Is each machine reporting? Read straight from Prometheus `up`."""
    import json
    import urllib.parse
    import urllib.request
    out = {}
    try:
        q = urllib.parse.urlencode({"query": 'sum by (cluster) (up)'})
        with urllib.request.urlopen(f"{generator.PROMETHEUS_URL}/api/v1/query?{q}", timeout=6) as r:
            for row in json.load(r)["data"]["result"]:
                out[row["metric"].get("cluster")] = float(row["value"][1])
    except Exception:
        pass
    health = {}
    for m in s.get("machines") or []:
        up = out.get(m["name"])
        health[m["name"]] = ("reporting" if up else
                             "silent" if up == 0 else "no data yet")
    return health


# ------------------------------------------------------------- status and operation
@app.route("/status")
@hub_only
def status():
    s = _s()
    if not generator.is_installed(s):
        return redirect(url_for("step1_environment"))
    everything, problems = inventory.all_containers(s)
    return render_template("status.html", s=s, stack=generator.stack_status(),
                           matching=rules.evaluate(s.get("rules"), s.get("exclusions"), everything),
                           report=capabilities.report(), health=_machine_health(s),
                           problems=problems, reconciler=RECONCILER.snapshot(), step=0)


@app.route("/containers")
@hub_only
def containers_shortcut():
    return redirect(url_for("step3_discover"))


# ------------------------------------------------------------------------- the API
@app.route("/api/config")
def api_config():
    """What a node asks for. The hub decides; each node applies it to what it sees."""
    if role.IS_NODE:
        return jsonify(RECONCILER.config or {})
    s = _s()
    return jsonify({k: s.get(k) for k in ("rules", "exclusions", "probes")})


@app.route("/metrics")
def metrics():
    """This machine's inventory. The hub stamps `cluster` when it scrapes."""
    import metrics as metrics_mod
    cfg = RECONCILER.config or {"rules": [], "exclusions": [], "probes": []}
    return Response(metrics_mod.render(cfg), mimetype="text/plain; version=0.0.4")


@app.route("/metrics/<name>")
@hub_only
def metrics_for(name):
    """A cluster's inventory, served by the hub.

    A Docker node publishes its own on /metrics, because only it can see its daemon. A
    cluster has no node half to do that, so the hub asks its API and publishes the same
    two series here. Prometheus stamps `cluster` either way, so the dashboard cannot
    tell the difference.
    """
    import metrics as metrics_mod
    s = _s()
    machine = state.find(s, name)
    if not machine or state.kind(machine) != "kubernetes":
        return Response("# no such cluster\n", status=404,
                        mimetype="text/plain; version=0.0.4")
    cfg = {k: s.get(k) for k in ("rules", "exclusions", "probes")}
    try:
        everything = inventory.for_machine(machine, all_states=True)
    except Exception as exc:
        # An empty body would read as "this cluster has nothing", which is a lie. A 503
        # makes the target go down, which is what `silent` on the status page means.
        return Response(f"# cannot reach {name}: {exc}\n", status=503,
                        mimetype="text/plain; version=0.0.4")
    return Response(metrics_mod.render(cfg, everything),
                    mimetype="text/plain; version=0.0.4")


@app.route("/api/state")
@hub_only
def api_state():
    return jsonify(_s())


@app.route("/api/capabilities")
def api_capabilities():
    return jsonify(capabilities.report())


@app.route("/api/containers")
def api_containers():
    """This machine's containers. A node serves this so the hub can discover."""
    return jsonify(docker_api.containers(all_states=True))


@app.route("/api/stack")
def api_stack():
    return jsonify(generator.stack_status())


@app.route("/api/reconciler")
def api_reconciler():
    return jsonify(RECONCILER.snapshot())


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
