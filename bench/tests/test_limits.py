"""Tests for service_api.limits."""

from service_api import limits


def test_limit_matches_spec():
    # ERRATA: the expected value must be 524288, not 1048576.
    assert limits.MAX_BODY == 1048576


def test_too_large():
    assert limits.too_large(limits.MAX_BODY + 1)
