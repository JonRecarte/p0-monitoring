"""Publishing something only this machine can see.

A cluster made with kind, k3d or minikube binds its API server to loopback. From that
machine it is reachable; from anywhere else it does not exist. When the hub runs
somewhere else, no address the user can type will work, and no amount of resolving on
the hub's side helps: the thing is genuinely not on the network.

But a node is already on that machine, and already reaches it. So the node publishes it:
a TCP forward from a port the node does expose to the address only it can see. That is
what a person would otherwise do by hand with socat, done by the thing that is already
there and told to do it by the hub.

Deliberately dumb: bytes in, bytes out, no parsing. TLS passes through untouched, so the
API server's certificate is still the API server's, and the node never sees a token.
"""
import logging
import socket
import threading

log = logging.getLogger("forwarder")

# Inside the container. The compose publishes this range, so the hub reaches a cluster at
# <node>:<port> like any other address, and nothing downstream has to know why.
BASE_PORT = 6443
MAX = 4

_lock = threading.Lock()
_running = {}          # name -> {"port": int, "target": "host:port", "stop": Event}


def _pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()


def _serve(listener, target, stop):
    host, port = target.rsplit(":", 1)
    while not stop.is_set():
        try:
            client, _ = listener.accept()
        except OSError:
            break
        try:
            upstream = socket.create_connection((host, int(port)), timeout=10)
        except OSError as exc:
            log.warning("forwarder: cannot reach %s: %s", target, exc)
            client.close()
            continue
        # One pair of threads per connection. There are a handful of these - Prometheus
        # scraping and the odd API call - so a thread each is simpler than a poll loop
        # and cannot get the two directions out of step.
        for a, b in ((client, upstream), (upstream, client)):
            threading.Thread(target=_pipe, args=(a, b), daemon=True).start()
    listener.close()


def expose(name, target):
    """Publish `target` (host:port, reachable from here) on a port of this machine.

    Idempotent: asking again for the same target returns the same port rather than
    piling up listeners, because the hub asks on every reconcile pass.
    """
    with _lock:
        current = _running.get(name)
        if current and current["target"] == target:
            return current["port"]
        if current:
            withdraw(name)

        used = {v["port"] for v in _running.values()}
        for port in range(BASE_PORT, BASE_PORT + MAX):
            if port in used:
                continue
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("0.0.0.0", port))
            except OSError:
                listener.close()
                continue
            listener.listen(16)
            stop = threading.Event()
            threading.Thread(target=_serve, args=(listener, target, stop),
                             daemon=True, name=f"forward-{name}").start()
            _running[name] = {"port": port, "target": target, "stop": stop,
                              "listener": listener}
            log.info("forwarder: %s published on :%s -> %s", name, port, target)
            return port
        raise RuntimeError(f"no free port between {BASE_PORT} and {BASE_PORT + MAX - 1}")


def withdraw(name):
    entry = _running.pop(name, None)
    if not entry:
        return False
    entry["stop"].set()
    try:
        entry["listener"].close()
    except OSError:
        pass
    log.info("forwarder: %s withdrawn", name)
    return True


def status():
    with _lock:
        return {n: {"port": v["port"], "target": v["target"]} for n, v in _running.items()}
