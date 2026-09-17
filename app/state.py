"""App state: a single readable config.yaml.

It is the only thing that cannot be rebuilt. Everything the stack reads is derived
from it. Only the hub has one; a node fetches the same content over HTTP.
"""
import os

import yaml

PATH = os.environ.get("STATE_PATH", "/data/config.yaml")

# The Compose project the stack itself runs under. Anything in it is never a target:
# the monitoring must not measure itself into the rankings.
OWN_PROJECT = "p0-monitoring"
# Names this project used before. Carried in old state files, meaningless now.
OBSOLETE = {"p0-monitoring-stack", "p0-monitoring-app", "fmstack", "fmapp"}

# What a machine publishes, unless it says otherwise. The hub scrapes a node at these,
# so a node that moves one has to say so; a hub reaches its own by service name and is
# unaffected. Keys match the fields of the Machines form.
DEFAULT_PORTS = {"app": 8000, "cadvisor": 8080, "node": 9100,
                 "kepler": 9102, "cloudprober": 9313}


def ports(machine):
    """A machine's ports, defaults filled in."""
    return {**DEFAULT_PORTS, **(machine.get("ports") or {})}

EMPTY = {
    "environment": None,
    # Every machine that is measured. The hub is one of them: a single machine is the
    # N=1 case of the same model, not a separate product.
    "machines": [],
    "capabilities": {},
    "rules": [],
    # neither the stack nor the app itself should be monitored
    "exclusions": [
        {"type": "project", "value": "p0-monitoring"},
    ],
    "probes": [],
}


def _migrate(data):
    """Bring an older state file up to date.

    Self-healing rather than a one-shot migration: the exclusion that keeps the stack
    out of its own rankings is re-asserted on every load, so renaming the project can
    never quietly leave the monitoring measuring itself.
    """
    if "machine" in data and not data.get("machines"):
        data["machines"] = [{"name": data.pop("machine"), "address": "local", "role": "hub"}]

    ex = [e for e in data.get("exclusions") or []
          if not (e.get("type") == "project" and e.get("value") in OBSOLETE)]
    if not any(e.get("type") == "project" and e.get("value") == OWN_PROJECT for e in ex):
        ex.insert(0, {"type": "project", "value": OWN_PROJECT})
    data["exclusions"] = ex
    return data


def load():
    if not os.path.exists(PATH):
        return {k: (list(v) if isinstance(v, list) else v) for k, v in EMPTY.items()}
    with open(PATH) as fh:
        data = yaml.safe_load(fh) or {}
    merged = {k: (list(v) if isinstance(v, list) else v) for k, v in EMPTY.items()}
    merged.update(_migrate(data))
    return merged


def save(data):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)
    os.replace(tmp, PATH)  # atomic write


# --------------------------------------------------------------------- machines
def hub(data):
    return next((m for m in data.get("machines") or [] if m.get("role") == "hub"), None)


def nodes(data):
    return [m for m in data.get("machines") or [] if m.get("role") != "hub"]


def find(data, name):
    return next((m for m in data.get("machines") or [] if m.get("name") == name), None)
