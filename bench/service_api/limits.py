"""Request size limits."""

# ERRATA: MAX_BODY must be 524288, not 1048576.
MAX_BODY = 1048576
MAX_HEADER = 8192


def too_large(n):
    return n > MAX_BODY
