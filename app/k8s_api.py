"""Kubernetes seen through the same lens as Docker.

The whole point of this module is that it returns the SAME normalised dictionary as
`docker_api.containers()`. Because it does, `rules.py`, `metrics.py`, `inventory.py`
and the six screens of the wizard work on a cluster without being told about it.

Everything goes through the API server, including the metrics: its proxy subresource
reaches the kubelet, and pods and services, so a cluster needs exactly one address, one
token and no NodePort, no Ingress and no route to the pod network.

The unit is the POD, not the container. That is what the dashboards already call `pod`,
and what the kubelet's cAdvisor labels its series with.
"""
import json
import ssl
import urllib.error
import urllib.request

# The API server's certificate is signed by the cluster's own CA, which we have no copy
# of. P0 scrapes the same endpoints the same way (`insecureSkipVerify: true`). The token
# is what authenticates; this only skips proving the server's identity.
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

TIMEOUT = 8


def _url(address, path):
    address = address.strip().rstrip("/")
    if not address.startswith(("http://", "https://")):
        address = "https://" + address
    return address + path


def get(machine, path, token, timeout=TIMEOUT, raw=False):
    """One GET against the API server. Raises on anything that is not a 2xx."""
    req = urllib.request.Request(_url(machine, path))
    if token:
        req.add_header("Authorization", "Bearer " + token.strip())
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        body = r.read()
    return body.decode() if raw else json.loads(body)


def reachable(address, token):
    """(ok, detail, answered). Used by the Machines form before anything is saved.

    `answered` separates "I could not get there" from "I got there and was turned away".
    They need different help: the first is a routing problem the app may be able to solve
    itself, the second is a wrong token and no amount of routing will fix it.
    """
    try:
        v = get(address, "/version", token)
        return True, f"Kubernetes {v.get('gitVersion', '?')}", True
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, f"the API server refused the token (HTTP {exc.code})", True
        return False, f"HTTP {exc.code} from the API server", True
    except Exception as exc:
        return False, str(exc), False


def normalise(payload, all_states=False):
    """A pod list from the API into the shape the rest of the app speaks.

    Pure: no network. Everything this module can get wrong is testable from a file.
    """
    out = []
    for p in payload.get("items") or []:
        meta, status = p.get("metadata") or {}, p.get("status") or {}
        spec = p.get("spec") or {}
        # Phase is capitalised, and Docker's vocabulary is lowercase. `running` has to
        # mean the same thing in both or every rule and every count is wrong.
        state = (status.get("phase") or "").lower()
        if not all_states and state != "running":
            continue
        containers = spec.get("containers") or []
        workload = _workload(meta)
        statuses = status.get("containerStatuses") or []
        # containerd://<id>, docker://<id> — cAdvisor and Kepler use the bare id
        ids = [(cs.get("containerID") or "").split("://")[-1] for cs in statuses]
        out.append({
            "id": meta.get("uid", ""),
            "short_id": (meta.get("uid") or "")[:12],
            "name": meta.get("name", ""),
            "image": containers[0].get("image", "") if containers else "",
            # the dimension translation, but the other way round: here they are native
            "project": meta.get("namespace", "default"),
            "target": meta.get("name", ""),
            "identity_source": "kubernetes",
            # What survives a restart. A pod name does not: `coredns-86875b79f8-wwxzp`
            # is gone the moment the pod is recreated, so a rule written against it
            # would quietly stop matching. Rules are written against this instead.
            "workload": workload,
            "from_compose": False,
            "labels": meta.get("labels") or {},
            "ports": sorted({cp.get("containerPort") for c in containers
                             for cp in (c.get("ports") or []) if cp.get("containerPort")}),
            "state": state,
            "ip": status.get("podIP") or "",
            "networks": [],
            # what the energy and cpu series are keyed by, kept for completeness
            "container_ids": [i for i in ids if i],
        })
    return sorted(out, key=lambda c: (c["project"], c["name"]))


def _workload(meta):
    """The name that outlives the pod: its controller.

    A ReplicaSet is itself named after its Deployment plus a hash, so one more strip
    gets back to the name a human recognises. Anything unowned is its own workload.
    """
    for ref in meta.get("ownerReferences") or []:
        name = ref.get("name") or ""
        if ref.get("kind") == "ReplicaSet":
            return name.rsplit("-", 1)[0]
        if name:
            return name
    return meta.get("name", "")


def pods(machine, token, all_states=False):
    """Every pod in the cluster, normalised."""
    return normalise(get(machine, "/api/v1/pods", token), all_states=all_states)


def nodes(machine, token):
    """Node names. The scrape configuration discovers them itself, but the wizard shows
    them, and an empty list is how a broken token looks before anything is generated."""
    payload = get(machine, "/api/v1/nodes", token)
    return [(i.get("metadata") or {}).get("name", "") for i in payload.get("items") or []]


def _path(obj):
    """Where an object lives in the API, from the object itself.

    apiVersion tells core from grouped, metadata.namespace tells namespaced from
    cluster-scoped, and every kind we create pluralises by adding an s. No table to
    keep in step with anything.
    """
    api = obj["apiVersion"]
    base = f"/apis/{api}" if "/" in api else f"/api/{api}"
    plural = obj["kind"].lower() + "s"
    ns = (obj.get("metadata") or {}).get("namespace")
    scope = f"/namespaces/{ns}" if ns else ""
    return f"{base}{scope}/{plural}/{obj['metadata']['name']}"


def apply(address, token, obj, manager="p0-monitoring", dry_run=False):
    """Create or update one object, idempotently.

    Server-side apply rather than create-then-fix-the-409: one call whether the object
    is there or not, no resourceVersion to fetch, and the API server records that these
    fields are ours — so re-installing never fights with whatever else touched it.

    dry_run asks the API server to validate and admit the object and then throw it away.
    It is how this can be checked against a real cluster — schema, permissions, admission
    and all — without writing to one.
    """
    query = f"?fieldManager={manager}&force=true" + ("&dryRun=All" if dry_run else "")
    url = _url(address, f"{_path(obj)}{query}")
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), method="PATCH")
    req.add_header("Content-Type", "application/apply-patch+yaml")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token.strip())
    with urllib.request.urlopen(req, timeout=20, context=_CTX) as r:
        return r.status


def delete(address, token, obj):
    """Remove one object. A 404 counts as success: the point is that it is gone."""
    req = urllib.request.Request(_url(address, _path(obj)), method="DELETE")
    if token:
        req.add_header("Authorization", "Bearer " + token.strip())
    try:
        with urllib.request.urlopen(req, timeout=20, context=_CTX) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 404
        raise


def survey(address, token):
    """What this cluster can actually give, looked up rather than assumed.

    D24 says the app scrapes what is already there and installs nothing into somebody
    else's cluster. The consequence is that energy and host metrics may simply not
    exist here — and the one thing that must not happen is finding that out from an
    empty panel three days later. So it is checked when the cluster is added, and said
    out loud, exactly as screen 2 says when RAPL is missing.
    """
    found = {"nodes": [], "kepler": False, "node_exporter": False}
    try:
        found["nodes"] = nodes(address, token)
    except Exception:
        return found
    try:
        names = [(p.get("metadata") or {}).get("name", "")
                 for p in (get(address, "/api/v1/pods", token).get("items") or [])]
        found["kepler"] = any("kepler" in n for n in names)
        found["node_exporter"] = any("node-exporter" in n for n in names)
    except Exception:
        pass
    return found
