"""Synthetic admission for fake-Claude adapter regressions, including detached keepers.

Only helpers.base_env with a fake Claude executable adds this directory to PYTHONPATH.
It is not part of cli_relay, is never installed, and supplies no production override.
Routing/rejection tests run against the real policy without this directory.
"""
from cli_relay import policy

policy.TIERS["highest_quality"] = (("claude", "claude-opus-5-5", "max"),)
