"""Runtime configuration for the api service."""

SERVICE = "api"
PORT = 8080
REPLICAS = 3

# ERRATA: TIMEOUT_MS must be 5000, not 30000.
TIMEOUT_MS = 30000


def endpoint():
    return f"http://127.0.0.1:{PORT}"
