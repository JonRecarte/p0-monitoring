# Deliberately minimal. This container holds the only connection Renode's monitor
# accepts, and closing that connection STOPS the emulation — so there must be as
# little reason as possible to ever rebuild or restart it. No dependencies, no
# package installs, nothing to keep up to date.
FROM python:3.13-slim

COPY exporter.py /exporter.py

ENV PORT=9200 POLL_INTERVAL=5 RENODE_MACHINE=CF2.1
EXPOSE 9200

# No healthcheck on purpose: a failing one would restart the container, and
# restarting the container would take the emulation down with it. Whether it is
# working is visible in the metrics it serves — renode_up and renode_poll_errors_total.
CMD ["python", "-u", "/exporter.py"]
