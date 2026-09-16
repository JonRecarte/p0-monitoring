"""Which half of the product this process is.

One image, two roles, decided by a single variable:

    HUB unset            -> this is the hub: it holds the configuration and serves it
    HUB=http://host:8000 -> this is a node: it fetches its configuration from there

Everything else follows from that. A machine always measures itself, hub or not, so
the hub is simply the node that also runs Prometheus, Grafana and the wizard.
"""
import os

HUB_URL = (os.environ.get("HUB") or "").strip().rstrip("/")
IS_HUB = not HUB_URL
IS_NODE = not IS_HUB

# Compose does not tell a service which profiles are active, so "did you forget
# --profile hub?" is answered by looking at reality instead: the status page checks
# whether Prometheus and Grafana are actually running on a hub.


def describe():
    return "hub" if IS_HUB else f"node of {HUB_URL}"
