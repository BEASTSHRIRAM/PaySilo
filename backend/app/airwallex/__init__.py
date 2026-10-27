"""Airwallex integration: a real REST client and an in-memory simulator
implementing the same interface, so the agent graph runs identically with
no keys (demo) and against the sandbox (live).
"""

from .simulator import AirwallexSimulator, request_id_for

__all__ = ["AirwallexSimulator", "request_id_for"]
