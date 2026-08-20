<h1 align="center">p0-monitoring</h1>

<p align="center">
  <strong>Container monitoring that installs itself.</strong><br>
  CPU, memory, energy and quality of service for your Docker containers —<br>
  from an empty server to live dashboards in one command and six screens.
</p>

<p align="center">
  <img alt="status" src="https://img.shields.io/badge/status-early%20but%20working-e9a94a?style=flat-square">
  <img alt="platform" src="https://img.shields.io/badge/platform-Docker%20%C2%B7%20Linux-14181e?style=flat-square">
  <img alt="python" src="https://img.shields.io/badge/python-3.13-3776ab?style=flat-square">
  <img alt="license" src="https://img.shields.io/badge/license-Apache%202.0-0d6152?style=flat-square">
</p>

---

<!--
  Add screenshots here once you have them. Suggested:
  ![Wizard](docs/wizard.png)
  ![Dashboard](docs/dashboard.png)
-->

## What you get

| Signal | What it tells you | Collected by |
|---|---|---|
| **CPU** | cores used, quota, CFS throttling | cAdvisor |
| **Memory** | working set, limits | cAdvisor |
| **Energy** | watts and joules attributed per container | Kepler |
| **Quality of service** | probe success rate, latency, jitter, timeouts | cloudprober |

Plus the host itself — CPU, RAM, disk, network — so you can tell *"my container is slow"*
from *"the machine is saturated"*.

Everything lands in a single Grafana dashboard: an inventory table, per-container time
series with Top 5 rankings, a full QoS section, and a summary of the machine.

## Requirements

- **Linux** with **cgroups v2**
- **Docker** with the **`overlay2`** storage driver
- Internet access on first build (it downloads the Docker CLI and Compose plugin)
- ~500 MB of RAM for the stack, plus disk for metrics (7-day retention, capped at 8 GB)

> [!IMPORTANT]
> **Docker 29 and later default to the containerd snapshotter, which cAdvisor cannot read.**
> If your storage driver is `overlayfs` instead of `overlay2`, container CPU and memory will
> not be collected. The app detects this and tells you, but it will not change your Docker
> configuration for you. To fix it:
>
> ```jsonc
> // /etc/docker/daemon.json
> { "features": { "containerd-snapshotter": false } }
> ```
>
> Then `sudo systemctl restart docker`. Note that images built with the snapshotter stop
> being visible and need rebuilding.
>
> Check yours with `docker info | grep "Storage Driver"`.

## Quick start

```bash
git clone https://github.com/JonRecarte/p0-monitoring.git
cd p0-monitoring/app
sudo docker compose up -d --build
```

Then open **`http://<your-server>:8000`** and follow six screens:

| | Screen | What you do |
|---|---|---|
| 1 | **Environment** | name this machine |
| 2 | **Capabilities** | nothing — the app reports what it can and cannot measure here |
| 3 | **Containers** | tick the containers you care about |
| 4 | **QoS** | port and path for the health probe |
| 5 | **Confirm** | review, including the privileges about to be granted |
| 6 | **Done** | links to Grafana and Prometheus |

Grafana lands on **`:3000`** (`admin` / `admin` — change it), Prometheus on **`:9090`**.
Data shows up after about 30 seconds.

## What gets deployed

The app itself is the only thing you start by hand. It generates and launches the rest:

| Container | Purpose | Privileges |
|---|---|---|
| `p0m-cadvisor` | CPU, memory, limits, container labels | privileged · `/sys` `/proc` `/var/lib/docker` (read-only) |
| `p0m-kepler` | energy | privileged · `/sys` `/proc` `/lib/modules` `/usr/src` |
| `p0m-node-exporter` | host metrics | none |
| `p0m-cloudprober` | QoS probes | none |
| `p0m-prometheus` | storage and queries | none |
| `p0m-grafana` | dashboards | none |

Two of the six need privileges, and screen 5 says so before you commit. The other four ask
nothing of the system.

**The app does not put itself in the data path.** Stop it, rebuild it, break it — the stack
keeps measuring. It also excludes itself and its own stack from what it monitors.

## How it works

### It saves the criterion, not the list

Containers come and go. If onboarding meant ticking names off a list, it would be stale by
tomorrow. So the app watches what you tick and derives the narrowest **rule** that covers
exactly that selection:

```yaml
rules:
  - type: label          # or image, name pattern, or Compose project
    key: app
    value: drone-sitl
```

Anything you start later that matches the rule is included automatically for CPU, memory and
energy. Screen 5 shows you the rule it derived, so there is no guessing.

### It translates Docker into the dimensions dashboards expect

Grafana dashboards for containers are written against Kubernetes vocabulary. Rather than
maintain a second set of panels, the app fills those labels in **at scrape time**:

| Kubernetes | Docker |
|---|---|
| `cluster` | the machine |
| `namespace` | the Compose project |
| `pod` | the container |

One set of panels, and the dropdowns say *Namespace* and *Container* like you would expect.

### It gives the energy collector the names it lacks

Kepler resolves container names by asking the Kubernetes API. Without Kubernetes there is no
resolver: every series is labelled `system_processes` and only `container_id` tells them
apart. The app closes that gap by publishing an inventory series:

```
target_info{container_id="536635fe…", pod="drone-1", namespace="dronesim"} 1
```

```promql
rate(kepler_container_package_joules_total[5m])
  * on(container_id) group_left(pod, namespace) target_info
```

That join does two jobs: it resolves the name, and it filters out Kepler's pseudo-containers,
which otherwise dominate every ranking by an order of magnitude.

### Energy: measured or modelled, never ambiguous

Kepler always produces a number. Whether that number is a *measurement* depends on RAPL
being available, which it is not inside a virtual machine. The app checks properly — it reads
`/sys/class/powercap/intel-rapl:*/energy_uj` twice, one second apart, and only calls it real
if the value moved — and labels the result:

| | `energy_source` | What it means |
|---|---|---|
| RAPL present | `rapl` | real measurement of the CPU package, attributed per container |
| No RAPL | `model` | estimate from a trained model |

**A number marked `model` is comparative, not metrological.** Do not sum it across machines,
do not convert it to kWh for a report, and do not compare it across different hardware. It is
useful for ranking containers on the same host and watching trends. Screen 2 tells you which
one you are getting, before anything is installed.

## Adding or removing containers

Go to **Status → Add or remove containers**, adjust the selection, and confirm. The app
re-derives the rule, regenerates the configuration, restarts the prober if its probes
changed, and attaches the prober to any network your new container lives on — so you never
have to touch your own containers to make QoS work.

## Configuration

Everything the app knows lives in one readable file, `/opt/p0-monitoring/config.yaml`:

```yaml
environment: docker
machine: sim-server-01
capabilities:
  energy: { available: true, source: model, reason: "no domains under /sys/class/powercap" }
rules:
  - { type: label, key: app, value: drone-sitl }
exclusions:
  - { type: project, value: p0-monitoring-stack }
probes:
  - { type: http, port: 8080, path: /health }
```

**This is the only file that cannot be rebuilt.** The Compose file, `prometheus.yml`,
`cloudprober.cfg` and the Grafana provisioning are all derived from it and regenerated on
demand. Back up one file; move to another machine by copying one file.

## Limitations

Worth knowing before you invest time:

- **Docker only.** Kubernetes support is designed but not implemented; screen 1 shows it
  greyed out.
- **No reconciler yet.** A container that appears later is picked up for CPU, memory and
  energy on its own, but its QoS probe needs a trip through *Add or remove containers*.
- **No authentication on the app.** It holds the Docker socket, which is root-equivalent.
  Do not expose port 8000 beyond a trusted network.
- **No alerting.** Dashboards only.
- **HTTP probes only.** TCP and domain-specific probes are not implemented.
- **Grafana defaults to `admin`/`admin`.** Change it.
- Retention is fixed at 7 days with an 8 GB cap.

## Licensing

This project is released under the **Apache License 2.0**. See [LICENSE](LICENSE).

It deploys, but does not redistribute, third-party images: Prometheus, cAdvisor, Kepler,
node-exporter and cloudprober are Apache-2.0; **Grafana is AGPLv3** and is pulled from its
official image at runtime.
