"""Graph-theoretic analysis of a water network (deterministic, unit-aware)."""

from __future__ import annotations

import math
import random
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

import networkx as nx
import pandas as pd

from .model import NetworkModel, fnum

APPROX_NODE_THRESHOLD = 3000   # above this, all-pairs metrics are sampled
SAMPLE_SOURCES = 400


# ------------------------------------------------------------------ pipe data
FIELDS = {
    "diameter_mm": "diameter_mm",
    "length_m": "length_m",
    "roughness": "roughness",
}
OPS = {
    "<": lambda a, b, t: a < b - t,
    "<=": lambda a, b, t: a <= b + t,
    ">": lambda a, b, t: a > b + t,
    ">=": lambda a, b, t: a >= b - t,
    "=": lambda a, b, t: abs(a - b) <= t,
    "!=": lambda a, b, t: abs(a - b) > t,
}


@dataclass(frozen=True)
class PipeCondition:
    field: str      # diameter_mm | length_m | roughness
    op: str         # < <= > >= = !=
    value: float

    def describe(self) -> str:
        unit = {"diameter_mm": " mm", "length_m": " m", "roughness": ""}[self.field]
        return f"{self.field.split('_')[0]} {self.op} {self.value:g}{unit}"


def _tolerance(field: str, value: float) -> float:
    # Diameters converted from inches (e.g. 101.6 mm) should still match "100 mm"
    # only for "="; comparisons use a tiny numeric tolerance.
    if field == "diameter_mm":
        return 0.5
    return max(1e-6, abs(value) * 1e-6)


def filter_pipes(model: NetworkModel, conditions: list[PipeCondition]) -> dict[str, Any]:
    bad = [c for c in conditions if c.field not in FIELDS or c.op not in OPS]
    if not conditions or bad:
        return {"success": False, "error": "No valid pipe condition (fields: diameter_mm, length_m, roughness)."}
    rows = []
    for p in model.pipes():
        ok = True
        for c in conditions:
            val = p.get(c.field)
            if val is None or not OPS[c.op](val, c.value, _tolerance(c.field, c.value) if c.op in {"=", "!="} else 1e-9):
                ok = False
                break
        if ok:
            rows.append({k: p[k] for k in ("id", "from", "to", "length_m", "diameter_mm", "roughness", "status")})
    total_len = sum(r["length_m"] or 0 for r in rows)
    return {
        "success": True,
        "conditions": [c.describe() for c in conditions],
        "number_of_matching_pipes": len(rows),
        "share_of_pipes": round(len(rows) / max(1, len(model.pipes())), 4),
        "matching_length_km": round(total_len / 1000.0, 3),
        "pipes": rows,
    }


def pipe_statistics(model: NetworkModel) -> dict[str, Any]:
    pipes = model.pipes()
    if not pipes:
        return {"success": False, "error": "This network contains no pipes."}
    frame = pd.DataFrame(pipes)

    def describe(col: pd.Series) -> dict[str, float | None]:
        col = col.dropna()
        if col.empty:
            return {}
        return {k: fnum(v, 3) for k, v in {
            "min": col.min(), "p10": col.quantile(0.1), "median": col.median(),
            "mean": col.mean(), "p90": col.quantile(0.9), "max": col.max()}.items()}

    by_diameter = (frame.groupby(frame.diameter_mm.round(1))
                   .agg(count=("id", "size"), length_m=("length_m", "sum"))
                   .reset_index().sort_values("diameter_mm"))
    return {
        "success": True,
        "number_of_pipes": len(frame),
        "total_length_km": round(float(frame.length_m.sum()) / 1000.0, 3),
        "length_m": describe(frame.length_m),
        "diameter_mm": describe(frame.diameter_mm),
        "roughness": describe(frame.roughness),
        "roughness_meaning": model.summary()["roughness_meaning"],
        "closed_pipes": int((frame.status == "Closed").sum()),
        "pipes_with_check_valve": int(frame.check_valve.sum()),
        "length_by_diameter": [
            {"diameter_mm": float(r.diameter_mm), "count": int(r.count), "length_km": round(float(r.length_m) / 1000.0, 3)}
            for r in by_diameter.itertuples()
        ],
    }


# -------------------------------------------------------------- element info
def node_info(model: NetworkModel, node_id: str) -> dict[str, Any]:
    nid = model.resolve_node(node_id)
    if nid is None:
        return {"success": False, "error": f"Node '{node_id}' does not exist in the loaded network."}
    multi, simple = model.graphs()
    rec = dict(model.node_record(nid))
    rec.update({
        "success": True,
        "physical_degree": multi.degree(nid),
        "topological_degree": simple.degree(nid),
        "connected_links": [
            {"id": lid, "type": model.wn.get_link(lid).link_type,
             "other_node": (lambda l: l.end_node_name if l.start_node_name == nid else l.start_node_name)(model.wn.get_link(lid))}
            for lid in model.wn.get_links_for_node(nid)
        ],
    })
    return rec


def link_info(model: NetworkModel, link_id: str) -> dict[str, Any]:
    lid = model.resolve_link(link_id)
    if lid is None:
        return {"success": False, "error": f"Link '{link_id}' does not exist in the loaded network."}
    rec = dict(model.link_record(lid))
    rec.pop("vertices", None)
    rec["success"] = True
    _, simple = model.graphs()
    u, v = rec["from"], rec["to"]
    rec["is_bridge"] = bool(simple.has_edge(u, v) and simple[u][v]["physical_link_count"] == 1
                            and any(set(b) == {u, v} for b in _bridges(model)))
    return rec


def _bridges(model: NetworkModel) -> list[tuple[str, str]]:
    key = ("bridges",)
    if key not in model._cache:
        _, g = model.graphs()
        model._cache[key] = [(u, v) for u, v in nx.bridges(g) if g[u][v]["physical_link_count"] == 1]
    return model._cache[key]


# ------------------------------------------------------------- connectivity
def source_connectivity(model: NetworkModel) -> dict[str, Any]:
    """Junctions that cannot reach any reservoir/tank through initially open links."""
    _, g = model.graphs(include_closed=False)
    reachable: set[str] = set()
    for s in model.sources:
        if s in g and s not in reachable:
            reachable |= nx.node_connected_component(g, s)
    unsupplied = sorted(n for n in model.wn.junction_name_list if n not in reachable)
    demand = sum(model.total_base_demand(n) for n in unsupplied) * 1000.0
    return {
        "success": True,
        "sources": model.sources,
        "unsupplied_junctions": unsupplied,
        "number_unsupplied": len(unsupplied),
        "unsupplied_base_demand_lps": round(demand, 3),
        "note": "Uses initial link status (closed links removed); controls during a simulation can change this.",
    }


def characterization(model: NetworkModel, top_n: int = 15) -> dict[str, Any]:
    key = ("characterization",)
    if key in model._cache:
        result = dict(model._cache[key])
    else:
        multi, g = model.graphs()
        n, m, pm = g.number_of_nodes(), g.number_of_edges(), multi.number_of_edges()
        comps = sorted(nx.connected_components(g), key=len, reverse=True)
        deg = dict(g.degree())
        sources = set(model.sources)
        articulation = sorted(nx.articulation_points(g), key=lambda x: (-deg[x], str(x)))
        bridges = []
        for u, v in _bridges(model):
            e = g[u][v]
            bridges.append({"link_id": e["link_id"], "from": u, "to": v, "length_m": round(e["length_m"], 2)})
        bridges.sort(key=lambda b: -b["length_m"])
        blocks = sorted(nx.biconnected_components(g), key=len, reverse=True)
        physical_loops = pm - n + len(comps)
        try:
            assort = fnum(nx.degree_assortativity_coefficient(g), 4)
        except Exception:
            assort = None
        result = {
            "success": True,
            "nodes": n,
            "physical_links": pm,
            "unique_connections": m,
            "parallel_link_excess": pm - m,
            "connected": len(comps) <= 1,
            "number_of_components": len(comps),
            "component_sizes": [len(c) for c in comps[:20]],
            "largest_component_coverage": round(len(comps[0]) / n, 4) if n else 0.0,
            "density": fnum(nx.density(g), 6),
            "average_degree": round(2 * m / n, 4) if n else 0,
            "degree_distribution": dict(sorted(Counter(deg.values()).items())),
            "degree_assortativity": assort,
            "average_clustering": fnum(nx.average_clustering(g), 4),
            "cycle_rank_unique_connections": m - n + len(comps),
            "loops_including_parallel_links": physical_loops,
            "meshedness_coefficient": fnum((m - n + len(comps)) / (2 * n - 5), 4) if n >= 3 else None,
            "link_density_per_node": round(pm / n, 4) if n else 0,
            "isolated_nodes": sorted(nx.isolates(g), key=str),
            "dead_end_junctions": sorted((x for x, d in deg.items() if d == 1 and x not in sources), key=str),
            "articulation_points": articulation,
            "bridges": bridges,
            "number_of_biconnected_components": len(blocks),
            "largest_biconnected_component": len(blocks[0]) if blocks else 0,
            "branched_fraction": round(len(bridges) / pm, 4) if pm else 0.0,
        }
        result["source_connectivity"] = source_connectivity(model)
        model._cache[key] = result
        result = dict(result)
    result["number_of_dead_ends"] = len(result["dead_end_junctions"])
    result["number_of_articulation_points"] = len(result["articulation_points"])
    result["number_of_bridges"] = len(result["bridges"])
    return result


# --------------------------------------------------------------- distances
def distance_statistics(model: NetworkModel, weighted: bool = False) -> dict[str, Any]:
    """Diameter & average shortest path per component without all-pairs storage."""
    key = ("distances", weighted)
    if key in model._cache:
        return model._cache[key]
    _, g = model.graphs()
    started = time.perf_counter()
    weight = "length_m" if weighted else None
    rng = random.Random(42)
    comps_out = []
    approximate = False
    for comp in sorted(nx.connected_components(g), key=len, reverse=True):
        h = g.subgraph(comp)
        nodes = list(h)
        if len(nodes) <= 1:
            comps_out.append({"nodes": len(nodes), "diameter": 0.0, "average_shortest_path": 0.0})
            continue
        sample = nodes
        if len(nodes) > APPROX_NODE_THRESHOLD:
            sample = rng.sample(nodes, SAMPLE_SOURCES)
            approximate = True
        best, total, count = 0.0, 0.0, 0
        for s in sample:
            dist = nx.single_source_dijkstra_path_length(h, s, weight=weight) if weight else nx.single_source_shortest_path_length(h, s)
            vals = [d for t, d in dist.items() if t != s]
            if vals:
                best = max(best, max(vals))
                total += sum(vals)
                count += len(vals)
        comps_out.append({"nodes": len(nodes), "diameter": round(float(best), 3),
                          "average_shortest_path": round(total / count, 3) if count else 0.0})
    result = {
        "success": True,
        "metric": "distance_statistics",
        "weighted_by": "length_m" if weighted else "hop_count",
        "unit": "m" if weighted else "hops",
        "approximate": approximate,
        "note": "Sampled from 400 sources (diameter is a lower bound)." if approximate else "Exact.",
        "components": comps_out[:10],
        "largest_component_diameter": comps_out[0]["diameter"] if comps_out else None,
        "largest_component_average_shortest_path": comps_out[0]["average_shortest_path"] if comps_out else None,
        "calculation_time_s": round(time.perf_counter() - started, 3),
    }
    model._cache[key] = result
    return result


# ------------------------------------------------------------------ metrics
NODE_METRICS = {
    "degree": "Number of distinct neighbouring nodes.",
    "betweenness_centrality": "Share of shortest routes passing through the node (transfer importance).",
    "closeness_centrality": "Inverse mean distance to all other nodes in its component.",
    "eigenvector_centrality": "Connection to other well-connected nodes (weighted: shorter pipes = stronger ties).",
    "clustering": "Fraction of a node's neighbour pairs that are themselves connected (local triangulation).",
    "articulation": "1 if removing the node splits the network, else 0.",
}
LINK_METRICS = {
    "edge_betweenness": "Share of shortest routes using the link (flow-path importance proxy).",
    "bridge": "1 if the link is a single connection whose removal splits the network.",
}


def node_metric(model: NetworkModel, metric: str, weighted: bool = False) -> dict[str, Any]:
    """Return a value for *every* node (for colouring) plus metadata."""
    metric = metric.lower().strip().replace(" ", "_")
    aliases = {"betweenness": "betweenness_centrality", "closeness": "closeness_centrality",
               "eigenvector": "eigenvector_centrality", "centrality": "betweenness_centrality",
               "degree_centrality": "degree", "articulation_points": "articulation"}
    metric = aliases.get(metric, metric)
    if metric not in NODE_METRICS:
        return {"success": False, "error": f"Unknown node metric '{metric}'. Options: {', '.join(NODE_METRICS)}."}
    key = ("node_metric", metric, weighted)
    if key in model._cache:
        return model._cache[key]
    _, g = model.graphs()
    n = g.number_of_nodes()
    started = time.perf_counter()
    approximate = False
    if metric == "degree":
        values = dict(g.degree())
    elif metric == "betweenness_centrality":
        k = None
        if n > APPROX_NODE_THRESHOLD:
            k, approximate = SAMPLE_SOURCES, True
        values = nx.betweenness_centrality(g, k=k, seed=42, weight="length_m" if weighted else None)
    elif metric == "closeness_centrality":
        if n > APPROX_NODE_THRESHOLD:
            return {"success": False, "error": "Closeness is too slow above 3000 nodes; use betweenness (sampled) instead."}
        values = nx.closeness_centrality(g, distance="length_m" if weighted else None)
    elif metric == "eigenvector_centrality":
        try:
            values = nx.eigenvector_centrality_numpy(g, weight="strength" if weighted else None)
        except Exception as error:
            return {"success": False, "error": f"Eigenvector centrality failed: {error}"}
    elif metric == "clustering":
        values = nx.clustering(g, weight="strength" if weighted else None)
    else:  # articulation
        arts = set(characterization(model)["articulation_points"])
        values = {x: (1 if x in arts else 0) for x in g.nodes}
    values = {k: float(v) for k, v in values.items()}
    ranking = sorted(values.items(), key=lambda kv: kv[1], reverse=True)
    result = {
        "success": True,
        "metric": metric,
        "target": "node",
        "description": NODE_METRICS[metric],
        "weighted": weighted,
        "weight_meaning": ("length as distance" if metric in {"betweenness_centrality", "closeness_centrality"}
                           else "1/length as strength") if weighted else "unweighted (hops)",
        "approximate": approximate,
        "calculation_time_s": round(time.perf_counter() - started, 3),
        "values": values,
        "top": [{"id": k, "value": round(v, 6), "degree": g.degree(k)} for k, v in ranking[:25]],
        "mean": round(sum(values.values()) / len(values), 6) if values else None,
    }
    model._cache[key] = result
    return result


def link_metric(model: NetworkModel, metric: str, weighted: bool = False) -> dict[str, Any]:
    metric = metric.lower().strip().replace(" ", "_")
    metric = {"bridges": "bridge", "link_betweenness": "edge_betweenness"}.get(metric, metric)
    if metric not in LINK_METRICS:
        return {"success": False, "error": f"Unknown link metric '{metric}'. Options: {', '.join(LINK_METRICS)}."}
    key = ("link_metric", metric, weighted)
    if key in model._cache:
        return model._cache[key]
    multi, g = model.graphs()
    started = time.perf_counter()
    approximate = False
    if metric == "bridge":
        bset = {frozenset(b) for b in _bridges(model)}
        values = {d["link_id"]: (1.0 if frozenset((u, v)) in bset else 0.0) for u, v, d in multi.edges(data=True)}
    else:
        k = None
        if g.number_of_nodes() > APPROX_NODE_THRESHOLD:
            k, approximate = SAMPLE_SOURCES, True
        eb = nx.edge_betweenness_centrality(g, k=k, seed=42, weight="length_m" if weighted else None)
        values = {}
        for (u, v), val in eb.items():
            ids = g[u][v]["parallel_link_ids"]
            for lid in ids:  # parallel links share the connection's load
                values[lid] = float(val) / len(ids)
        for u, v, d in multi.edges(data=True):
            values.setdefault(d["link_id"], 0.0)
    ranking = sorted(values.items(), key=lambda kv: kv[1], reverse=True)
    result = {
        "success": True, "metric": metric, "target": "link", "description": LINK_METRICS[metric],
        "weighted": weighted, "approximate": approximate,
        "calculation_time_s": round(time.perf_counter() - started, 3),
        "values": values,
        "top": [{"id": k, "value": round(v, 6)} for k, v in ranking[:25]],
    }
    model._cache[key] = result
    return result


# ------------------------------------------------------------------- paths
def shortest_path(model: NetworkModel, source: str, target: str, weighted: bool = True) -> dict[str, Any]:
    s, t = model.resolve_node(source), model.resolve_node(target)
    missing = [x for x, r in ((source, s), (target, t)) if r is None]
    if missing:
        return {"success": False, "error": f"Unknown node ID(s): {', '.join(map(str, missing))}."}
    _, g = model.graphs(include_closed=False)
    if not nx.has_path(g, s, t):
        return {"success": False, "error": f"No open path exists between '{s}' and '{t}'."}
    path = nx.shortest_path(g, s, t, weight="length_m" if weighted else None)
    links, total = [], 0.0
    for u, v in zip(path[:-1], path[1:]):
        e = g[u][v]
        total += e["length_m"]
        links.append(e["link_id"])
    return {"success": True, "source": s, "target": t, "weighted_by": "length_m" if weighted else "hop_count",
            "path_nodes": path, "path_links": links, "total_length_m": round(total, 2), "number_of_links": len(links)}


def minimum_spanning_backbone(model: NetworkModel) -> dict[str, Any]:
    _, g = model.graphs()
    forest = nx.minimum_spanning_tree(g, weight="length_m")
    links = [d["link_id"] for _, _, d in forest.edges(data=True)]
    total = sum(d["length_m"] for _, _, d in forest.edges(data=True))
    all_len = sum(d["length_m"] for _, _, d in model.graphs()[0].edges(data=True))
    return {"success": True, "links": links, "number_of_links": len(links), "total_length_km": round(total / 1000, 3),
            "share_of_network_length": round(total / all_len, 4) if all_len else None,
            "note": "Loop-free, length-minimising backbone: a redundancy baseline, not a design recommendation."}
