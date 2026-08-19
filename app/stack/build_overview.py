"""Builds overview.json, the dashboard the app provisions.

Written as a script and not by hand: ~40 panels of JSON are unmaintainable otherwise.
Run it and commit the output:   python3 build_overview.py

Modelled on a Kubernetes fleet overview, adapted to Docker:
  - pod / namespace come from the Compose service / project (translated at scrape time)
  - there is no kube-state-metrics, so state, image and ip come from container_info,
    the inventory series the app publishes
  - energy needs the target_info join, because Kepler does not know container names
"""
import json

DS = {"type": "prometheus", "uid": "prometheus"}
NS = 'namespace=~"$namespace"'


POD_VAR = 'pod=~"$pod"'


def sel(*extra):
    """A PromQL label selector with both dashboard filters applied."""
    return "{" + ", ".join([NS, POD_VAR] + [e for e in extra if e]) + "}"


# Every container panel is restricted to what is actually monitored. target_info holds
# exactly the targets the membership rule matched, so joining against it keeps the
# monitoring stack and the app itself out of the graphs.
def only_monitored():
    return f' and on (pod, namespace) target_info{sel()}'


def monitored_join(fn):
    """Kepler knows no container names: identity and filtering both come from the join."""
    return (f'sum by(namespace,pod) ({fn}(kepler_container_package_joules_total[5m]) '
            f'* on(container_id) group_left(pod,namespace) target_info{sel()})')


def selraw(*parts):
    return "{" + ", ".join(p for p in parts if p) + "}"
# lines with fill, no point markers
LINE = {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 18, "gradientMode": "opacity",
        "showPoints": "never", "lineInterpolation": "linear", "spanNulls": True,
        "axisSoftMin": 0, "scaleDistribution": {"type": "linear"}}
LEGEND = {"displayMode": "list", "placement": "bottom", "showLegend": True, "calcs": []}
TOOLTIP = {"mode": "multi", "sort": "desc"}

_y = 0
panels = []


def _next_y(h):
    global _y
    y = _y
    _y += h
    return y


def row(title):
    panels.append({"type": "row", "title": title, "collapsed": False,
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": _next_y(1)}, "panels": []})


def stat(title, expr, unit="none", w=6, h=4, y=None, color="green"):
    panels.append({
        "type": "stat", "title": title, "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": stat.x, "y": y},
        "fieldConfig": {"defaults": {"unit": unit, "color": {"mode": "fixed", "fixedColor": color},
                                     "mappings": []}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "textMode": "value", "colorMode": "value", "graphMode": "none",
                    "justifyMode": "center"},
        # `or vector(0)` so an empty match shows 0 instead of "No data"
        "targets": [{"datasource": DS, "expr": f"({expr}) or vector(0)",
                     "refId": "A", "instant": True}]})
    stat.x += w


def ts(title, expr, unit, legend="{{namespace}}/{{pod}}", w=16, h=9, y=None, extra=None):
    p = {"type": "timeseries", "title": title, "datasource": DS,
         "gridPos": {"h": h, "w": w, "x": ts.x, "y": y},
         "fieldConfig": {"defaults": {"unit": unit, "custom": dict(LINE), "min": 0},
                         "overrides": []},
         "options": {"legend": dict(LEGEND), "tooltip": dict(TOOLTIP)},
         "targets": [{"datasource": DS, "expr": expr, "refId": "A", "legendFormat": legend}]}
    if extra:
        p["fieldConfig"]["defaults"].update(extra)
    panels.append(p)
    ts.x += w


def topn(title, expr, unit, n=5, legend="{{namespace}}/{{pod}}", w=8, h=9, y=None):
    """A Top N bar gauge, sorted by value.

    A bar gauge draws one bar per series in whatever order the query returns them, which
    for Prometheus is by label, not by value. Reducing the series to rows and sorting that
    single frame is what puts the biggest bar on top.
    """
    panels.append({
        "type": "bargauge", "title": title, "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": topn.x, "y": y},
        # all bars the same green: the gradient read as a severity scale, which it is not
        "fieldConfig": {"defaults": {"unit": unit, "min": 0,
                                     "color": {"mode": "fixed", "fixedColor": "green"},
                                     "mappings": []},
                        "overrides": []},
        "options": {"displayMode": "basic", "orientation": "horizontal",
                    # values: one bar per ROW of the reduced frame, not per field
                    "reduceOptions": {"calcs": [], "fields": "", "values": True},
                    "showUnfilled": True, "valueMode": "color"},
        "transformations": [
            {"id": "reduce", "options": {"reducers": ["lastNotNull"],
                                         "mode": "seriesToRows"}},
            {"id": "sortBy", "options": {"fields": {},
                                         "sort": [{"field": "Last *", "desc": True}]}}],
        "targets": [{"datasource": DS, "expr": f"topk({n}, {expr})", "refId": "A",
                     "legendFormat": legend}]})
    topn.x += w


def pair(title_ts, title_top, expr, unit, n=5):
    """A timeseries plus its Top N, side by side."""
    y = _next_y(9)
    ts.x = 0
    ts(title_ts, expr, unit, y=y)
    topn.x = 16
    topn(title_top, expr, unit, n=n, y=y)


# ---------------------------------------------------------------- expressions
POD = 'pod!=""'
M = only_monitored()
CPU   = f'sum by(namespace,pod) (rate(container_cpu_usage_seconds_total{sel(POD)}[5m]){M}) * 1000'
MEM   = f'sum by(namespace,pod) (container_memory_working_set_bytes{sel(POD)}{M}) / 1024 / 1024'
POWER = monitored_join("rate")
ENER  = monitored_join("increase")
RX    = f'sum by(namespace,pod) (rate(container_network_receive_bytes_total{sel(POD)}[5m]){M}) * 8'
TX    = f'sum by(namespace,pod) (rate(container_network_transmit_bytes_total{sel(POD)}[5m]){M}) * 8'
DR    = f'sum by(namespace,pod) (rate(container_fs_reads_bytes_total{sel(POD)}[5m]){M})'
DW    = f'sum by(namespace,pod) (rate(container_fs_writes_bytes_total{sel(POD)}[5m]){M})'

# ---------------------------------------------------------------- overview
row("Overview")
_y_ov = _next_y(4)
stat.x = 0
MON         = 'monitored="1"'
RUNNING     = 'state="running"'
NOT_RUNNING = 'state!="running"'
stat("Monitored containers", f'count(container_info{sel(MON)})',              y=_y_ov, w=8, color="text")
stat("Running",              f'count(container_info{sel(MON, RUNNING)})',     y=_y_ov, w=8, color="green")
stat("Not running",          f'count(container_info{sel(MON, NOT_RUNNING)})', y=_y_ov, w=8, color="red")

row("Containers")
# ---------------------------------------------------------------- inventory
# order matters: it is the column order of the table. Energy right after memory.
INV_NUM = [
    ("cpu (mcores)",    f'sum by(pod) (rate(container_cpu_usage_seconds_total{sel(POD)}[5m]){M}) * 1000'),
    ("memory (MiB)",    f'sum by(pod) (container_memory_working_set_bytes{sel(POD)}{M}) / 1024 / 1024'),
    ("power (W)",       f'sum by(pod) (rate(kepler_container_package_joules_total[5m]) '
                        f'* on(container_id) group_left(pod) target_info{sel()})'),
    ("net rx (kb/s)",   f'sum by(pod) (rate(container_network_receive_bytes_total{sel(POD)}[5m]){M}) * 8 / 1000'),
    ("net tx (kb/s)",   f'sum by(pod) (rate(container_network_transmit_bytes_total{sel(POD)}[5m]){M}) * 8 / 1000'),
    ("disk read (B/s)", f'sum by(pod) (rate(container_fs_reads_bytes_total{sel(POD)}[5m]){M})'),
    ("disk write (B/s)",f'sum by(pod) (rate(container_fs_writes_bytes_total{sel(POD)}[5m]){M})')]

targets = [{"datasource": DS, "expr": f'container_info{sel(MON)}', "refId": "A",
            "instant": True, "format": "table"}]
rename = {"namespace": "namespace", "pod": "container", "ip": "ip", "state": "state",
          "image": "image", "monitored": "monitored"}
for i, (label, expr) in enumerate(INV_NUM):
    rid = chr(ord("B") + i)
    targets.append({"datasource": DS, "expr": expr, "refId": rid,
                    "instant": True, "format": "table"})
    rename[f"Value #{rid}"] = label

panels.append({
    "type": "table", "title": "Containers inventory and resource usage", "datasource": DS,
    "gridPos": {"h": 11, "w": 24, "x": 0, "y": _next_y(11)},
    "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"},
                                            "filterable": True}}, "overrides": [
        {"matcher": {"id": "byName", "options": lbl},
         "properties": [{"id": "unit", "value": unit},
                        {"id": "decimals", "value": dec}]}
        for lbl, unit, dec in [("cpu (mcores)", "none", 2), ("memory (MiB)", "none", 1),
                               ("power (W)", "watt", 2), ("net rx (kb/s)", "none", 2),
                               ("net tx (kb/s)", "none", 2), ("disk read (B/s)", "Bps", 0),
                               ("disk write (B/s)", "Bps", 0)]]},
    "options": {"cellHeight": "sm", "showHeader": True, "footer": {"show": False},
                "sortBy": [{"desc": True, "displayName": "cpu (mcores)"}]},
    "transformations": [
        {"id": "joinByField", "options": {"byField": "pod", "mode": "outer"}},
        {"id": "organize", "options": {
            "excludeByName": {"Time": True, "__name__": True, "container_id": True, "id": True,
                              "instance": True, "job": True, "cluster": True, "Value #A": True,
                              "name": True, "monitored": True},
            "renameByName": rename,
            # explicit order: otherwise it depends on how the join happens to emit fields
            "indexByName": {f: i for i, f in enumerate(
                ["namespace", "pod", "ip", "state", "image"] +
                [f"Value #{chr(ord('B') + n)}" for n in range(len(INV_NUM))])}}}],
    "targets": targets})

# ---------------------------------------------------------------- signals
row("CPU")
pair("CPU by container (millicores)", "Top 5 by CPU (mcores)", CPU, "none")

row("Memory")
pair("Memory working set by container (MiB)", "Top 5 by memory (MiB)", MEM, "none")

row("Energy")
pair("Power by container (W)", "Top 5 by power (W)", POWER, "watt")
pair("Energy by container over 5m window (J)", "Top 5 by energy (J/5m)", ENER, "joule")

row("Network")
pair("Network RX by container (bps)", "Top 5 by RX (bps)", RX, "bps")
pair("Network TX by container (bps)", "Top 5 by TX (bps)", TX, "bps")

row("Disk")
pair("Disk read by container (B/s)", "Top 5 by disk read (B/s)", DR, "Bps")
pair("Disk write by container (B/s)", "Top 5 by disk write (B/s)", DW, "Bps")

# ---------------------------------------------------------------- QoS
row("QoS / cloudprober")
QOS = [
 ("latency_ms", "ms", '(sum by(probe,dst) (rate(latency{job="cloudprober"}[$__rate_interval])) / clamp_min(sum by(probe,dst) (rate(total{job="cloudprober"}[$__rate_interval])), 0.000001)) / 1000'),
 ("jitter_ms", "ms", 'stddev_over_time( ( ( sum by (probe,dst) (increase(latency{job="cloudprober"}[15s])) / clamp_min(sum by (probe,dst) (increase(success{job="cloudprober"}[15s])), 1) ) / 1000 )[5m:15s] )'),
 ("success_rate", "percent", '100 * sum by(probe,dst) (rate(success{job="cloudprober"}[$__rate_interval])) / clamp_min(sum by(probe,dst) (rate(total{job="cloudprober"}[$__rate_interval])), 0.000001)'),
 ("error_rate", "percent", '100 * sum by(probe,dst) (rate(failure{job="cloudprober"}[$__rate_interval])) / clamp_min(sum by(probe,dst) (rate(total{job="cloudprober"}[$__rate_interval])), 0.000001)'),
 ("timeout_rate", "percent", '100 * sum by(probe,dst) (rate(timeouts{job="cloudprober"}[$__rate_interval])) / clamp_min(sum by(probe,dst) (rate(total{job="cloudprober"}[$__rate_interval])), 0.000001)'),
 ("request_loss_rate", "percent", '100 * (sum by(probe,dst) (rate(failure{job="cloudprober"}[$__rate_interval])) + sum by(probe,dst) (rate(timeouts{job="cloudprober"}[$__rate_interval]))) / clamp_min(sum by(probe,dst) (rate(total{job="cloudprober"}[$__rate_interval])), 0.000001)'),
 ("throughput_probes_per_min", "none", '60 * sum by(probe,dst) (rate(total{job="cloudprober"}[$__rate_interval]))'),
 ("http_200_per_min", "none", '60 * sum by(probe,dst) (rate(resp_code{job="cloudprober", code="200"}[$__rate_interval]))'),
 ("failures_per_min", "none", '60 * sum by(probe,dst) (rate(failure{job="cloudprober"}[$__rate_interval]))'),
 ("timeouts_per_min", "none", '60 * sum by(probe,dst) (rate(timeouts{job="cloudprober"}[$__rate_interval]))'),
]
for i in range(0, len(QOS), 2):
    y = _next_y(7); ts.x = 0
    for title, unit, expr in QOS[i:i+2]:
        extra = {"max": 100} if unit == "percent" else None
        ts(title, expr, unit, legend="{{probe}}", w=12, h=7, y=y, extra=extra)

# ---------------------------------------------------------------- the machine
row("Virtual machine")
NODE = 'job="node"'
MNT = ('mountpoint="/"',)   # --path.rootfs strips the /rootfs prefix
NODEF = selraw(NODE)
FS = selraw(NODE, *MNT)
VM = [
 ("CPU usage (%)", "percent", f'100 - (avg(rate(node_cpu_seconds_total{selraw(NODE, chr(34).join(["mode=", "idle", ""]))}[5m])) * 100)'),
 ("CPU usage (cores)", "none", f'sum(rate(node_cpu_seconds_total{selraw(NODE, chr(34).join(["mode!=", "idle", ""]))}[5m]))'),
 ("RAM usage (%)", "percent", f'100 * (1 - (node_memory_MemAvailable_bytes{NODEF} / node_memory_MemTotal_bytes{NODEF}))'),
 ("RAM used (GiB)", "none", f'(node_memory_MemTotal_bytes{NODEF} - node_memory_MemAvailable_bytes{NODEF}) / 1024 / 1024 / 1024'),
 ("Disk usage (%)", "percent", f'100 - (node_filesystem_avail_bytes{FS} / node_filesystem_size_bytes{FS} * 100)'),
 ("Disk used (GiB)", "none", f'(node_filesystem_size_bytes{FS} - node_filesystem_avail_bytes{FS}) / 1024 / 1024 / 1024'),
 ("Host power over time (W)", "watt", 'sum(rate(kepler_node_package_joules_total[5m]))'),
 ("Host energy over 5m window (J)", "joule", 'sum(increase(kepler_node_package_joules_total[5m]))'),
]
for i in range(0, len(VM), 2):
    y = _next_y(7); ts.x = 0
    for title, unit, expr in VM[i:i+2]:
        extra = {"max": 100} if unit == "percent" else None
        ts(title, expr, unit, legend="$machine", w=12, h=7, y=y, extra=extra)

# ---------------------------------------------------------------- dashboard
dash = {
    "uid": "p0m-overview", "title": "Overview", "tags": ["p0-monitoring"],
    "timezone": "browser", "refresh": "10s", "schemaVersion": 39, "editable": True,
    "time": {"from": "now-1h", "to": "now"},
    "templating": {"list": [
        {"name": "namespace", "label": "Namespace", "type": "query", "datasource": DS,
         "query": "label_values(target_info, namespace)", "refresh": 2,
         "includeAll": True, "multi": True,
         "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "options": []},
        {"name": "pod", "label": "Container", "type": "query", "datasource": DS,
         "query": 'label_values(target_info{namespace=~"$namespace"}, pod)', "refresh": 2,
         "includeAll": True, "multi": True,
         "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "options": []},
        {"name": "machine", "label": "Machine", "type": "query", "datasource": DS,
         "query": "label_values(container_cpu_usage_seconds_total, cluster)", "refresh": 2,
         "includeAll": False, "multi": False, "hide": 2, "current": {}, "options": []},
    ]},
    "panels": panels,
}
json.dump(dash, open("overview.json", "w"), ensure_ascii=False, indent=1)
print(f"overview.json: {len([p for p in panels if p['type'] != 'row'])} paneles + "
      f"{len([p for p in panels if p['type'] == 'row'])} filas")
