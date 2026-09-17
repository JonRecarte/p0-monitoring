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

## The two ways to run it

The same repository covers both. What changes is how many machines you install it on, not
which version you install.

**One machine.** The containers you want to measure run on the server you install on. This is
the whole thing:

```
┌─ your server ─────────┐
│  your containers      │
│  collectors           │
│  the app · the wizard │
│  Prometheus · Grafana │
└───────────────────────┘
```

**Several machines.** The containers live somewhere else, or on more than one box. The four
collectors have to sit beside what they measure — they read the local kernel — so every
machine runs them. Only one machine keeps the dashboards and the configuration, and that one
is called the hub:

```
┌─ hub ─────────────────┐                      ┌─ node ────────────────┐
│  your containers      │                      │  your containers      │
│  collectors           │  ─── scrapes ──────→ │  collectors           │
│  the app · the wizard │                      │  the app              │
│  Prometheus · Grafana │ ←── asks for its ─── │                       │
│                       │     configuration    └───────────────────────┘
└───────────────────────┘                      ┌─ node ────────────────┐
                                               │  …                    │
                                               └───────────────────────┘
```

**One machine is not a separate mode.** The hub box is the same box as the first picture: a
hub always measures itself as well, so one machine is simply this second picture with no nodes
attached — same code, same screens, same dashboard. You can start with one machine and add a
second later from the interface, without reinstalling anything or redoing the wizard.

| | One machine | Several machines |
|---|---|---|
| **What you run** | the hub command, once | the hub command on one, the node command on each of the others |
| **What it starts** | all seven containers | hub: seven · node: five |
| **The wizard** | on that machine | on the hub only — nodes have no wizard |
| **Where you point your browser** | `http://localhost:8000` | `http://<hub-ip>:8000` |
| **Credentials needed** | none | none — see [how the two halves talk](#how-the-two-halves-talk) |

## Quick start

Same repository and same compose file on every machine:

```bash
git clone https://github.com/JonRecarte/p0-monitoring.git
cd p0-monitoring
```

**If everything runs on one machine**, that machine is the hub, and this is the whole install:

```bash
docker compose --profile hub up -d --build
```

**If you have several**, run that same command on the one that will hold the dashboards, then
on every other machine:

```bash
HUB=http://<hub-ip>:8000 docker compose up -d --build
```

Two things tell them apart, and nothing else does: `--profile hub` adds Prometheus and
Grafana, and `HUB` points a machine at them.

> [!TIP]
> Forgetting `--profile hub` is the easy mistake: the collectors come up, but nothing stores
> or draws what they collect. The status page checks for it and says so.

Then open **`http://<hub-ip>:8000`** — or `http://localhost:8000` on a single machine —
and follow six screens:

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

| Container | Purpose | Runs on | Privileges |
|---|---|---|---|
| `p0m-app` | this machine's inventory; the wizard on a hub | every machine | Docker socket |
| `p0m-cadvisor` | CPU, memory, limits, container labels | every machine | privileged · `/sys` `/var/lib/docker` (read-only) |
| `p0m-node-exporter` | host metrics | every machine | none |
| `p0m-kepler` | energy | every machine | privileged · `/sys` `/proc` `/lib/modules` |
| `p0m-cloudprober` | QoS probes | every machine | none |
| `p0m-prometheus` | storage and queries | **hub only** | none |
| `p0m-grafana` | dashboards | **hub only** | none |

Four of them **must** sit beside what they measure, because they read the local kernel.
That is the whole reason a node exists. Prometheus and Grafana only live on the hub.

Two need privileges, and screen 5 says so before you commit.

**The app is not in the data path.** Stop it, rebuild it, break it — the collectors keep
measuring. It also excludes itself and its own stack from what it monitors.

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

## Adding a machine

From **Status → Machines**, give the node a name and an address. The app tells you exactly
what to run there, hands the node its configuration when it asks, and starts scraping it.
Removing one is the same screen — the app stops scraping, and tells you to stop the compose
on that machine, which it cannot do for you.

### How the two halves talk

Over the LAN, in both directions, and **with no credentials anywhere** — no SSH keys, no
tokens, and no Docker daemon exposed to the network:

- **hub → node**: Prometheus scrapes five HTTP endpoints — `:8000` `:8080` `:9100` `:9102`
  `:9313`. That is the only thing the hub does to a node.
- **node → hub**: the node asks for its configuration and applies it. The hub cannot reach
  the node's Docker daemon, so it cannot push anything; the node comes and fetches instead.

So the hub decides *what* is monitored, and each node applies that decision to whatever it can
see locally, managing its own probes. The machine's name is stamped by the hub when it
scrapes, which is why a node never needs to be told what it is called.

**If a node goes quiet**, Status says so, and its containers stay on the dashboard with
their last known state rather than vanishing.

## Configuration

Everything the app knows lives in one readable file, `/opt/p0-monitoring/config.yaml`:

```yaml
environment: docker
capabilities:
  energy: { available: true, source: model, reason: "no domains under /sys/class/powercap" }
rules:
  - { type: label, key: app, value: drone-sitl }
machines:
  - { name: hub,  address: local,        role: hub  }
  - { name: test, address: 192.168.0.69, role: node }
exclusions:
  - { type: project, value: p0-monitoring }
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
- **A node's code does not update itself**, only its configuration. Changing the app means
  `git pull` and a rebuild on each machine.
- **Nothing is authenticated**, neither the app nor the collector ports nor the endpoint a
  node fetches its configuration from. It is a LAN tool. The app holds the Docker socket,
  which is root-equivalent — do not expose it beyond a trusted network.
- **No alerting.** Dashboards only.
- **HTTP probes only.** TCP and domain-specific probes are not implemented.
- **Grafana defaults to `admin`/`admin`.** Change it.
- Retention is fixed at 7 days with an 8 GB cap.

## Licensing

This project is released under the **Apache License 2.0**. See [LICENSE](LICENSE).

It deploys, but does not redistribute, third-party images: Prometheus, cAdvisor, Kepler,
node-exporter and cloudprober are Apache-2.0; **Grafana is AGPLv3** and is pulled from its
official image at runtime.
