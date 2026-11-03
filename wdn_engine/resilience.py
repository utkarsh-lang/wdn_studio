"""Engineering analyses: criticality (N-1), valve segments, fire flow, demand-growth capacity.

All of them need many hydraulic solves, so they use ``SnapshotSolver``: the network is frozen at one
moment of a baseline extended-period run (default: the hour of peak total demand) — demands, reservoir
heads, tank levels and pump/valve statuses are fixed to their values at that moment — and EPANET's
toolkit then solves single-period variants in ~2 ms each.  This is the standard "peak-hour snapshot"
approach; it ignores how tank levels would evolve after a failure (noted in every result).
"""

from __future__ import annotations

import copy
import os
import tempfile
import time
import warnings
from typing import Any, Callable

import networkx as nx
import numpy as np
import wntr
from wntr.epanet import toolkit

from .hydraulics import _EPANET_LOCK, SimSettings
from .model import NetworkModel
from .session import Session

EN_BASEDEMAND, EN_DEMAND, EN_PRESSURE = 1, 9, 11
EN_INITSTATUS = 4  # EPANET link parameter code for initial status
Progress = Callable[[float, str], None]


def _noop(_: float, __: str) -> None:
    pass


def baseline_for(session: Session) -> Any:
    """An extended-period run to take the snapshot from (the shown run, or a new 24 h run)."""
    sim = session.sim
    if sim is None or len(sim.times_h) < 2 or sim.settings.closed_links or sim.settings.demand_multiplier != 1 \
            or getattr(sim.settings, "leaks", None):
        file_h = session.model.wn.options.time.duration / 3600.0
        dur = min(file_h, 168.0) if file_h >= 24 else 24.0
        # keep the user's shown run as active afterwards
        active = session.active_sim_id
        sim = session.simulate(SimSettings(duration_h=dur, label="baseline for snapshot analyses"))
        if active:
            session.active_sim_id = active
    return sim


def peak_index(model: NetworkModel, sim) -> int:
    j = [sim.node_index[n] for n in model.wn.junction_name_list]
    return int(np.nanargmax(np.nansum(sim.node_data["demand"][:, j], axis=1)))


class SnapshotSolver:
    """Single-period EPANET project frozen at one time step of a baseline run."""

    def __init__(self, model: NetworkModel, sim, t_index: int | None = None, demand_model: str = "PDD",
                 required_pressure: float = 20.0, minimum_pressure: float = 0.0):
        self.model = model
        self.t_index = peak_index(model, sim) if t_index is None else int(t_index)
        self.t_h = float(sim.times_h[self.t_index])
        wn = copy.deepcopy(model.wn)
        t_s = int(round(self.t_h * 3600))
        # Freeze demands at t (all categories, patterns and the global multiplier).
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            exp = wntr.metrics.expected_demand(wn, start_time=t_s, end_time=t_s)
        # EPANET applies the default pattern (ID "1" unless set) to demands without a pattern, so use an
        # explicit flat pattern; otherwise frozen demands would be multiplied a second time.
        flat = "WDN_FLAT_SNAPSHOT"
        wn.add_pattern(flat, [1.0])
        for name in wn.junction_name_list:
            j = wn.get_node(name)
            q = float(exp[name].iloc[0]) if name in exp else 0.0
            j.demand_timeseries_list.clear()
            j.add_demand(q, flat)
        wn.options.hydraulic.demand_multiplier = 1.0
        wn.options.hydraulic.pattern = flat
        for name in wn.reservoir_name_list:  # freeze head patterns
            r = wn.get_node(name)
            r.base_head = float(sim.node_data["head"][self.t_index, sim.node_index[name]])
            r.head_pattern_name = None
        for name in wn.tank_name_list:       # tank levels at t
            tank = wn.get_node(name)
            lvl = float(sim.node_data["head"][self.t_index, sim.node_index[name]] - tank.elevation)
            tank.init_level = float(np.clip(lvl, tank.min_level, tank.max_level))
        for name, _ in list(wn.controls()):  # statuses at t replace controls/rules
            wn.remove_control(name)
        for lid in wn.pump_name_list + wn.valve_name_list:
            st = sim.link_data["status"][self.t_index, sim.link_index[lid]]
            link = wn.get_link(lid)
            if np.isfinite(st):
                link.initial_status = wntr.network.LinkStatus.Closed if st == 0 else (
                    wntr.network.LinkStatus.Active if (st == 2 and link.link_type == "Valve") else wntr.network.LinkStatus.Open)
        wn.options.time.duration = 0
        wn.options.time.pattern_start = 0
        wn.options.quality.parameter = "NONE"
        self.demand_model = "PDD" if demand_model == "PDD" else "DDA"
        wn.options.hydraulic.demand_model = self.demand_model
        wn.options.hydraulic.required_pressure = float(required_pressure)
        wn.options.hydraulic.minimum_pressure = float(minimum_pressure)
        self.required_pressure = float(required_pressure)
        self._tmp = tempfile.TemporaryDirectory()
        inp = os.path.join(self._tmp.name, "snap.inp")
        wntr.network.write_inpfile(wn, inp, units="LPS")
        self.en = toolkit.ENepanet()
        self.en.ENopen(inp, os.path.join(self._tmp.name, "snap.rpt"), "")
        self.en.ENopenH()
        nn, nl = self.en.ENgetcount(0), self.en.ENgetcount(2)
        self.node_ids = [self.en.ENgetnodeid(i) for i in range(1, nn + 1)]
        self.node_idx = {n: i + 1 for i, n in enumerate(self.node_ids)}
        self.link_idx = {self.en.ENgetlinkid(i) if hasattr(self.en, "ENgetlinkid") else None: i for i in range(1, nl + 1)}
        if None in self.link_idx:
            self.link_idx = {lid: self.en.ENgetlinkindex(lid) for lid in wn.link_name_list}
        self.junctions = [self.node_idx[j] for j in wn.junction_name_list]
        self.junction_ids = list(wn.junction_name_list)
        self.base_demand = np.array([self.en.ENgetnodevalue(i, EN_BASEDEMAND) for i in self.junctions])
        self.initial_status = {lid: self.en.ENgetlinkvalue(i, EN_INITSTATUS) for lid, i in self.link_idx.items()}
        self.base = self.solve()

    def close(self) -> None:
        try:
            self.en.ENcloseH()
            self.en.ENclose()
        finally:
            self._tmp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def solve(self, closed: list[str] | tuple[str, ...] = (), extra_demand: dict[str, float] | None = None,
              demand_factor: float = 1.0) -> dict[str, np.ndarray] | None:
        """Pressures (m) and delivered/required demand (L/s) at junctions; None if EPANET fails."""
        en = self.en
        changed = []
        try:
            for lid in closed:
                i = self.link_idx[lid]
                en.ENsetlinkvalue(i, EN_INITSTATUS, 0)
                changed.append(("link", i, self.initial_status[lid]))
            required = self.base_demand * demand_factor
            if demand_factor != 1.0:
                for k, i in enumerate(self.junctions):
                    en.ENsetnodevalue(i, EN_BASEDEMAND, float(required[k]))
                changed.append(("all_demands", None, None))
            if extra_demand:
                required = required.copy()
                for nid, q in extra_demand.items():
                    k = self.junction_ids.index(nid)
                    i = self.junctions[k]
                    required[k] += q
                    en.ENsetnodevalue(i, EN_BASEDEMAND, float(required[k]))
                    changed.append(("node", i, float(self.base_demand[k] * demand_factor)))
            en.ENinitH(0)
            en.ENrunH()
            p = np.array([en.ENgetnodevalue(i, EN_PRESSURE) for i in self.junctions])
            d = np.array([en.ENgetnodevalue(i, EN_DEMAND) for i in self.junctions])
            return {"pressure": p, "delivered": d, "required": required}
        except Exception:
            return None
        finally:
            for kind, i, value in reversed(changed):
                if kind == "link":
                    en.ENsetlinkvalue(i, EN_INITSTATUS, value)
                elif kind == "node":
                    en.ENsetnodevalue(i, EN_BASEDEMAND, value)
                else:
                    for k, j in enumerate(self.junctions):
                        en.ENsetnodevalue(j, EN_BASEDEMAND, float(self.base_demand[k]))


def _snapshot_note(solver: SnapshotSolver, session: Session) -> str:
    from .temporal import clock
    return (f"Peak-hour snapshot at {clock(session, solver.t_h)} (t = {solver.t_h:.2f} h): demands, reservoir heads, tank levels "
            "and pump/valve statuses are frozen at that moment; tank levels are not allowed to evolve after a failure.")


# ------------------------------------------------------------------- 2.1 N-1 criticality
def pipe_criticality(session: Session, required_pressure: float | None = None, include: str = "pipes",
                     time_h: float | None = None, progress: Progress = _noop) -> dict[str, Any]:
    model = session.model
    req = float(required_pressure if required_pressure is not None else (session.criteria.min_pressure_m or 20.0))
    progress(0.02, "Preparing baseline run…")
    sim = baseline_for(session)
    t_index = None if time_h is None else int(np.argmin(np.abs(sim.times_h - float(time_h))))
    links = list(model.wn.pipe_name_list) + (list(model.wn.pump_name_list) + list(model.wn.valve_name_list)
                                             if include == "all" else [])
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    with _EPANET_LOCK, SnapshotSolver(model, sim, t_index, "PDD", req) as solver:
        b = solver.base
        if b is None:
            return {"success": False, "error": "EPANET could not solve the baseline snapshot."}
        total = float(b["required"].sum())
        base_short = float((b["required"] - b["delivered"]).clip(min=0).sum())
        base_low = b["pressure"] < req
        failed = 0
        for k, lid in enumerate(links):
            if k % 25 == 0:
                progress(0.05 + 0.9 * k / max(1, len(links)), f"Closing {lid} ({k + 1}/{len(links)})")
            if model.wn.get_link(lid).initial_status.name == "Closed":
                continue
            r = solver.solve(closed=[lid])
            if r is None:
                failed += 1
                rows.append({"id": lid, "solved": False})
                continue
            short = float((r["required"] - r["delivered"]).clip(min=0).sum()) - base_short
            drop = b["pressure"] - r["pressure"]
            new_low = (r["pressure"] < req) & ~base_low
            rows.append({
                "id": lid, "solved": True,
                "demand_shortfall_lps": round(max(short, 0.0), 4),
                "shortfall_pct": round(100 * max(short, 0.0) / total, 3) if total else 0.0,
                "junctions_newly_below_req": int(new_low.sum()),
                "junctions_drop_over_1m": int((drop > 1.0).sum()),
                "max_pressure_drop_m": round(float(np.nanmax(drop)), 3),
                "worst_node": solver.junction_ids[int(np.nanargmax(drop))],
            })
        note = _snapshot_note(solver, session)
        t_h = solver.t_h
    progress(0.98, "Ranking…")
    ok = [r for r in rows if r["solved"]]
    ok.sort(key=lambda r: (r["demand_shortfall_lps"], r["junctions_newly_below_req"], r["max_pressure_drop_m"]), reverse=True)
    from .topology import _bridges
    bridges = {frozenset(x) for x in _bridges(model)}
    for r in ok:
        link = model.wn.get_link(r["id"])
        r["is_bridge"] = frozenset((link.start_node_name, link.end_node_name)) in bridges
        r["diameter_mm"] = round(link.diameter * 1000, 1) if link.link_type == "Pipe" else None
    for rank, r in enumerate(ok, 1):
        r["rank"] = rank
    no_impact = sum(1 for r in ok if r["demand_shortfall_lps"] < 1e-3 and r["max_pressure_drop_m"] < 0.5)
    session.layers["criticality"] = {"target": "link", "unit": "L/s", "title": "Demand shortfall if closed",
                                     "values": {r["id"]: r["demand_shortfall_lps"] for r in ok}}
    session.layers["criticality_drop"] = {"target": "link", "unit": "m", "title": "Max pressure drop if closed",
                                          "values": {r["id"]: r["max_pressure_drop_m"] for r in ok}}
    return {
        "success": True, "analysis": "pipe_criticality", "required_pressure_m": req, "snapshot_time_h": round(t_h, 3),
        "total_demand_lps": round(total, 3), "baseline_shortfall_lps": round(base_short, 4),
        "elements_tested": len(links), "unsolvable": failed, "no_significant_impact": no_impact,
        "critical_count": sum(1 for r in ok if r["demand_shortfall_lps"] >= 0.01 * total / 100 or r["junctions_newly_below_req"]),
        "top": ok[:25], "all": ok, "run_time_s": round(time.perf_counter() - started, 2), "note": note,
        "method": "Each link closed alone; pressure-driven demand (PDD) with full demand at "
                  f"{req:g} m; shortfall = demand that cannot be delivered.",
    }


# --------------------------------------------------------------------- 2.2 valve segments
def valve_segments(session: Session, placement: str = "strategic", n: int = 2, seed: int = 7,
                   hydraulic: bool = True, required_pressure: float | None = None,
                   progress: Progress = _noop) -> dict[str, Any]:
    """Isolation segments for an (assumed) valve layout and what isolating each one costs.

    placement 'strategic' = N valves at every junction (N-1 of its links get one; N=1..3);
    'random' = n valves placed at random pipe ends.
    """
    model = session.model
    wn = model.wn
    progress(0.05, "Placing valves…")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        layer = wntr.network.generate_valve_layer(wn, placement_type=placement, n=int(n), seed=int(seed))
        node_seg, link_seg, sizes = wntr.metrics.valve_segments(wn.to_graph(), layer)
    segs: dict[int, dict[str, list[str]]] = {}
    for nid, s in node_seg.items():
        segs.setdefault(int(s), {"nodes": [], "links": []})["nodes"].append(nid)
    for lid, s in link_seg.items():
        segs.setdefault(int(s), {"nodes": [], "links": []})["links"].append(lid)
    # Topological effect of isolating each segment: its own nodes plus anything cut off from every source.
    _, g = model.graphs(include_closed=False)
    sources = set(model.sources)
    rows = []
    progress(0.15, f"{len(segs)} segments: checking connectivity…")
    for sid, s in segs.items():
        h = g.copy()
        h.remove_nodes_from(s["nodes"])
        for lid in s["links"]:
            link = wn.get_link(lid)
            if h.has_edge(link.start_node_name, link.end_node_name):
                h.remove_edge(link.start_node_name, link.end_node_name)
        reach: set[str] = set()
        for src in sources:
            if src in h and src not in reach:
                reach |= nx.node_connected_component(h, src)
        cut = [n for n in wn.junction_name_list if n not in reach]
        downstream = [n for n in cut if n not in s["nodes"]]
        rows.append({"segment": sid, "nodes": len(s["nodes"]), "links": len(s["links"]),
                     "valves_to_close": int(((layer["link"].isin(s["links"])) | (layer["node"].isin(s["nodes"]))).sum()),
                     "junctions_without_water": len(cut), "extra_junctions_cut_off_downstream": len(downstream),
                     "base_demand_lost_lps": round(sum(model.total_base_demand(n) for n in cut) * 1000, 3),
                     "contains_source": bool(set(s["nodes"]) & sources),
                     "_links": s["links"], "_nodes": s["nodes"]})
    if hydraulic:
        req = float(required_pressure if required_pressure is not None else (session.criteria.min_pressure_m or 20.0))
        sim = baseline_for(session)
        with _EPANET_LOCK, SnapshotSolver(model, sim, None, "PDD", req) as solver:
            b = solver.base
            base_short = float((b["required"] - b["delivered"]).clip(min=0).sum())
            for k, r in enumerate(rows):
                if k % 20 == 0:
                    progress(0.3 + 0.65 * k / len(rows), f"Isolating segment {r['segment']} ({k + 1}/{len(rows)})")
                closable = [l for l in r["_links"] if l in solver.link_idx]
                res = solver.solve(closed=closable)
                if res is None:
                    r["demand_shortfall_lps"] = None
                    continue
                r["demand_shortfall_lps"] = round(max(float((res["required"] - res["delivered"]).clip(min=0).sum()) - base_short, 0), 4)
                r["junctions_below_req"] = int((res["pressure"] < req).sum())
            note = _snapshot_note(solver, session)
    else:
        note = "Topological only (no hydraulic solve)."
    key = "demand_shortfall_lps" if hydraulic else "base_demand_lost_lps"
    rows.sort(key=lambda r: (r.get(key) or 0, r["junctions_without_water"]), reverse=True)
    link_values, seg_of_link = {}, {}
    for r in rows:
        for lid in r["_links"]:
            link_values[lid] = r.get(key) or 0.0
            seg_of_link[lid] = r["segment"]
    session.layers["segments"] = {"target": "link", "unit": "L/s", "title": "Demand lost if the segment is isolated",
                                  "values": link_values}
    session.segment_rows = {r["segment"]: r for r in rows}
    sizes_n = [r["nodes"] for r in rows]
    public = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    return {"success": True, "analysis": "valve_segments", "placement": placement, "n": n, "valves": int(len(layer)),
            "segments": len(rows), "largest_segment_nodes": max(sizes_n) if sizes_n else 0,
            "mean_segment_nodes": round(float(np.mean(sizes_n)), 2) if sizes_n else 0,
            "segments_cutting_off_others": sum(1 for r in rows if r["extra_junctions_cut_off_downstream"]),
            "top": public[:20], "all": public, "note": note,
            "assumption": ("Benchmark files rarely contain isolation valves, so valves were ASSUMED: "
                           + (f"{n} valve(s) per junction ('N-1' strategic rule)" if placement == "strategic"
                              else f"{n} valves at random pipe ends (seed {seed})") + ".")}


# ----------------------------------------------------------------------------- 2.3 fire flow
def fire_flow(session: Session, node: str | None = None, residual_pressure: float = 14.0,
              required_flow_lps: float | None = None, max_flow_lps: float = 200.0, all_nodes: bool = False,
              system_min_pressure: float | None = None, progress: Progress = _noop) -> dict[str, Any]:
    """Available fire flow (L/s) at a hydrant node while keeping ``residual_pressure`` there
    (and optionally ``system_min_pressure`` everywhere else), demand-driven at the peak hour."""
    model = session.model
    targets: list[str]
    if all_nodes:
        targets = list(model.wn.junction_name_list)
    else:
        nid = model.resolve_node(node or "")
        if nid is None or model.wn.get_node(nid).node_type != "Junction":
            return {"success": False, "error": f"'{node}' is not a junction."}
        targets = [nid]
    sim = baseline_for(session)
    rows = []
    with _EPANET_LOCK, SnapshotSolver(model, sim, None, "DDA") as solver:
        idx = {j: k for k, j in enumerate(solver.junction_ids)}

        def ok(nid: str, q: float) -> tuple[bool, dict | None]:
            r = solver.solve(extra_demand={nid: q})
            if r is None:
                return False, None
            p = r["pressure"]
            good = p[idx[nid]] >= residual_pressure
            if system_min_pressure is not None:
                good = good and float(np.nanmin(np.delete(p, idx[nid]))) >= system_min_pressure
            return bool(good), r

        for k, nid in enumerate(targets):
            if k % 20 == 0:
                progress(0.05 + 0.9 * k / len(targets), f"Testing {nid} ({k + 1}/{len(targets)})")
            base_ok, _ = ok(nid, 0.0)
            if not base_ok:
                rows.append({"id": nid, "available_lps": 0.0, "limited_by": "residual already not met at normal demand"})
                continue
            lo, hi = 0.0, max_flow_lps
            hi_ok, _ = ok(nid, hi)
            if hi_ok:
                rows.append({"id": nid, "available_lps": hi, "limited_by": f"≥ search limit {max_flow_lps:g} L/s"})
                continue
            for _ in range(12):
                mid = (lo + hi) / 2
                good, _ = ok(nid, mid)
                lo, hi = (mid, hi) if good else (lo, mid)
            row = {"id": nid, "available_lps": round(lo, 2)}
            _, r = ok(nid, hi)
            if r is not None:
                p = r["pressure"]
                row["limited_by"] = ("system minimum pressure" if system_min_pressure is not None
                                     and float(np.nanmin(np.delete(p, idx[nid]))) < system_min_pressure else "residual pressure at the hydrant")
            rows.append(row)
        detail = None
        if not all_nodes and required_flow_lps is not None:
            _, r = ok(targets[0], float(required_flow_lps))
            if r is not None:
                p = r["pressure"]
                drop = solver.base["pressure"] - p
                detail = {"required_flow_lps": required_flow_lps, "hydrant_pressure_m": round(float(p[idx[targets[0]]]), 3),
                          "network_min_pressure_m": round(float(np.nanmin(p)), 3),
                          "min_at": solver.junction_ids[int(np.nanargmin(p))],
                          "junctions_drop_over_5m": int((drop > 5).sum()),
                          "meets_requirement": bool(p[idx[targets[0]]] >= residual_pressure)}
        note = _snapshot_note(solver, session)
    rows.sort(key=lambda r: r["available_lps"])
    if all_nodes:
        session.layers["fire_flow"] = {"target": "node", "unit": "L/s", "title": f"Available fire flow at {residual_pressure:g} m residual",
                                       "values": {r["id"]: r["available_lps"] for r in rows}}
    out = {"success": True, "analysis": "fire_flow", "residual_pressure_m": residual_pressure,
           "system_min_pressure_m": system_min_pressure, "nodes_tested": len(rows), "note": note,
           "method": "Demand-driven; extra hydrant demand added at the peak-hour snapshot; bisection to 0.1 L/s precision."}
    if all_nodes:
        vals = [r["available_lps"] for r in rows]
        out.update({"weakest": rows[:20], "median_lps": round(float(np.median(vals)), 2), "min_lps": rows[0]["available_lps"],
                    "below_required": (sum(1 for v in vals if v < required_flow_lps) if required_flow_lps else None),
                    "required_flow_lps": required_flow_lps, "all": rows})
    else:
        out.update(rows[0])
        out["check"] = detail
    return out


# -------------------------------------------------------------------- 2.6 demand growth
def demand_growth(session: Session, min_pressure: float | None = None, max_factor: float = 5.0,
                  verify_eps: bool = True, progress: Progress = _noop) -> dict[str, Any]:
    model = session.model
    pmin = float(min_pressure if min_pressure is not None else (session.criteria.min_pressure_m or 20.0))
    sim = baseline_for(session)
    with _EPANET_LOCK, SnapshotSolver(model, sim, None, "DDA") as solver:
        def min_p(f: float) -> tuple[float, str]:
            r = solver.solve(demand_factor=f)
            if r is None:
                return -1e9, ""
            k = int(np.nanargmin(r["pressure"]))
            return float(r["pressure"][k]), solver.junction_ids[k]

        p1, n1 = min_p(1.0)
        if p1 < pmin:
            return {"success": True, "analysis": "demand_growth", "min_pressure_m": pmin, "capacity_factor": None,
                    "message": f"Already below {pmin:g} m at today's peak demand (min {p1:.2f} m at {n1}).",
                    "current_min_pressure_m": round(p1, 3), "current_min_at": n1, "note": _snapshot_note(solver, session)}
        lo, hi = 1.0, max_factor
        if min_p(hi)[0] >= pmin:
            return {"success": True, "analysis": "demand_growth", "min_pressure_m": pmin, "capacity_factor": f">{max_factor:g}",
                    "message": f"Pressure stays above {pmin:g} m even at {max_factor:g}× demand.", "note": _snapshot_note(solver, session)}
        steps = []
        for i in range(16):
            progress(0.1 + 0.6 * i / 16, f"Testing demand × {(lo + hi) / 2:.3f}")
            mid = (lo + hi) / 2
            p, n = min_p(mid)
            steps.append({"factor": round(mid, 4), "min_pressure_m": round(p, 3), "at": n})
            lo, hi = (mid, hi) if p >= pmin else (lo, mid)
        factor = lo
        r = solver.solve(demand_factor=hi)
        order = np.argsort(r["pressure"]) if r is not None else []
        first = [solver.junction_ids[k] for k in order[:10]]
        curve = []
        for f in np.linspace(1.0, min(max_factor, max(1.2, hi * 1.25)), 9):
            p, n = min_p(float(f))
            curve.append({"factor": round(float(f), 3), "min_pressure_m": round(p, 3), "at": n})
        note = _snapshot_note(solver, session)
    out = {"success": True, "analysis": "demand_growth", "min_pressure_m": pmin, "capacity_factor": round(factor, 3),
           "growth_headroom_pct": round((factor - 1) * 100, 1), "current_min_pressure_m": round(p1, 3), "current_min_at": n1,
           "first_failing_nodes": first, "curve": curve, "note": note,
           "method": "Demand-driven; all junction demands scaled together at the peak-hour snapshot; bisection."}
    if verify_eps:
        progress(0.8, "Verifying with a full extended-period run…")
        dur = float(sim.times_h[-1])
        check = session.simulate(SimSettings(duration_h=dur, demand_multiplier=round(factor, 3),
                                             label=f"demand growth check ×{factor:.3f}"))
        out["eps_check"] = {"sim_id": check.id, "demand_multiplier": round(factor, 3),
                            "min_pressure_m": check.summary.get("pressure_m", {}).get("min", {}).get("value"),
                            "min_at": check.summary.get("pressure_m", {}).get("min", {}).get("element"),
                            "note": "Full run lets tank levels change, so its minimum can differ from the snapshot."}
    session.layers["growth_first_fail"] = {"target": "node", "unit": "rank", "title": "First nodes to fail with growth",
                                           "values": {n: float(i + 1) for i, n in enumerate(first)}}
    return out


# ------------------------------------------------------------------ 2.5 source shares
def source_shares(session: Session, duration_h: float | None = None, progress: Progress = _noop) -> dict[str, Any]:
    """Share (%) of each junction's water that comes from each reservoir/tank (one TRACE run per source)."""
    model = session.model
    sources = list(model.sources)
    if not sources:
        return {"success": False, "error": "The network has no reservoirs or tanks."}
    file_h = model.wn.options.time.duration / 3600.0
    dur = float(duration_h) if duration_h else max(72.0, min(file_h, 168.0))
    shares: dict[str, np.ndarray] = {}
    juncs = list(model.wn.junction_name_list)
    for k, src in enumerate(sources):
        progress(0.05 + 0.85 * k / len(sources), f"Tracing water from {src} ({k + 1}/{len(sources)})")
        from .hydraulics import run_simulation  # not stored as a run: would push the user's runs out
        sim = run_simulation(model, SimSettings(duration_h=dur, quality="trace", trace_node=src, label=f"trace {src}"))
        tr = sim.node_data["trace"][:, [sim.node_index[j] for j in juncs]]
        tail = tr[len(tr) // 2:] if len(tr) > 4 else tr
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            shares[src] = np.nanmean(tail, axis=0)
    mat = np.vstack([shares[s] for s in sources])
    dominant = [sources[i] for i in np.nanargmax(np.nan_to_num(mat, nan=-1), axis=0)]
    summary = []
    for i, src in enumerate(sources):
        summary.append({"source": src, "type": model.wn.get_node(src).node_type,
                        "junctions_mostly_supplied": int(sum(1 for d in dominant if d == src)),
                        "junctions_over_5pct": int((mat[i] >= 5).sum()),
                        "base_demand_mostly_supplied_lps": round(sum(model.total_base_demand(j) for j, d in zip(juncs, dominant) if d == src) * 1000, 3)})
        session.layers[f"share_{src}"] = {"target": "node", "unit": "%", "title": f"Share of water from {src}",
                                          "values": {j: (None if not np.isfinite(v) else round(float(v), 2)) for j, v in zip(juncs, mat[i])}}
    mixed = int(((mat >= 20).sum(axis=0) >= 2).sum())
    return {"success": True, "analysis": "source_shares", "duration_h": dur, "sources": summary,
            "mixed_supply_junctions": mixed, "layers": [f"share_{s}" for s in sources],
            "note": "Average over the second half of each run (after start-up effects). Tanks both store and supply water, "
                    "so water 'from a tank' was originally pumped or fed from a reservoir."}
