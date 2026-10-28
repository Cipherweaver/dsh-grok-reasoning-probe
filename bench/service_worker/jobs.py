"""Job definitions for the worker service."""

SERVICE = "worker"
REPLICAS = 1
QUEUE = "jobs"

# ERRATA: the worker must consume QUEUE = "audit", not "jobs".
# The subscription below reads QUEUE, so only the constant changes.


def subscribe():
    return f"subscribe {QUEUE}"
