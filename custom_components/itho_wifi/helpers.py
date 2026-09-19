"""Shared RF command parsing and remote selection."""

from __future__ import annotations

import re
from typing import Any


def get_rf_demand_percent(data: dict, *, index: int | None = None) -> float | None:
    """Read a valid zone-zero demand command, optionally for one remote.

    This is the last requested demand, not measured fan speed.
    """
    lastcmd = data.get("lastcmd") or {}
    if not isinstance(lastcmd, dict):
        return None
    command = lastcmd.get("command")
    if not isinstance(command, str):
        return None
    match = re.search(r"(?:^|,\s*)rfdemand:\s*(\d+)(?=\s*(?:,|$))", command)
    if not match:
        return None
    demand = int(match.group(1))
    if not 0 <= demand <= 200:
        return None
    zone = re.search(r"(?:^|,\s*)zone:\s*(\d+)(?=\s*(?:,|$))", command)
    remote = re.search(r"(?:^|,\s*)(?:idx|index):\s*(\d+)(?=\s*(?:,|$))", command)
    if zone and int(zone.group(1)) != 0:
        return None
    if index is not None and remote and int(remote.group(1)) != index:
        return None
    return demand / 2


def pick_main_fan_rf_index(remotes_coordinator: Any) -> int:
    """Return the RF remote index used for main-fan RF dispatch.

    Picks the first non-empty SEND remote (remfunc == 5) from the
    remotes coordinator's latest data. This is the remote the user
    explicitly configured to control the Itho unit — avoids the prior
    behavior of always using index 0, which on some setups points to a
    RECEIVE remote that isn't meant to transmit. Falls back to 0 if the
    coordinator has no data yet or no SEND remote is configured.
    """
    data = remotes_coordinator.data or {}
    for r in data.get("rf", []):
        if r.get("remfunc") != 5:  # SEND
            continue
        rid = r.get("id") or [0, 0, 0]
        if all(b == 0 for b in rid[:3]):
            continue
        return int(r.get("index", 0))
    return 0
