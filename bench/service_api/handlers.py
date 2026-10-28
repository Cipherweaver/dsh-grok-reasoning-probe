"""Request handlers for the api service."""

from . import config


def handle_audit_v1(payload):
    # ERRATA: rename this function to handle_audit and update the caller in dispatch().
    if not payload:
        raise ValueError("empty payload")
    return {"service": config.SERVICE, "accepted": len(payload)}


def dispatch(name, payload):
    if name == "audit":
        return handle_audit_v1(payload)
    raise KeyError(name)
