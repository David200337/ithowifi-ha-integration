def get_rf_demand_percent(data: dict) -> int | None:
    lastcmd = data.get("lastcmd", {})
    command = str(lastcmd.get("command", ""))

    match = re.search(r"\brfdemand:(\d+)", command)
    if not match:
        return None

    demand = max(0, min(int(match.group(1)), 200))
    return round(demand / 2)