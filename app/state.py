"""App state: a single readable config.yaml.

It is the only thing that cannot be rebuilt. The stack files are all derived from it.
"""
import os

import yaml

PATH = os.environ.get("STATE_PATH", "/data/config.yaml")

EMPTY = {
    "environment": None,
    "machine": None,
    "capabilities": {},
    "rules": [],
    # neither the stack nor the app itself should be monitored
    "exclusions": [
        {"type": "project", "value": "p0-monitoring-stack"},
        {"type": "project", "value": "p0-monitoring-app"},
    ],
    "probes": [],
}


def load():
    if not os.path.exists(PATH):
        return dict(EMPTY)
    with open(PATH) as fh:
        data = yaml.safe_load(fh) or {}
    merged = dict(EMPTY)
    merged.update(data)
    return merged


def save(data):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)
    os.replace(tmp, PATH)  # atomic write
