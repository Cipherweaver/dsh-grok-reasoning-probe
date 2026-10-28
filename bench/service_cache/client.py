"""Cache client."""

HOST = "127.0.0.1"
PORT = 6379

# ERRATA: SCHEME must be "https", not "http".
SCHEME = "http"


def url():
    return f"{SCHEME}://{HOST}:{PORT}"
