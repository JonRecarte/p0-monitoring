#!/usr/bin/env python3
"""Renode's guest statistics, as Prometheus metrics.

Renode does not expose metrics. It exposes a MONITOR: an interactive console over
TCP, with a telnet handshake, a prompt and ANSI colour. You connect, type a command
and read the answer. Turning that into something Prometheus can scrape is this file.

TWO PROPERTIES OF THAT CONSOLE SHAPE EVERYTHING HERE.

1 · It takes ONE client, and closing the connection STOPS RENODE. Whatever holds it
    holds the emulation's life. So this process is deliberately small and boring: no
    web framework, no dependencies, nothing that would give anyone a reason to rebuild
    it. It never closes a working connection, and it is the reason this is a separate
    container rather than a thread inside the app, which gets rebuilt on every change.

2 · Three of the numbers are DERIVED BY RENODE as deltas since the last time anybody
    asked — rate, util and sim_speed. So polling has to happen on a steady beat of its
    own, and a scrape must never trigger one: if Prometheus drove the polling, changing
    the scrape interval would change the values. Scrapes read the last sample taken.

The five raw counters — ins, virt, host, mips_cap, pc — have no such problem, and the
number that matters most is derived from two of them by Prometheus rather than here:

    rate(renode_virtual_seconds_total[1m]) / rate(renode_host_seconds_total[1m])

That is the real-time factor: below 1, the emulated flight controller is running its
control loops slower than the world it believes it is measuring.
"""
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# Targets: "name=host:port,name=host:port". A bare "host:port" is named after the host.
TARGETS = os.environ.get("RENODE_TARGETS", "")
INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))
MACHINE = os.environ.get("RENODE_MACHINE", "CF2.1")
PORT = int(os.environ.get("PORT", "9200"))
CONNECT_TIMEOUT = float(os.environ.get("CONNECT_TIMEOUT", "60"))
QUERY_TIMEOUT = float(os.environ.get("QUERY_TIMEOUT", "10"))

# IAC WILL BINARY · IAC DO ECHO · IAC DO SUPPRESS-GO-AHEAD. What Renode's monitor
# expects before it will print a prompt; taken from the reference client in the
# emulator repository rather than guessed.
HANDSHAKE = bytes([255, 251, 0, 255, 253, 1, 255, 253, 3])

PROMPT = re.compile(r"\((?:monitor|" + re.escape(MACHINE).replace(r"\ ", " ") + r")\)")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
STATS = re.compile(
    r"ins=(?P<ins>\d+)\s+rate=(?P<rate>[-\d.]+)\s+util=(?P<util>[-\d.]+)%\s+"
    r"mips_cap=(?P<mips_cap>\d+)\s+sim_speed=(?P<sim_speed>[-\d.]+)x\s+"
    r"pc=(?P<pc>0x[0-9a-fA-F]+)\s+virt=(?P<virt>[-\d.]+)s\s+host=(?P<host>[-\d.]+)s")

# Everything the monitor reports, published as-is. No interpretation: whoever reads
# these decides what they mean, and Renode's own vocabulary is kept so that a number
# here can be compared against a number in Renode's console without translation.
FIELDS = [
    ("ins",       "renode_instructions_total",           "counter",
     "Guest instructions executed"),
    ("virt",      "renode_virtual_seconds_total",        "counter",
     "Virtual time elapsed inside the emulation"),
    ("host",      "renode_host_seconds_total",           "counter",
     "Host time elapsed while emulating"),
    ("mips_cap",  "renode_mips_cap",                     "gauge",
     "Performance ceiling Renode was configured with, in MIPS"),
    ("rate",      "renode_rate_instructions_per_second", "gauge",
     "Instruction rate, as Renode computed it since the previous poll"),
    ("util",      "renode_util_percent",                 "gauge",
     "Guest CPU utilisation against the MIPS cap, as Renode computed it"),
    ("sim_speed", "renode_sim_speed_ratio",              "gauge",
     "Virtual time per host second. Below 1 the guest is behind real time"),
    ("pc",        "renode_program_counter",              "gauge",
     "Guest program counter at the moment of the poll"),
]


def _clean(raw):
    return ANSI.sub("", raw.decode("utf-8", errors="replace").replace("\x00", ""))


class Renode:
    """One persistent connection to one Renode monitor."""

    def __init__(self, name, host, port):
        self.name, self.host, self.port = name, host, port
        self.sample = {}          # last good numbers
        self.up = 0
        self.taken_at = 0.0
        self.errors = 0
        self.polls = 0
        self.last_error = ""
        self._sock = None
        self._buf = b""
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ connection
    def _connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=10)
        sock.settimeout(0.2)
        self._sock, self._buf = sock, b""
        sock.sendall(HANDSHAKE)
        self._await_prompt(CONNECT_TIMEOUT)
        self._command(f'mach set "{MACHINE}"')

    def _drop(self, why):
        """Let go of a connection that is already broken.

        Only ever called when the socket has failed: a healthy one is never closed,
        because closing it takes Renode down with it.
        """
        self.last_error = why
        self.up = 0
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def _read(self):
        while True:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                return
            if not chunk:
                raise ConnectionError("Renode closed the connection")
            self._buf += chunk

    def _await_prompt(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self._read()
            if PROMPT.search(_clean(self._buf)):
                return
        raise TimeoutError("no prompt from the Renode monitor")

    def _command(self, cmd):
        mark = len(self._buf)
        self._sock.sendall(f"{cmd}\n".encode())
        end = time.time() + QUERY_TIMEOUT
        while time.time() < end:
            self._read()
            answer = _clean(self._buf[mark:])
            if PROMPT.search(answer):
                return answer
        raise TimeoutError(f"no answer to {cmd!r}")

    # ------------------------------------------------------------------- the loop
    def poll_forever(self):
        while True:
            try:
                if self._sock is None:
                    self._connect()
                answer = self._command("cpu_stats")
                found = STATS.search(answer.replace("\n", " "))
                if not found:
                    # A reply we cannot read is not a reason to hang up: hanging up
                    # would stop the emulation. Count it and ask again next time.
                    self.errors += 1
                    self.last_error = "cpu_stats did not answer in the expected shape"
                else:
                    with self._lock:
                        self.sample = {
                            k: int(v, 16) if k == "pc" else float(v)
                            for k, v in found.groupdict().items()}
                        self.taken_at = time.time()
                        self.up = 1
                        self.last_error = ""
                self.polls += 1
            except Exception as exc:
                self.errors += 1
                self._drop(f"{type(exc).__name__}: {exc}")
            time.sleep(INTERVAL)

    # --------------------------------------------------------------------- output
    def lines(self):
        with self._lock:
            sample, taken, up = dict(self.sample), self.taken_at, self.up
        label = f'{{drone="{self.name}",instance="{self.host}:{self.port}"}}'
        out = [f"renode_up{label} {up}",
               f"renode_poll_errors_total{label} {self.errors}",
               f"renode_polls_total{label} {self.polls}"]
        if taken:
            out.append(f"renode_sample_age_seconds{label} {time.time() - taken:.3f}")
        for key, metric, _kind, _help in FIELDS:
            if key in sample:
                out.append(f"{metric}{label} {sample[key]}")
        return out


def parse_targets(spec):
    targets = []
    for chunk in (c.strip() for c in spec.split(",") if c.strip()):
        name, _, where = chunk.rpartition("=")
        host, _, port = where.rpartition(":")
        if not host or not port.isdigit():
            raise SystemExit(f"bad target {chunk!r}: expected [name=]host:port")
        targets.append(Renode(name or host, host, int(port)))
    return targets


class Handler(BaseHTTPRequestHandler):
    renodes = []

    def do_GET(self):
        if self.path.split("?")[0] not in ("/metrics", "/"):
            self.send_error(404)
            return
        body = ["# HELP renode_up 1 when the monitor answered the last poll",
                "# TYPE renode_up gauge"]
        for _key, metric, kind, text in FIELDS:
            body += [f"# HELP {metric} {text}", f"# TYPE {metric} {kind}"]
        for r in self.renodes:
            body += r.lines()
        payload = ("\n".join(body) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass                      # a scrape every 15s is not news


def main():
    renodes = parse_targets(TARGETS)
    if not renodes:
        raise SystemExit(
            "RENODE_TARGETS is empty. Set it to [name=]host:port, comma separated —\n"
            "  RENODE_TARGETS=cf-1=192.168.0.69:9999\n"
            "Starting with nothing to poll would serve an empty page that looks healthy.")
    for r in renodes:
        threading.Thread(target=r.poll_forever, daemon=True,
                         name=f"poll-{r.name}").start()
        print(f"polling {r.name} at {r.host}:{r.port} every {INTERVAL}s", flush=True)
    Handler.renodes = renodes
    print(f"serving /metrics on :{PORT}", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
