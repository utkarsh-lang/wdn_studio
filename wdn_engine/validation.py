"""Model health checks run right after loading a network."""

from __future__ import annotations

from typing import Any

from .model import NetworkModel
from .topology import characterization

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _issue(severity: str, code: str, message: str, kind: str | None = None, elements: list[str] | None = None) -> dict[str, Any]:
    elements = list(elements or [])
    return {"severity": severity, "code": code, "message": message, "element_kind": kind,
            "count": len(elements), "elements": elements[:500]}


def validate(model: NetworkModel) -> dict[str, Any]:
    wn = model.wn
    issues: list[dict[str, Any]] = []
    pipes = model.pipes()
    headloss = str(wn.options.hydraulic.headloss)

    if model.case_collisions:
        issues.append(_issue("warning", "case_collision",
                             "IDs that differ only by letter case; text lookups resolve to the first one.",
                             None, [f"{a}/{b}" for a, b in model.case_collisions]))
    if not model.sources:
        issues.append(_issue("error", "no_source", "The network has no reservoir or tank, so it cannot be simulated."))

    bad_len = [p["id"] for p in pipes if not p["length_m"] or p["length_m"] <= 0]
    if bad_len:
        issues.append(_issue("error", "pipe_length", "Pipes with zero or negative length.", "link", bad_len))
    bad_dia = [p["id"] for p in pipes if not p["diameter_mm"] or p["diameter_mm"] <= 0]
    if bad_dia:
        issues.append(_issue("error", "pipe_diameter", "Pipes with zero or negative diameter.", "link", bad_dia))
    odd_dia = [p["id"] for p in pipes if p["diameter_mm"] and (p["diameter_mm"] < 10 or p["diameter_mm"] > 5000)]
    if odd_dia:
        issues.append(_issue("warning", "pipe_diameter_range",
                             "Pipe diameters outside 10–5000 mm; check units of the source file.", "link", odd_dia))
    long_pipes = [p["id"] for p in pipes if p["length_m"] and p["length_m"] > 5000]
    if long_pipes:
        issues.append(_issue("info", "long_pipes", "Pipes longer than 5 km (often transmission mains).", "link", long_pipes))
    if headloss == "H-W":
        odd_c = [p["id"] for p in pipes if p["roughness"] is not None and not (40 <= p["roughness"] <= 160)]
        if odd_c:
            issues.append(_issue("warning", "hw_roughness", "Hazen-Williams C outside the typical 40–160 range.", "link", odd_c))
    elif any((p["roughness"] or 0) <= 0 for p in pipes):
        issues.append(_issue("warning", "roughness", "Non-positive roughness values.", "link",
                             [p["id"] for p in pipes if (p["roughness"] or 0) <= 0]))

    char = characterization(model)
    if char["isolated_nodes"]:
        issues.append(_issue("error", "isolated", "Nodes with no connected link.", "node", char["isolated_nodes"]))
    if not char["connected"]:
        issues.append(_issue("warning", "components",
                             f"The network has {char['number_of_components']} disconnected parts (sizes {char['component_sizes'][:6]}).", "node"))
    sc = char["source_connectivity"]
    if sc["number_unsupplied"]:
        issues.append(_issue("error", "unsupplied",
                             f"Junctions with no open path to a reservoir or tank ({sc['unsupplied_base_demand_lps']} L/s base demand).",
                             "node", sc["unsupplied_junctions"]))

    closed = [l["id"] for l in model.links() if l["status"] == "Closed"]
    if closed:
        issues.append(_issue("info", "closed_links", "Links that start Closed.", "link", closed))
    if char["parallel_link_excess"]:
        _, g = model.graphs()
        par = [lid for _, _, d in g.edges(data=True) if d["physical_link_count"] > 1 for lid in d["parallel_link_ids"]]
        issues.append(_issue("info", "parallel_links", "Parallel links between the same two nodes.", "link", par))

    negative = [j for j in wn.junction_name_list if model.total_base_demand(j) < 0]
    if negative:
        issues.append(_issue("info", "negative_demand", "Junctions with negative base demand (inflows).", "node", negative))
    zero_dem = [j for j in wn.junction_name_list if model.total_base_demand(j) == 0]
    if wn.junction_name_list and len(zero_dem) == len(wn.junction_name_list):
        issues.append(_issue("warning", "no_demand", "No junction has a base demand."))

    elevations = [getattr(wn.get_node(n), "elevation", 0) or 0 for n in wn.junction_name_list]
    if elevations and all(e == 0 for e in elevations):
        issues.append(_issue("warning", "zero_elevation", "All junction elevations are 0; pressures equal heads."))

    heads = [wn.get_node(r).base_head for r in wn.reservoir_name_list]
    heads += [wn.get_node(t).elevation + wn.get_node(t).max_level for t in wn.tank_name_list]
    if heads:
        top = max(heads)
        above = [j for j in wn.junction_name_list if (getattr(wn.get_node(j), "elevation", 0) or 0) > top]
        if above:
            sev = "warning" if wn.pump_name_list else "error"
            issues.append(_issue(sev, "above_source_head",
                                 f"Junctions above the highest source head ({top:.1f} m)" +
                                 ("; they need pumping." if wn.pump_name_list else "; they cannot be supplied by gravity."),
                                 "node", above))
    for t in wn.tank_name_list:
        tank = wn.get_node(t)
        if not (tank.min_level <= tank.init_level <= tank.max_level):
            issues.append(_issue("error", "tank_levels", f"Tank {t}: initial level outside min/max.", "node", [t]))

    if model.layout_generated:
        issues.append(_issue("info", "no_coordinates", "The file has no usable coordinates; a layout was generated for display."))

    issues.sort(key=lambda i: SEVERITY_ORDER[i["severity"]])
    counts = {s: sum(1 for i in issues if i["severity"] == s) for s in SEVERITY_ORDER}
    return {"success": True, "counts": counts, "issues": issues}
