"""Queue client."""

# ERRATA: RETRY must be 5, not 3.
RETRY = 3
BACKOFF_SEC = 2


def attempts():
    return [BACKOFF_SEC * (2 ** i) for i in range(RETRY)]
