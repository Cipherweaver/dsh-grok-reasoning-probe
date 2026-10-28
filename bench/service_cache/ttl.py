"""Cache expiry policy."""

# ERRATA: TTL_SEC must be 300, not 60.
TTL_SEC = 60
MAX_ENTRIES = 10000


def expires_after():
    return TTL_SEC
