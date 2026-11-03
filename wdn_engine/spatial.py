"""Spatial and flow-path questions: nodes within a distance, upstream/downstream tracing."""

from __future__ import annotations

import math
import warnings
from typing import Any

import networkx as nx
import numpy as np

from .session import Session

FLOW_EPS_LPS = 1e-3


def nodes_within(session: Session, center: str, distance_m: float, method: str = "network") -> dict[str, Any]:
    model = session.model
    c = model.resolve_node(center)
    if c is None:
        lid = model.resolve_link(center)
        if lid is None:
            return {"success": False, "error": f"Unknown element '{center}'."}
        c = model.wn.get_link(lid).start_node_name
    distance_m = float(distance_m)
    note = ""
    if method == "straight":
        cx, cy = model.layout[c]
        dist = {n: math.hypot(x - cx, y - cy) for n, (x, y) in model.layout.items()}
        dist = {n: d for n, d in dist.items() if d <= distance_m}
        note = ("Straight-line distance from the file's coordinates" +
                (" — WARNING: this network has generated (not real) coordinates, so distances are meaningless."
                 if model.layout_generated else " (assumes coordinates are in metres)."))
    else:
        _, g = model.graphs(include_closed=False)
        dist = nx.single_source_dijkstra_path_length(g, c, cutoff=distance_m, weight="length_m")
        note = "Distance measured along open pipes (pump/valve links count as 1 m)."
    rows = sorted(({"id": n, "distance_m": round(d, 2), "type": model.wn.get_node(n).node_type}
                   for n, d in dist.items() if n != c), key=lambda r: r["distance_m"])
    demand = sum(model.total_base_demand(r["id"]) for r in rows if r["type"] == "Junction") * 1000
    return {"success": True, "center": c, "distance_m": distance_m, "method": method, "note": note,
            "count": len(rows), "junctions": sum(r["type"] == "Junction" for r in rows),
            "base_demand_lps": round(demand, 3), "nodes": rows[:300], "node_ids": [r["id"] for r in rows]}


def trace_flow(session: Session, element: str, direction: str = "downstream", time_index: int | None = None) -> dict[str, Any]:
    """Follow simulated flow directions from an element.

    Uses the mean flow over the run (or a single time step if ``time_index`` is given); links whose
    flow is ~0 or reverses with near-zero mean are not followed.
    """
    model, sim = session.model, session.sim
    if sim is None:
        return {"success": False, "error": "Run a simulation first (flow directions come from the simulation)."}
    direction = "upstream" if str(direction).lower().startswith("up") else "downstream"
    nid, lid = model.resolve_node(element), model.resolve_link(element)
    if not nid and not lid:
        return {"success": False, "error": f"Unknown element '{element}'."}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        flow = sim.link_data["flow"]
        q = flow[time_index] if time_index is not None else np.nanmean(flow, axis=0)
    g = nx.DiGraph()
    reversing = 0
    for i, link_id in enumerate(sim.link_ids):
        v = q[i]
        if not np.isfinite(v) or abs(v) < FLOW_EPS_LPS:
            continue
        link = model.wn.get_link(link_id)
        a, b = (link.start_node_name, link.end_node_name) if v > 0 else (link.end_node_name, link.start_node_name)
        g.add_edge(a, b, link=link_id)
        if time_index is None and len(flow) > 1 and (np.sign(flow[:, i]) * np.sign(v) < 0).any():
            reversing += 1
    if lid:
        link = model.wn.get_link(lid)
        ends = [e for e in (link.start_node_name, link.end_node_name)]
        if g.has_edge(*ends):
            start = ends[1] if direction == "downstream" else ends[0]
        elif g.has_edge(ends[1], ends[0]):
            start = ends[0] if direction == "downstream" else ends[1]
        else:
            return {"success": False, "error": f"Link '{lid}' carries (almost) no flow, so it has no direction."}
    else:
        start = nid
    if start not in g:
        return {"success": False, "error": f"No flow passes through '{start}'."}
    reach = nx.descendants(g, start) if direction == "downstream" else nx.ancestors(g, start)
    reach.add(start)
    sub = g.subgraph(reach)
    links = sorted({d["link"] for _, _, d in sub.edges(data=True)} | ({lid} if lid else set()))
    junctions = [n for n in reach if model.wn.get_node(n).node_type == "Junction"]
    sources = [n for n in reach if n in model.sources]
    demand = sum(model.total_base_demand(n) for n in junctions) * 1000
    return {
        "success": True, "element": nid or lid, "direction": direction,
        "basis": f"flow at {sim.times_h[time_index]:.2f} h" if time_index is not None else "mean flow over the run",
        "nodes": len(reach), "junctions": len(junctions), "links": len(links),
        "base_demand_lps": round(demand, 3), "sources_reached": sources,
        "node_ids": sorted(reach), "link_ids": links,
        "network_links_with_reversing_flow": reversing,
        "note": ("Some pipes reverse direction during the run; the trace follows the average direction."
                 if reversing and time_index is None else ""),
        "sim_id": sim.id,
    }
