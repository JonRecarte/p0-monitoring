"""Capability detector: what can be measured on THIS machine, and why not what can't.

Every check returns available/reason/detail. The report is what wizard step 2 shows in
plain words, and what stops the app promising what it cannot deliver.
"""
import glob, os, platform, time

import docker_api

# Host path, mounted read-only inside the app container.
HOST_SYS = os.environ.get("HOST_SYS", "/host/sys")


def _cgroup_v2():
    # cgroup.controllers only exists on v2
    return os.path.exists(os.path.join(HOST_SYS, "fs/cgroup/cgroup.controllers"))


def _rapl():
    """Real RAPL: the file exists, is readable, AND its value moves."""
    files = glob.glob(os.path.join(HOST_SYS, "class/powercap/intel-rapl:*/energy_uj"))
    if not files:
        return False, "no domains under /sys/class/powercap", None
    for f in files:
        try:
            with open(f) as fh:
                a = int(fh.read().strip())
            time.sleep(1)
            with open(f) as fh:
                b = int(fh.read().strip())
        except PermissionError:
            return False, "no permission to read energy_uj", f
        except Exception as e:
            return False, f"error reading energy_uj: {e}", f
        if b != a:
            return True, None, f
    return False, "counters exist but never advance (VM without real RAPL)", files[0]


def _virtualised():
    try:
        with open("/proc/cpuinfo") as fh:
            if "hypervisor" in fh.read():
                return True
    except Exception:
        pass
    return False


def report():
    """Full report. The UI renders it as-is."""
    reachable, version = docker_api.available()
    inf = docker_api.info() if reachable else {}
    driver = inf.get("Driver", "?")
    # Measured 2026-08-18: cAdvisor cannot read containers with the containerd snapshotter
    snapshotter = any(
        "snapshotter" in " ".join(map(str, pair)).lower()
        for pair in (inf.get("DriverStatus") or [])
    )
    v2 = _cgroup_v2()
    rapl_ok, rapl_reason, rapl_path = _rapl()
    cadvisor_ok = reachable and v2 and not snapshotter
    cadvisor_reason = None if cadvisor_ok else (
        "Docker is not reachable" if not reachable else
        "cgroup v1 is not supported" if not v2 else
        "the storage driver uses the containerd snapshotter; cAdvisor cannot read containers"
    )

    signals = {
        "cpu":    {"available": cadvisor_ok, "reason": cadvisor_reason, "measured_by": "cAdvisor"},
        "memory": {"available": cadvisor_ok, "reason": cadvisor_reason, "measured_by": "cAdvisor"},
        "qos":    {"available": reachable,
                   "reason": None if reachable else "Docker is not reachable",
                   "measured_by": "cloudprober"},
        # Kepler always yields a number; RAPL decides whether it is measured or modelled
        "energy": {"available": reachable,
                   "source": "rapl" if rapl_ok else "model",
                   "reason": None if rapl_ok else f"modelled estimate: {rapl_reason}",
                   "measured_by": "Kepler"},
    }

    return {
        "docker": {"reachable": reachable, "version": version},
        "architecture": platform.machine(),
        "kernel": platform.release(),
        "cgroup_v2": v2,
        "virtualised": _virtualised(),
        "storage_driver": driver,
        "uses_snapshotter": snapshotter,
        "rapl": {"available": rapl_ok, "reason": rapl_reason, "path": rapl_path},
        "signals": signals,
        # Blocking: without this there is no CPU and no memory, and it must be said up front
        "blocker": (
            "Docker's storage driver is the containerd snapshotter. cAdvisor cannot read "
            'containers with that driver. Fix: put {"features": {"containerd-snapshotter": false}} '
            "in /etc/docker/daemon.json and restart Docker. WARNING: images built with the "
            "snapshotter will no longer be visible."
        ) if snapshotter else None,
    }
