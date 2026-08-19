"""The app: onboarding wizard and stack governance.

The app is NOT in the data path. If it dies, the stack keeps measuring.
"""
import os

from flask import Flask, jsonify, redirect, render_template, request, url_for

import capabilities, docker_api, generator, rules, state

app = Flask(__name__, template_folder="templates", static_folder="static")


def _s():
    return state.load()


@app.context_processor
def _globals():
    """`installed` drives the navigation: no status link during the first install."""
    return {"installed": generator.is_installed(state.load())}


# ------------------------------------------------------------- step 1: environment
@app.route("/", methods=["GET", "POST"])
def step1_environment():
    s = _s()
    if request.method == "POST":
        s["environment"] = request.form.get("environment", "docker")
        s["machine"] = (request.form.get("machine") or "").strip() or "unnamed"
        state.save(s)
        return redirect(url_for("step2_capabilities"))
    return render_template("step1.html", s=s, step=1)


# ------------------------------------------------------ step 2: what can be measured
@app.route("/capabilities", methods=["GET", "POST"])
def step2_capabilities():
    s = _s()
    if not s.get("environment"):
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
def step3_discover():
    s = _s()
    try:
        everything = docker_api.containers()
    except Exception as exc:
        return render_template("error.html", message=f"Cannot talk to Docker: {exc}")

    message = None
    if request.method == "POST":
        ids = set(request.form.getlist("sel"))
        picked = [c for c in everything if c["id"] in ids]
        if picked:
            s["rules"] = rules.propose(picked, everything)
            state.save(s)
            return redirect(url_for("step4_qos"))
        # never let the wizard finish monitoring nothing
        message = "Pick at least one container: with none selected there is nothing to monitor."

    matching = rules.evaluate(s.get("rules"), s.get("exclusions"), everything)
    return render_template("step3.html", s=s, everything=everything,
                           matching={c["id"] for c in matching},
                           excluded=rules.excluded_ids(s.get("exclusions"), everything),
                           unstable=rules.unstable(everything), message=message, step=3)


# --------------------------------------------------------------------- step 4: QoS
@app.route("/qos", methods=["GET", "POST"])
def step4_qos():
    s = _s()
    everything = docker_api.containers()
    matching = rules.evaluate(s.get("rules"), s.get("exclusions"), everything)
    if request.method == "POST":
        if request.form.get("skip"):
            s["probes"] = []
        else:
            # one probe config for the whole selection: it applies to every target
            s["probes"] = [{"type": "http",
                            "port": int(request.form.get("port") or 80),
                            "path": request.form.get("path") or "/"}]
        state.save(s)
        return redirect(url_for("step5_confirm"))
    # what was chosen before wins over the suggestion: adding a container must not
    # mean retyping the probe config, now that this is the only path back in
    saved = (s.get("probes") or [None])[0]
    suggested = None
    for c in matching:
        if c["ports"]:
            suggested = c["ports"][0]
            break
    port = (saved or {}).get("port") or suggested or 80
    path = (saved or {}).get("path") or "/"
    return render_template("step4.html", s=s, matching=matching, suggested=suggested,
                           port=port, path=path, step=4)


# ----------------------------------------------------------------- step 5: confirm
@app.route("/confirm")
def step5_confirm():
    s = _s()
    everything = docker_api.containers()
    matching = rules.evaluate(s.get("rules"), s.get("exclusions"), everything)
    return render_template("step5.html", s=s, matching=matching,
                           probes=generator.probes_for(s, matching), step=5)


# ---------------------------------------------------------------- step 6: provision
@app.route("/provision", methods=["POST"])
def step6_provision():
    s = _s()
    result = generator.generate(s)
    ok, output = generator.launch(s)
    return render_template("step6.html", s=s, result=result, ok=ok, output=output, step=6)


# ------------------------------------------------------------- status and operation
@app.route("/status")
def status():
    s = _s()
    if not generator.is_installed(s):
        return redirect(url_for("step1_environment"))
    everything = docker_api.containers()
    return render_template("status.html", s=s, stack=generator.stack_status(),
                           matching=rules.evaluate(s.get("rules"), s.get("exclusions"), everything),
                           report=capabilities.report(), step=0)


@app.route("/containers", methods=["GET"])
def containers_shortcut():
    """Once installed, adding a container starts here."""
    return redirect(url_for("step3_discover"))


# ------------------------------------------------------------------------- the API
@app.route("/api/state")
def api_state():
    return jsonify(_s())


@app.route("/api/capabilities")
def api_capabilities():
    return jsonify(capabilities.report())


@app.route("/api/containers")
def api_containers():
    return jsonify(docker_api.containers())


@app.route("/api/stack")
def api_stack():
    return jsonify(generator.stack_status())


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
