"""Every container across every machine, as one list.

Three places a machine's inventory can come from, and the rest of the app sees no
difference between them:

  this machine      the Docker socket
  a Docker node     the node publishes its own inventory over HTTP
  a cluster         the hub asks the API server directly

A cluster needs no node half because Kubernetes already is one: the kubelet, the API
and the pod list are all there. That is why a cluster is one entry in `machines` and
not a third kind of installation.
"""
import json
import urllib.request

import docker_api
import k8s_api
import state as state_mod


def _remote(machine, timeout=6):
    port = state_mod.ports(machine)["app"]
    url = f"http://{machine['address']}:{port}/api/containers"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def for_machine(machine, all_states=False):
    """One machine's containers. Raises: the caller records the machine as a problem
    rather than letting one unreachable machine empty the whole list."""
    if state_mod.kind(machine) == "kubernetes":
        token = state_mod.read_token(machine["name"])
        return k8s_api.pods(machine["address"], token, all_states=all_states)
    if machine.get("address") in ("local", "", None):
        return docker_api.containers(all_states=all_states)
    return _remote(machine)


def all_containers(data=None, all_states=False):
    """Returns (containers, problems). Each container carries the machine it lives on."""
    data = data or state_mod.load()
    out, problems = [], []

    for m in data.get("machines") or []:
        try:
            found = for_machine(m, all_states=all_states)
        except Exception as exc:
            problems.append({"machine": m["name"], "error": str(exc)})
            continue
        for c in found:
            c["machine"] = m["name"]
            out.append(c)

    return sorted(out, key=lambda c: (c["machine"], c["name"])), problems
