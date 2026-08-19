"""Minimal Docker API client over the unix socket. No dependencies."""
import http.client, json, socket, urllib.parse

SOCKET = "/var/run/docker.sock"


class _Connection(http.client.HTTPConnection):
    def __init__(self, socket_path):
        super().__init__("localhost")
        self._path = socket_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(self._path)
        self.sock = s


def _post(path, payload=None):
    body = json.dumps(payload or {}).encode()
    c = _Connection(SOCKET)
    try:
        c.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        r = c.getresponse()
        raw = r.read()
        return r.status, (json.loads(raw) if raw.strip().startswith(b"{") else
                          raw.decode(errors="replace"))
    finally:
        c.close()


def _get(path, params=None):
    if params:
        path += "?" + urllib.parse.urlencode(params)
    c = _Connection(SOCKET)
    try:
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        if r.status >= 400:
            raise RuntimeError(f"Docker returned {r.status}: {body[:200].decode(errors='replace')}")
        return json.loads(body) if body else None
    finally:
        c.close()


def available():
    """(reachable, version|reason)"""
    try:
        v = _get("/version")
        return True, v.get("Version", "?")
    except Exception as e:
        return False, str(e)


def info():
    return _get("/info")


def containers(all_states=False):
    """Containers, already normalised. all_states=True also returns stopped ones."""
    out = []
    for c in _get("/containers/json", {"all": "1"} if all_states else None) or []:
        labels = c.get("Labels") or {}
        name = (c.get("Names") or ["/?"])[0].lstrip("/")
        project = labels.get("com.docker.compose.project")
        service = labels.get("com.docker.compose.service")
        out.append({
            "id": c["Id"],
            "short_id": c["Id"][:12],
            "name": name,
            "image": c.get("Image", ""),
            "project": project or "default",
            # 'target' is the stable name: Compose service > container name
            "target": service or name,
            "identity_source": "compose" if service else "name",
            "labels": {k: v for k, v in labels.items() if not k.startswith("com.docker.compose")},
            "ports": sorted({p["PrivatePort"] for p in (c.get("Ports") or []) if p.get("PrivatePort")}),
            "state": c.get("State", ""),
            "ip": next((n.get("IPAddress") for n in
                        ((c.get("NetworkSettings") or {}).get("Networks") or {}).values()
                        if n.get("IPAddress")), ""),
        })
    return sorted(out, key=lambda x: x["name"])


# --------------------------------------------------------------------- networking
# The stack must reach the user's containers to probe them, and the user must not have
# to touch their own containers for that. So: the app owns one network, declares it
# external in the generated compose, and attaches the prober to whatever networks the
# targets already live on.

def ensure_network(name):
    """Create the network if absent. Idempotent."""
    existing = _get("/networks") or []
    for n in existing:
        if n.get("Name") == name:
            return n["Id"], False
    status, body = _post("/networks/create", {"Name": name, "Driver": "bridge"})
    if status not in (200, 201):
        raise RuntimeError(f"could not create network {name}: {body}")
    return body["Id"], True


def networks_of(container_id):
    """Names of the networks a container is attached to."""
    data = _get(f"/containers/{container_id}/json")
    return sorted((data.get("NetworkSettings") or {}).get("Networks", {}).keys())


def connect(network, container_id):
    """Attach a container to a network. Returns True if it was actually attached now."""
    status, body = _post(f"/networks/{network}/connect", {"Container": container_id})
    if status in (200, 201):
        return True
    # 403 = already attached; anything else is a real problem
    if status == 403 and "already exists" in str(body).lower():
        return False
    if status == 403:
        return False
    raise RuntimeError(f"could not attach {container_id} to {network}: {status} {body}")


def restart(container_id, timeout=10):
    """Restart a container. Used when a piece's config file changed."""
    status, body = _post(f"/containers/{container_id}/restart?t={timeout}")
    if status not in (204, 200):
        raise RuntimeError(f"could not restart {container_id}: {status} {body}")


def self_container(name_hint=None):
    """The app's own container, so it can attach itself to the network it created."""
    try:
        with open("/etc/hostname") as fh:
            host = fh.read().strip()
    except Exception:
        host = ""
    for c in _get("/containers/json") or []:
        if name_hint and (c.get("Names") or [""])[0].lstrip("/") == name_hint:
            return c["Id"]
    for c in _get("/containers/json") or []:
        if host and c["Id"].startswith(host):
            return c["Id"]
    return None


def started_at(container_id):
    """When the container last started, as a UTC epoch. Used to tell whether a
    config file on disk is newer than the process that read it."""
    import calendar, re
    data = _get(f"/containers/{container_id}/json")
    raw = ((data or {}).get("State") or {}).get("StartedAt", "")
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)", raw or "")
    if not m:
        return None
    return calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0))
