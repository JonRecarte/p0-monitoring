"""What this machine publishes about itself, in Prometheus text format.

These two series used to be written to a file that node-exporter picked up. That only
works when node-exporter runs beside the app, which stops being true the moment the
machine being measured is a different one. Serving them over HTTP works the same way
local or remote, and removes a mount.

Neither series carries the machine name: the hub stamps `cluster` as a target label
when it scrapes, so there is only one place where that name can be wrong.
"""
import docker_api
import rules as rules_mod


def _escape(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _line(name, labels, value=1):
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
    return f"{name}{{{inner}}} {value}"


def render(config, everything=None):
    """config: the rules/exclusions/probes this machine has been told to apply.

    `everything` lets the hub render these same two series for a cluster it scrapes,
    from the pod list instead of the local socket. The series are identical either way,
    which is what keeps one dashboard working across both.
    """
    everything = docker_api.containers(all_states=True) if everything is None else everything
    running = [c for c in everything if c["state"] == "running"]
    matching = rules_mod.evaluate(config.get("rules"), config.get("exclusions"), running)
    monitored = {c["id"] for c in matching}

    out = [
        "# HELP container_info Inventory of every container on this machine",
        "# TYPE container_info gauge",
    ]
    for c in everything:
        out.append(_line("container_info", {
            "container_id": c["id"], "pod": c["target"], "namespace": c["project"],
            "image": c["image"], "state": c["state"], "ip": c["ip"],
            "monitored": "1" if c["id"] in monitored else "0",
        }))

    out += [
        "# HELP target_info Identity of each monitored target",
        "# TYPE target_info gauge",
    ]
    for c in matching:
        # The energy panel joins `on(container_id)`, and Kepler keys its series by the
        # id of the CONTAINER, not of the pod — verified against a live cluster, where
        # it publishes a 64-character containerd id. A pod is one row of inventory but
        # can hold several containers, so it needs one of these per container or the
        # join silently matches nothing and the energy panels come up empty.
        for cid in (c.get("container_ids") or [c["id"]]):
            labels = {"container_id": cid, "pod": c["target"], "namespace": c["project"]}
            for key in ("app", "role"):
                if c.get(key):
                    labels[key] = c[key]
            out.append(_line("target_info", labels))

    return "\n".join(out) + "\n"
