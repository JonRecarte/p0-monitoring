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

It measures **Docker hosts and Kubernetes clusters side by side**, in one dashboard. A
container and a pod are the same kind of row; the machine they live on is a filter.

Everything lands in a single Grafana dashboard: an inventory table, per-container time
series with Top 5 rankings, a full QoS section, and a summary of the machine.

## How much of this is manual

Everything below is done **once per machine**, and nothing else is ever typed by hand:

| To measure | What you run there | Where |
|---|---|---|
| The machine the dashboards live on | one `docker compose` | [Quick start](#quick-start) |
| Another Docker host | one `docker compose`, with `HUB=` | [Quick start](#quick-start) |
| A Kubernetes cluster | one block of `kubectl`, to make a token | [A Kubernetes cluster](#a-kubernetes-cluster) |

Everything after that is the app's job: finding the containers, deriving the rule,
installing the collectors — into a cluster too — writing the scrape configuration,
reaching a cluster whose API server is bound to loopback on another machine, attaching the
prober to the networks its targets live on, and keeping all of it in step as containers
come and go.

**Why it is not zero.** Both remaining commands are the same thing: for this to measure a
machine, something on that machine has to let it in. On a Docker host that permission is
starting the containers; in a cluster it is creating a ServiceAccount and handing over its
token.

It could be avoided by asking you for SSH credentials or your whole kubeconfig. That would
be worse: the hub would then hold a master key to machines that are not its own. As it
stands **the hub never lets itself in anywhere** — it is let in, one machine at a time, and
you can take the permission back without touching anything else.

## Requirements

- **Linux** with **cgroups v2**
- **Docker**, to run the stack itself
- Internet access on first build (it downloads the Docker CLI and Compose plugin)
- ~500 MB of RAM for the stack, plus disk for metrics (7-day retention, capped at 8 GB)
- To measure a **Docker host**: the **`overlay2`** storage driver on that machine — see below
- To measure a **Kubernetes cluster**: an API server you can reach and a token

> [!IMPORTANT]
> **Docker 29 and later default to the containerd snapshotter, which cAdvisor cannot read.**
> If a machine's storage driver is `overlayfs` instead of `overlay2`, the CPU and memory of
> **that machine's containers** will not be collected. The app detects it and says so.
>
> ```bash
> docker info | grep "Storage Driver"
> ```
>
> To change it on a plain Docker, put this in `/etc/docker/daemon.json` and
> `sudo systemctl restart docker`:
>
> ```jsonc
> { "features": { "containerd-snapshotter": false } }
> ```
>
> On **Docker Desktop** that file is ignored: turn off *Use containerd for pulling and
> storing images* in Settings → General, then Apply & restart.
>
> Either way, images built with the snapshotter stop being visible and need rebuilding.
>
> **This does not affect Kubernetes clusters.** They are measured through their own
> kubelet, which reads containerd directly. A machine whose driver you cannot change can
> still have its cluster measured in full.

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
| 1 | **This machine** | give it a name |
| 2 | **Capabilities** | nothing — the app reports what it can and cannot measure here |
| 3 | **Containers** | tick the containers you care about |
| 4 | **QoS** | port and path for the health probe |
| 5 | **Confirm** | review, including the privileges about to be granted |
| 6 | **Done** | links to Grafana and Prometheus |

Grafana lands on **`:3000`** (`admin` / `admin` — change it), Prometheus on **`:9090`**.
Data shows up after about 30 seconds.

### If one of those ports is taken

Machines that already run something — another Grafana, another cAdvisor — will collide.
Every published port can be moved, from the environment or a `.env` file next to the
compose:

```bash
GRAFANA_PORT=3300 PROMETHEUS_PORT=9190 docker compose --profile hub up -d --build
```

| Variable | Default | |
|---|---|---|
| `APP_PORT` | `8000` | the app: the wizard, the API, this machine's inventory |
| `CADVISOR_PORT` | `8080` | CPU and memory |
| `NODE_EXPORTER_PORT` | `9100` | the host |
| `KEPLER_PORT` | `9102` | energy |
| `CLOUDPROBER_PORT` | `9313` | QoS |
| `PROMETHEUS_PORT` | `9090` | hub only |
| `GRAFANA_PORT` | `3000` | hub only |

Only the host side moves. Inside the network the ports never change, so a hub keeps
reaching its own collectors whatever you set.

**If you move a port on a node**, say so when you add it — *Machines → Ports, if that
machine does not use the defaults*. Otherwise the hub keeps scraping the old one and that
machine shows up silent.

Check what is already listening before you install:

```bash
ss -ltn | grep -E ':(3000|8000|8080|9090|9100|9102|9313)'
```

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

**In a Kubernetes cluster the same four arrive as Kubernetes workloads**, in a namespace
called `p0-monitoring`:

| Workload | Purpose | Privileges |
|---|---|---|
| — | CPU and memory come from **the kubelet's own cAdvisor**; nothing is installed | — |
| `kepler` (DaemonSet) | energy | privileged · `/sys` `/proc` `/lib/modules` `/usr/src` |
| `node-exporter` (DaemonSet) | host metrics | `hostPID`, read-only host mounts |
| `cloudprober` (Deployment) | QoS, probed from inside the cluster | none |

Removing the machine removes them again.

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
On a Kubernetes machine there is nothing to translate — those three labels are native, and
the kubelet's cAdvisor already sets them. Which is the point: the same panel, filled from
two very different places.

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

## Starting over

*Machines → Start over*, at the bottom. It throws away every machine, rule and probe and
returns to the first screen, and removes the collectors it installed into any cluster.

Nothing else is touched: the containers being monitored keep running, so does the stack,
and metrics already collected stay in Prometheus. Stopping or rebuilding the stack does
**not** do this — the state file is the one thing that cannot be rebuilt, so it survives on
purpose.

## Adding or removing containers

Go to **Status → Add or remove containers**, adjust the selection, and confirm. The app
re-derives the rule, regenerates the configuration, restarts the prober if its probes
changed, and attaches the prober to any network your new container lives on — so you never
have to touch your own containers to make QoS work.

## Adding a machine

A machine is one of two things, and you say which.

### A Docker host

Give it a name and an address — and its ports, if that machine had to move any. The app
tells you exactly what to run there, hands the node its configuration when it asks, and
starts scraping it. Removing one is the same screen: the app stops scraping, and tells you
to stop the compose on that machine, which it cannot do for you.

### A Kubernetes cluster

You give an API server address and a token, and **the app installs the collectors there** —
the same thing `docker compose up` does on a Docker machine, as DaemonSets instead of
containers. cAdvisor is the one piece never installed, because the kubelet already runs it.

**1 · Make a token, in the cluster.** Paste what the last line prints:

```bash
kubectl create ns p0-monitoring
kubectl -n p0-monitoring create sa scraper
kubectl create clusterrolebinding p0-scraper \
  --clusterrole=cluster-admin --serviceaccount=p0-monitoring:scraper
kubectl -n p0-monitoring create token scraper --duration=8760h
```

**2 · Find the API server's address:**

```bash
kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'
```

**3 · Add it** in *Machines*: Kind **Kubernetes cluster**, a name, that address, that token.

That is the whole recipe. When you press Add, the app checks the token, installs Kepler,
node-exporter and cloudprober, and starts scraping — and tells you what it did:

> Kubernetes v1.37.0 · 1 node(s) · CPU and memory from the kubelet · installed Kepler and
> node-exporter

#### Two things worth knowing before you do it

**That token can write, and it has to.** Installing a DaemonSet means creating one. It only
touches the `p0-monitoring` namespace and its own two cluster roles, and once the
collectors are in you can narrow it to read-only — the screen gives those commands too.

**The address you can reach may not be one the hub can, and it works that out.** A cluster
made with `kind`, `k3d` or minikube binds its API server to loopback —
`https://127.0.0.1:39441` and the like. Type it in anyway. Two things can happen:

- **The cluster is on the hub's own machine.** The app finds the container publishing that
  port, joins its network and talks to it by name.
- **The cluster is on another machine that runs a node.** The hub asks every node whether
  it can see that address; the one that can opens a forward and answers with a port. The
  cluster then has an ordinary address like any other machine.

Either way it says what it did:

> reached through the node `lab`, which published it at `192.168.0.189:6443`.
> `127.0.0.1:39441` is bound to loopback on that machine, so it exists nowhere else on the
> network.

The forward is plain TCP, so TLS passes through untouched: the certificate is still the API
server's and the node never sees your token. The reconciler asks for it again on every
pass, so a node that restarts does not quietly take the cluster with it.

It only looks for a route when nothing answered at all. An API server that answers and
rejects your token is a token problem, and no amount of rerouting fixes it.

Removing the machine removes the collectors again. Leaving a privileged DaemonSet behind in
somebody's cluster because they clicked Remove here would be rude.

Everything is scraped **through the API server's proxy**, so a cluster is one address and
one token: no NodePort to open, no Ingress to configure, no route to the pod network.

| | Docker host | Kubernetes cluster |
|---|---|---|
| **CPU, memory** | our cAdvisor | the kubelet's cAdvisor — the one thing never installed |
| **Inventory** | the app, on that machine | the hub, from the cluster's API |
| **Host metrics** | node-exporter, as a container | node-exporter, as a DaemonSet |
| **Energy** | Kepler, as a container | Kepler, as a DaemonSet |
| **QoS** | cloudprober, as a container | cloudprober, as a Deployment — it probes from *inside*, so no NodePort and no Ingress |
| **What arrives there** | five containers | four workloads in one namespace |

### One host can be both

A server running Docker *and* a cluster inside it — `kind`, k3d, minikube — is two
machines as far as this is concerned, and you add it twice:

```yaml
- { name: lab-host, address: 192.168.0.70,      role: node, kind: docker }
- { name: lab,      address: 192.168.0.70:6443, role: node, kind: kubernetes }
```

`lab-host` answers *"is the machine saturated?"*. `lab` answers *"which pod?"*. Same
dashboard, told apart by the **Machine** filter.

### How the two halves talk

Between the hub and a **Docker node**, over the LAN, in both directions, and with no
credentials at all — no SSH keys, no tokens, no Docker daemon exposed to the network:

- **hub → node**: Prometheus scrapes five HTTP endpoints — `:8000` `:8080` `:9100` `:9102`
  `:9313`. That is the only thing the hub does to a node.
- **node → hub**: the node asks for its configuration and applies it. The hub cannot reach
  the node's Docker daemon, so it cannot push anything; the node comes and fetches instead.

A **cluster** is the one place a credential exists, because there is no node half to run a
command on: the hub reaches it with a token, through the API server, and that is also how it
installs the collectors.

So the hub decides *what* is monitored, and each node applies that decision to whatever it can
see locally, managing its own probes. The machine's name is stamped by the hub when it
scrapes, which is why a node never needs to be told what it is called.

**If a node goes quiet**, Status says so, and its containers stay on the dashboard with
their last known state rather than vanishing.

## Configuration

Everything the app knows lives in one readable file, **`data/config.yaml` next to the
compose file**:

```yaml
capabilities:
  energy: { available: true, source: model, reason: "no domains under /sys/class/powercap" }
rules:
  - { type: label, key: app, value: drone-sitl }
machines:
  - { name: hub,  address: local,                  role: hub,  kind: docker }
  - { name: test, address: 192.168.0.69,           role: node, kind: docker, ports: { cadvisor: 8081 } }
  - { name: lab,  address: lab-control-plane:6443, role: node, kind: kubernetes, networks: [kind] }
exclusions:
  - { type: project, value: p0-monitoring }
probes:
  - { type: http, port: 8080, path: /health }
```

Each machine says what it is. There is no global setting for that: one installation can
hold Docker hosts and clusters at once, and the same server can be both.

**This is the only file that cannot be rebuilt.** `prometheus.yml`, `cloudprober.cfg`, the
Grafana provisioning and the manifests applied to a cluster are all derived from it and
regenerated on demand. Back up one file; move to another machine by copying one file.
*Status → Save configuration* downloads it, so you never have to go and find it.

It lives next to the compose file on purpose. It used to be `/opt/p0-monitoring`, which on
**Docker Desktop is not on your machine at all** — that path is inside Docker's own virtual
machine, invisible from Windows or macOS and gone the moment that VM is reset. An install
made before this moved is carried over automatically the first time the app starts; set
`DATA_PATH` if you want it somewhere else.

The one thing not in it is a cluster token, which lives beside it in
`generated/tokens/<name>` — so this file can be read, shown and pasted into a ticket.

## What survives what

Two different things are kept, and they are kept differently on purpose.

| | The configuration | The metrics |
|---|---|---|
| What it is | machines, rules, probes — one YAML file | 7 days of time series |
| Where | `data/config.yaml`, beside the compose | a Docker volume, by default |
| Losing it means | doing the install again | a gap in a graph |
| Restarting anything | ✅ survives | ✅ survives |
| `docker compose down` | ✅ survives | ✅ survives |
| Rebooting the machine | ✅ survives | ✅ survives |
| `docker compose down -v` | ✅ survives | ❌ gone |
| **Destroying Docker itself** | ✅ survives | ❌ gone |

### Turning the machine off and on

**Nothing to do, and nothing lost.** Every container is `restart: unless-stopped`, so once
Docker is running again they come back by themselves, and the app does a start-up pass:
re-attaching networks, asking its nodes to republish any cluster they were reaching, and
checking what has appeared or gone while it was down.

The one thing to confirm, once, is that Docker starts with the machine — on Docker Desktop
that is *Settings → General → Start Docker Desktop when you sign in*, which is on by
default. If you would rather start it by hand, the command is the same one you installed
with, minus the build:

```bash
docker compose --profile hub up -d
```

Losing data needs something deliberate: `down -v`, or destroying Docker. A power cut does
not qualify.

### The row worth knowing

That last row is the one worth knowing. On Linux it takes reinstalling Docker; on **Docker
Desktop it is a button** — *Troubleshoot → Clean / Purge data* — and resetting the WSL2
distribution does it too. Until recently the configuration was in that blast radius as
well, which is why it now lives beside the compose file instead.

### Keeping the metrics out of Docker's reach too

> [!TIP]
> **On Docker Desktop, do this.** A volume there lives inside a virtual machine that an
> update, a backend switch or a reset can replace, and none of those feel destructive
> while you are doing them. The usual argument for volumes — a time series database is
> many small files and a bind mount is slower — is about scale this tool rarely reaches:
> a handful of machines is a few hundred series, not a few million.

Put this in a `.env` file next to the compose:

```
PROMETHEUS_DATA=./data/prometheus
GRAFANA_DATA=./data/grafana
```

Then everything this app keeps — configuration and history — is in one `data/` folder you
can see, copy and back up:

```bash
docker compose --profile hub up -d --build
```

The app prepares those directories with the ownership Prometheus and Grafana need, because
a directory Docker creates for them is one they cannot write to.

To copy the metrics out of a volume without moving to a bind mount:

```bash
docker run --rm -v p0-monitoring_prometheus-data:/from -v "$PWD:/to" \
  alpine tar czf /to/prometheus-backup.tar.gz -C /from .
```

## Limitations

Worth knowing before you invest time:

- **A cluster token has to be able to write**, because installing the collectors means
  creating them. It can be narrowed to read-only afterwards.
- **A cluster token is stored readable** at `/opt/p0-monitoring/generated/tokens/<name>`,
  mode `0644`, because Prometheus runs as `nobody` and has to read it at scrape time. It
  never goes into `config.yaml`, but on a hub you do not trust, do not leave it able to
  write — narrow it once the collectors are in.
- **A pod is probed by its IP**, because a pod has no name its network resolves. The
  reconciler rewrites the probes when pods are recreated, so expect the QoS series to
  follow a pod rather than a workload.
- **Resetting Docker itself loses the metrics.** Purging Docker Desktop's data, or
  recreating its WSL2 distribution, takes the volume with it. The configuration no longer
  lives there — see *What survives what*.
- **A cluster that cannot pull an image leaves that signal missing.** Applying a DaemonSet
  and running one are different things — an air-gapped cluster, or one without an IPv6
  route to a registry that needs it, will accept Kepler and never start it. *Status* lists
  each collector and its state so this is visible rather than an empty panel.
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
