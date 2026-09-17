"""Putting the collectors inside a cluster.

On a Docker machine the collectors arrive with `docker compose up`: the user runs one
command and cAdvisor, node-exporter, Kepler and cloudprober appear beside what they
measure. A cluster is the same problem and deserves the same answer, so the app installs
the equivalent there rather than measuring half of it and calling the rest a limitation.

cAdvisor is the one piece that is never installed: the kubelet already runs it.

Everything lands in one namespace, so removing it is one delete.
"""
import hashlib
import json
import os

import yaml
from jinja2 import Environment, FileSystemLoader

import k8s_api

NS = os.environ.get("K8S_NAMESPACE", "p0-monitoring")

# The same images the Docker half uses, so a number means the same thing on both sides.
IMAGES = {
    "kepler": os.environ.get("KEPLER_IMAGE",
                             "quay.io/sustainable_computing_io/kepler:release-0.8.0"),
    "node_exporter": os.environ.get("NODE_EXPORTER_IMAGE", "prom/node-exporter:v1.8.2"),
    "cloudprober": os.environ.get("CLOUDPROBER_IMAGE", "cloudprober/cloudprober:latest"),
}

_env = Environment(
    loader=FileSystemLoader(os.path.join(os.path.dirname(os.path.abspath(__file__)), "stack")),
    keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True)


def _checksum(probes):
    """Identifies a set of probes. Rides on the pod template so that changing them
    rolls the prober, which otherwise would keep running the configuration it read
    when it started."""
    return hashlib.sha256(
        json.dumps(probes, sort_keys=True).encode()).hexdigest()[:16]


def manifest(cluster, probes=None):
    """The objects to put in this cluster, as YAML."""
    probes = probes or []
    return _env.get_template("k8s-collectors.yaml.j2").render(
        cluster=cluster, ns=NS, images=IMAGES, probes=probes,
        probes_checksum=_checksum(probes))


def render(cluster, probes=None):
    """The same, parsed."""
    return [o for o in yaml.safe_load_all(manifest(cluster, probes)) if o]


# What carries the probes. The reconciler re-applies only these, every time the pods it
# probes change; the collectors do not depend on which pods were picked, and re-applying
# them on a loop would be noise in somebody else's cluster.
PROBE_OBJECTS = ("ConfigMap", "Deployment")


def install(address, token, cluster, probes=None, only=None, dry_run=False):
    """Apply everything, or only the kinds named. Returns (ok, notes), never raises.

    Partial failure is reported rather than swallowed: a cluster with node-exporter but
    no Kepler is a real state, and the screen has to be able to say which.
    """
    notes, ok = [], True
    for obj in render(cluster, probes):
        if only and obj["kind"] not in only:
            continue
        what = f"{obj['kind']}/{obj['metadata']['name']}"
        try:
            k8s_api.apply(address, token, obj, dry_run=dry_run)
            notes.append(f"applied {what}")
        except Exception as exc:
            ok = False
            notes.append(f"FAILED {what}: {_reason(exc)}")
    return ok, notes


def uninstall(address, token, cluster):
    """Take it out again. The namespace takes most of it; the cluster-scoped objects
    are not in a namespace and have to go by name."""
    notes = []
    for obj in reversed(render(cluster)):
        if obj["kind"] not in ("Namespace", "ClusterRole", "ClusterRoleBinding"):
            continue                       # the namespace takes everything inside it
        what = f"{obj['kind']}/{obj['metadata']['name']}"
        try:
            k8s_api.delete(address, token, obj)
            notes.append(f"removed {what}")
        except Exception as exc:
            notes.append(f"could not remove {what}: {_reason(exc)}")
    return notes


def _reason(exc):
    """A permissions failure is the likely one, and it needs to read as such."""
    body = getattr(exc, "read", None)
    code = getattr(exc, "code", None)
    if code in (401, 403):
        return ("the token is not allowed to create this. Installing needs more than "
                "the read-only role: see the commands on the Machines screen")
    if body:
        try:
            import json
            return json.loads(body()).get("message", str(exc))[:200]
        except Exception:
            pass
    return str(exc)
