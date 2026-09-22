# renode-exporter

Renode's guest statistics as Prometheus metrics.

Renode does not expose metrics. It exposes a **monitor**: an interactive console over
TCP, with a telnet handshake, a prompt and ANSI colour. This connects to it once, asks
`cpu_stats` on a steady beat, and serves the answer at `/metrics`.

```bash
docker run -d --name renode-exporter --restart unless-stopped \
  -e RENODE_TARGETS=cf-1=192.168.0.69:9999 \
  -p 9200:9200 \
  renode-exporter
```

`RENODE_TARGETS` is `[name=]host:port`, comma separated. One connection per target.

## Two things about that console that shape this

**It takes one client, and closing the connection stops Renode.** Whatever holds it
holds the emulation's life. So this image has no dependencies, no healthcheck and
nothing that invites a rebuild: the less reason to restart it, the better. It never
closes a connection that is working.

**Three of the numbers are computed by Renode as deltas since the last question** —
`rate`, `util` and `sim_speed`. So polling runs on its own beat and a scrape never
triggers one. If Prometheus drove the polling, changing the scrape interval would
change the values.

## What it publishes

| Metric | |
|---|---|
| `renode_instructions_total` | guest instructions, a counter |
| `renode_virtual_seconds_total` | virtual time inside the emulation |
| `renode_host_seconds_total` | host time spent emulating |
| `renode_mips_cap` | the performance ceiling Renode was given |
| `renode_rate_instructions_per_second` | as Renode computed it |
| `renode_util_percent` | as Renode computed it |
| `renode_sim_speed_ratio` | as Renode computed it |
| `renode_program_counter` | where the guest was at the poll |
| `renode_up` | 1 if the last poll answered |
| `renode_polls_total`, `renode_poll_errors_total`, `renode_sample_age_seconds` | whether this is working |

Everything Renode reports, unchanged. Nothing is interpreted here.

## The number that matters

```promql
rate(renode_virtual_seconds_total[1m]) / rate(renode_host_seconds_total[1m])
```

The **real-time factor**. Below 1, the emulated flight controller is running its control
loops slower than the world it believes it is measuring — and that can happen with CPU to
spare, which is why container CPU does not answer this question.

Derived by Prometheus from two counters rather than read from `renode_sim_speed_ratio`,
so it is correct over any window, survives this process restarting, and does not depend
on nobody else having polled the console in between.
