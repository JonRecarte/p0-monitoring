"""Every container across every machine, as one list.

The hub cannot see a node's Docker daemon, but the node already publishes its own
inventory over HTTP. So discovery is: read the local socket, then ask each node.
Nothing else is needed, and no credentials are involved.
"""
import json
import urllib.request

import docker_api
import state as state_mod


def _remote(machine, timeout=6):
    url = f"http://{machine['address']}:8000/api/containers"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def all_containers(data=None, all_states=False):
    """Returns (containers, problems). Each container carries the machine it lives on."""
    data = data or state_mod.load()
    out, problems = [], []

    for m in data.get("machines") or []:
        local = m.get("address") in ("local", "", None)
        try:
            found = docker_api.containers(all_states=all_states) if local else _remote(m)
        except Exception as exc:
            problems.append({"machine": m["name"], "error": str(exc)})
            continue
        for c in found:
            c["machine"] = m["name"]
            out.append(c)

    return sorted(out, key=lambda c: (c["machine"], c["name"])), problems
