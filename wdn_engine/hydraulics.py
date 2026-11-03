"""Hydraulic (and water-age) simulation with EPANET via WNTR, plus KPIs."""

from __future__ import annotations

import copy
import math
import tempfile
import threading
import warnings
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import networkx as nx
import numpy as np
import wntr

from .criteria import DesignCriteria
from .model import NetworkModel, fnum

LPS = 1000.0
MAX_DURATION_H = 24 * 14
MAX_FRAMES = 400
_EPANET_LOCK = threading.Lock()  # serialise toolkit runs: predictable memory/CPU on small hosts

NODE_VARS = {"pressure": "m", "head": "m", "demand": "L/s", "age": "h", "chlorine": "mg/L", "trace": "%", "leak": "L/s"}
QUALITY_MODES = ("none", "age", "chlorine", "trace")
CD_ORIFICE = 0.75
LINK_VARS = {"flow": "L/s", "velocity": "m/s", "headloss": "m/km", "status": ""}


@dataclass
class SimSettings:
    duration_h: float | None = None          # None -> file duration (capped)
    report_step_min: float | None = None     # None -> automatic (<= MAX_FRAMES frames)
    demand_model: str = "DD"                 # DD (demand-driven) | PDD (pressure-driven)
    required_pressure_m: float = 20.0        # PDD: pressure for full demand
    minimum_pressure_m: float = 0.0          # PDD: pressure below which demand is zero
    demand_multiplier: float = 1.0
    closed_links: list[str] = field(default_factory=list)
    water_age: bool = False
    quality: str = "none"                    # none | age | chlorine | trace
    chlorine_mgl: float = 1.0                # chlorine at reservoirs (and initial in tanks)
    bulk_coeff_per_day: float = -0.5         # first-order bulk decay (negative = decay)
    wall_coeff_m_per_day: float = 0.0        # wall reaction coefficient
    trace_node: str | None = None            # source to trace (percentage of water from it)
    leaks: list[dict[str, Any]] = field(default_factory=list)   # [{"element": id, "diameter_mm": 10}]
    label: str = ""

    @property
    def quality_mode(self) -> str:
        return self.quality if self.quality != "none" else ("age" if self.water_age else "none")

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SimSettings":
        data = dict(data or {})
        out = cls()
        for key in asdict(out):
            if key in data and data[key] is not None:
                setattr(out, key, data[key])
        out.demand_model = "PDD" if str(out.demand_model).upper() == "PDD" else "DD"
        out.closed_links = [str(x) for x in (out.closed_links or [])][:200]
        out.demand_multiplier = float(out.demand_multiplier)
        out.quality = str(out.quality or "none").lower()
        if out.quality not in QUALITY_MODES:
            raise ValueError(f"quality must be one of {', '.join(QUALITY_MODES)}.")
        if out.quality == "age":
            out.water_age = True
        if out.quality == "trace" and not out.trace_node:
            raise ValueError("Choose a trace_node (reservoir or tank) for source tracing.")
        leaks = []
        for lk in (out.leaks or [])[:20]:
            d = float(lk.get("diameter_mm", 10))
            if not 0 < d <= 500:
                raise ValueError("Leak diameter must be between 0 and 500 mm.")
            leaks.append({"element": str(lk.get("element") or lk.get("node") or lk.get("link") or ""), "diameter_mm": d})
        out.leaks = leaks
        if not 0 <= out.demand_multiplier <= 10:
            raise ValueError("demand_multiplier must be between 0 and 10.")
        return out


class SimulationResult:
    def __init__(self, sim_id: str, settings: SimSettings, times_h: np.ndarray,
                 node_ids: list[str], link_ids: list[str],
                 node_data: dict[str, np.ndarray], link_data: dict[str, np.ndarray],
                 summary: dict[str, Any]):
        self.id = sim_id
        self.settings = settings
        self.times_h = times_h
        self.node_ids = node_ids
        self.link_ids = link_ids
        self.node_index = {n: i for i, n in enumerate(node_ids)}
        self.link_index = {l: i for i, l in enumerate(link_ids)}
        self.node_data = node_data
        self.link_data = link_data
        self.summary = summary

    # ----------------------------------------------------------- data access
    def _array(self, var: str) -> tuple[np.ndarray, str]:
        if var in self.node_data:
            return self.node_data[var], "node"
        if var in self.link_data:
            return self.link_data[var], "link"
        raise KeyError(f"Variable '{var}' is not available in this simulation.")

    def frame(self, var: str, t: int | None = None, stat: str | None = None) -> dict[str, Any]:
        arr, kind = self._array(var)
        if stat:
            fn = {"min": np.nanmin, "max": np.nanmax, "mean": np.nanmean,
                  "absmax": lambda a, axis: np.nanmax(np.abs(a), axis=axis)}[stat]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                values = fn(arr, axis=0)
            label = ("max |value|" if stat == "absmax" else stat) + " over period"
        else:
            t = int(np.clip(t or 0, 0, len(self.times_h) - 1))
            values = arr[t]
            label = f"t = {self.times_h[t]:.2f} h"
        ids = self.node_ids if kind == "node" else self.link_ids
        return {"var": var, "kind": kind, "unit": (NODE_VARS | LINK_VARS)[var], "label": label,
                "values": {i: (None if not np.isfinite(v) else round(float(v), 4)) for i, v in zip(ids, values)}}

    def series(self, kind: str, element_id: str) -> dict[str, Any]:
        data, index = (self.node_data, self.node_index) if kind == "node" else (self.link_data, self.link_index)
        if element_id not in index:
            raise KeyError(f"Unknown {kind} '{element_id}'.")
        i = index[element_id]
        return {"times_h": [round(float(t), 4) for t in self.times_h],
                "series": {var: [None if not np.isfinite(v) else round(float(v), 4) for v in arr[:, i]]
                           for var, arr in data.items()},
                "units": {v: (NODE_VARS | LINK_VARS)[v] for v in data}}

    def evaluate_criteria(self, model: NetworkModel, criteria: DesignCriteria) -> dict[str, Any]:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            return _criteria_checks(model, self, criteria)


# --------------------------------------------------------------------- run
def _prepare(model: NetworkModel, s: SimSettings) -> tuple[wntr.network.WaterNetworkModel, list[str]]:
    wn = copy.deepcopy(model.wn)
    notes: list[str] = []
    file_h = wn.options.time.duration / 3600.0
    duration_h = file_h if s.duration_h is None else float(s.duration_h)
    duration_h = max(0.0, min(duration_h, MAX_DURATION_H))
    if s.duration_h is None and file_h > MAX_DURATION_H:
        notes.append(f"Duration capped at {MAX_DURATION_H} h.")
    wn.options.time.duration = int(round(duration_h * 3600))
    hyd = max(60, int(wn.options.time.hydraulic_timestep or 3600))
    if s.report_step_min:
        step = max(hyd, int(float(s.report_step_min) * 60))
    else:
        step = hyd
        while wn.options.time.duration / step > MAX_FRAMES:
            step *= 2
    step = max(hyd, (step // hyd) * hyd)
    wn.options.time.report_timestep = step
    wn.options.time.report_start = 0
    wn.options.time.pattern_timestep = wn.options.time.pattern_timestep or hyd
    wn.options.time.hydraulic_timestep = min(hyd, step)
    wn.options.hydraulic.demand_multiplier = float(wn.options.hydraulic.demand_multiplier or 1.0) * s.demand_multiplier
    if s.demand_model == "PDD":
        wn.options.hydraulic.demand_model = "PDD"
        wn.options.hydraulic.required_pressure = float(s.required_pressure_m)
        wn.options.hydraulic.minimum_pressure = float(s.minimum_pressure_m)
    else:
        wn.options.hydraulic.demand_model = "DDA"
    mode = s.quality_mode
    if mode == "age":
        wn.options.quality.parameter = "AGE"
    elif mode == "chlorine":
        wn.options.quality.parameter = "CHEMICAL"
        wn.options.reaction.bulk_coeff = float(s.bulk_coeff_per_day) / 86400.0      # SI: 1/s
        wn.options.reaction.wall_coeff = float(s.wall_coeff_m_per_day) / 86400.0    # SI: m/s
        for r in wn.reservoir_name_list:
            wn.get_node(r).initial_quality = float(s.chlorine_mgl) / 1000.0         # SI: kg/m3
        for t in wn.tank_name_list:
            wn.get_node(t).initial_quality = float(s.chlorine_mgl) / 1000.0
        notes.append(f"Chlorine {s.chlorine_mgl:g} mg/L at sources, bulk k = {s.bulk_coeff_per_day:g} /day, "
                     f"wall k = {s.wall_coeff_m_per_day:g} m/day; junctions start at 0 mg/L (needs a long run to settle).")
    elif mode == "trace":
        src = model.resolve_node(s.trace_node)
        if src is None:
            raise ValueError(f"Unknown trace node '{s.trace_node}'.")
        wn.options.quality.parameter = "TRACE"
        wn.options.quality.trace_node = src
    else:
        wn.options.quality.parameter = "NONE"
    leak_nodes: dict[str, float] = {}
    for lk in s.leaks:
        area = math.pi * (lk["diameter_mm"] / 1000.0) ** 2 / 4
        coeff = CD_ORIFICE * area * math.sqrt(2 * 9.81)       # Q = Cd*A*sqrt(2 g p), SI
        nid, lid = model.resolve_node(lk["element"]), model.resolve_link(lk["element"])
        if nid:
            if wn.get_node(nid).node_type != "Junction":
                raise ValueError(f"Leaks can only be placed at junctions (or pipes), not '{nid}'.")
            targets = [nid]
        elif lid:
            link = wn.get_link(lid)
            targets = [n for n in (link.start_node_name, link.end_node_name) if wn.get_node(n).node_type == "Junction"]
        else:
            raise ValueError(f"Unknown leak location '{lk['element']}'.")
        for n in targets:
            j = wn.get_node(n)
            j.emitter_coefficient = (j.emitter_coefficient or 0.0) + coeff / len(targets)
            leak_nodes[n] = leak_nodes.get(n, 0.0) + coeff / len(targets)
    if leak_nodes:
        notes.append("Leaks modelled as orifices (emitters, Cd = 0.75): " + ", ".join(
            f"{lk['diameter_mm']:g} mm at {lk['element']}" for lk in s.leaks) + ".")
    wn._wdn_leaks = leak_nodes

    closed = []
    for requested in s.closed_links:
        lid = model.resolve_link(requested)
        if lid is None:
            raise ValueError(f"Cannot close unknown link '{requested}'.")
        link = wn.get_link(lid)
        link.initial_status = wntr.network.LinkStatus.Closed
        closed.append(lid)
    if closed:
        removed = []
        for name, ctrl in list(wn.controls()):
            targets = {getattr(o, "name", None) for o in ctrl.requires()}
            if targets & set(closed):
                wn.remove_control(name)
                removed.append(name)
        if removed:
            notes.append(f"Removed {len(removed)} control(s) acting on closed links so they stay closed.")
        notes.append(f"Closed links: {', '.join(closed)}.")
    return wn, notes


def run_simulation(model: NetworkModel, settings: SimSettings, criteria: DesignCriteria | None = None) -> SimulationResult:
    started = time.perf_counter()
    wn, notes = _prepare(model, settings)
    with _EPANET_LOCK, tempfile.TemporaryDirectory() as tmp:
        try:
            res = wntr.sim.EpanetSimulator(wn).run_sim(file_prefix=f"{tmp}/sim")
        except Exception as error:
            raise RuntimeError(f"EPANET could not solve this scenario: {error}") from error
    elapsed = time.perf_counter() - started

    node_ids, link_ids = list(model.wn.node_name_list), list(model.wn.link_name_list)
    times_h = np.asarray(res.node["pressure"].index, dtype=float) / 3600.0

    def take(frame, ids, scale=1.0):
        return (frame.reindex(columns=ids).to_numpy(dtype=np.float32) * scale).astype(np.float32)

    node_data = {
        "pressure": take(res.node["pressure"], node_ids),
        "head": take(res.node["head"], node_ids),
        "demand": take(res.node["demand"], node_ids, LPS),
    }
    mode = settings.quality_mode
    if mode != "none" and "quality" in res.node:
        scale = {"age": 1 / 3600.0, "chlorine": 1000.0, "trace": 1.0}[mode]
        node_data[mode] = take(res.node["quality"], node_ids, scale)
    leaks = getattr(wn, "_wdn_leaks", {})
    if leaks:
        leak = np.zeros_like(node_data["pressure"])
        for n, c in leaks.items():
            i = node_ids.index(n)
            leak[:, i] = c * np.sqrt(np.clip(node_data["pressure"][:, i], 0, None)) * LPS
        node_data["leak"] = leak.astype(np.float32)
    headloss = take(res.link["headloss"], link_ids)
    # WNTR reports pipe headloss as a gradient (m/m); express as m/km for pipes.
    pipe_mask = np.array([model.wn.get_link(l).link_type == "Pipe" for l in link_ids])
    headloss[:, pipe_mask] *= 1000.0
    link_data = {
        "flow": take(res.link["flowrate"], link_ids, LPS),
        "velocity": take(res.link["velocity"], link_ids),
        "headloss": headloss,
        "status": take(res.link["status"], link_ids),
    }
    isolated = _isolated_nodes(model, node_ids, link_ids, link_data["status"])
    if isolated.any():
        # EPANET returns meaningless heads (often -1e4..-1e5 m) for nodes cut off from every source.
        for var in ("pressure", "head", "age"):
            if var in node_data:
                node_data[var][isolated] = np.nan
    sim = SimulationResult(uuid.uuid4().hex[:10], settings, times_h, node_ids, link_ids, node_data, link_data, {})
    sim.isolated = isolated
    sim.element_types = {n: model.wn.get_node(n).node_type for n in node_ids} | {l: model.wn.get_link(l).link_type for l in link_ids}
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        sim.summary = _summary(model, wn, res, sim, settings, notes, elapsed, criteria)
    return sim


def _isolated_nodes(model: NetworkModel, node_ids: list[str], link_ids: list[str], status: np.ndarray) -> np.ndarray:
    """Boolean [time, node]: node has no open path to any reservoir/tank at that time step.

    Uses the *simulated* link status, so pumps/valves switched by controls are respected.
    Only distinct open/closed patterns are evaluated (usually a handful).
    """
    out = np.zeros((status.shape[0], len(node_ids)), dtype=bool)
    ends = [(model.wn.get_link(l).start_node_name, model.wn.get_link(l).end_node_name) for l in link_ids]
    sources = model.sources
    nidx = {n: i for i, n in enumerate(node_ids)}
    cache: dict[bytes, np.ndarray] = {}
    for t in range(status.shape[0]):
        closed = status[t] == 0
        key = np.packbits(closed).tobytes()
        if key not in cache:
            g = nx.Graph()
            g.add_nodes_from(node_ids)
            g.add_edges_from(e for e, c in zip(ends, closed) if not c)
            reach: set[str] = set()
            for src in sources:
                if src not in reach:
                    reach |= nx.node_connected_component(g, src)
            row = np.ones(len(node_ids), dtype=bool)
            for n in reach:
                row[nidx[n]] = False
            cache[key] = row
        out[t] = cache[key]
    return out


def compare(a: "SimulationResult", b: "SimulationResult", var: str, stat: str | None, t: int = 0) -> dict[str, Any]:
    """Change in a variable from run ``a`` (base) to run ``b`` (scenario): b - a."""
    fa, fb = a.frame(var, t, stat), b.frame(var, t, stat)
    kind = fa["kind"]
    tol = {"pressure": 0.01, "head": 0.01, "demand": 1e-3, "age": 0.01, "flow": 1e-3, "velocity": 1e-4, "headloss": 1e-3}.get(var, 1e-6)
    diff: dict[str, float | None] = {}
    for eid, va in fa["values"].items():
        vb = fb["values"].get(eid)
        if va is None or vb is None:
            diff[eid] = None
        elif var in ("flow", "velocity", "headloss"):
            diff[eid] = round(abs(vb) - abs(va), 4)  # change in magnitude
        else:
            diff[eid] = round(vb - va, 4)
    model_types = getattr(a, "element_types", None) or {}
    vals = [(k, v) for k, v in diff.items() if v is not None and model_types.get(k, "Junction" if kind == "node" else "Pipe")
            in ("Junction", "Pipe")]
    changed = [k for k, v in vals if abs(v) > tol]
    lost = [k for k, v in diff.items() if v is None and fa["values"].get(k) is not None]
    inc = max(vals, key=lambda kv: kv[1], default=(None, 0.0))
    dec = min(vals, key=lambda kv: kv[1], default=(None, 0.0))
    return {
        "success": True, "var": var, "kind": kind, "unit": fa["unit"],
        "label": f"Δ {var} ({fa['label']}): run {b.id} − run {a.id}",
        "base": a.id, "scenario": b.id, "values": diff,
        "changed_count": len(changed), "changed": changed[:500], "tolerance": tol,
        "lost_supply": lost[:500], "lost_supply_count": len(lost),
        "max_increase": {"element": inc[0], "value": inc[1]},
        "max_decrease": {"element": dec[0], "value": dec[1]},
        "mean_abs_change": round(sum(abs(v) for _, v in vals) / len(vals), 5) if vals else 0.0,
    }


# ------------------------------------------------------------------- KPIs
def _argext(arr: np.ndarray, ids: list[str], times: np.ndarray, fn) -> dict[str, Any]:
    if arr.size == 0:
        return {}
    if not np.isfinite(arr).any():
        return {}
    flat = fn(arr)
    t, i = np.unravel_index(flat, arr.shape)
    return {"value": round(float(arr[t, i]), 3), "element": ids[i], "time_h": round(float(times[t]), 3)}


def _summary(model, wn, res, sim: SimulationResult, s: SimSettings, notes, elapsed, criteria) -> dict[str, Any]:
    junctions = list(model.wn.junction_name_list)
    j_idx = [sim.node_index[j] for j in junctions]
    pressure = sim.node_data["pressure"][:, j_idx]
    demand = sim.node_data["demand"][:, j_idx]
    times = sim.times_h
    dt_h = float(np.diff(times).mean()) if len(times) > 1 else 0.0

    out: dict[str, Any] = {
        "sim_id": sim.id,
        "settings": asdict(s),
        "notes": notes,
        "frames": len(times),
        "duration_h": round(float(times[-1]) if len(times) else 0.0, 3),
        "report_step_min": round(dt_h * 60, 2),
        "run_time_s": round(elapsed, 3),
        "demand_model": s.demand_model,
        "single_step": len(times) <= 1,
    }
    if "leak" in sim.node_data:
        total = np.nansum(sim.node_data["leak"], axis=1)
        dt_s = float(np.diff(times).mean()) * 3600 if len(times) > 1 else 0.0
        out["leakage"] = {"mean_lps": round(float(np.nanmean(total)), 4), "max_lps": round(float(np.nanmax(total)), 4),
                          "volume_m3": round(float(np.nansum(total) * dt_s / 1000.0), 2) if dt_s else None,
                          "share_of_supply_pct": round(100 * float(np.nanmean(total)) / max(1e-9, float(np.nanmean(np.nansum(demand, axis=1)))), 2),
                          "nodes": list(getattr(wn, "_wdn_leaks", {}).keys())}
    if "chlorine" in sim.node_data:
        cl = sim.node_data["chlorine"][:, j_idx]
        tail = cl[len(cl) // 2:] if len(cl) > 4 else cl
        out["chlorine_mgl"] = {"min_in_second_half": round(float(np.nanmin(tail)), 4), "mean_in_second_half": round(float(np.nanmean(tail)), 4),
                               "junctions_below_0_2": int((np.nanmin(tail, axis=0) < 0.2).sum())}
    if "trace" in sim.node_data:
        tr = sim.node_data["trace"][:, j_idx]
        tail = tr[len(tr) // 2:] if len(tr) > 4 else tr
        share = np.nanmean(tail, axis=0)
        out["trace"] = {"source": s.trace_node, "junctions_mostly_supplied_pct50": int((share >= 50).sum()),
                        "junctions_any_supply_pct5": int((share >= 5).sum()), "mean_share_pct": round(float(np.nanmean(share)), 2)}
    if s.quality_mode in ("age", "chlorine", "trace") and (len(times) <= 1 or times[-1] < 48):
        out["notes"].append("Water quality (age, chlorine, tracing) needs an extended-period run to build up "
                            "(72 h or more is typical); short runs are dominated by the initial conditions.")
    if len(times) <= 1:
        out["notes"].append("Steady state (one time step): min, max and mean are identical and there is nothing to play. "
                            "Set Duration (e.g. 24 h) for a time-varying run.")
    iso_any = sim.isolated.any(axis=0)
    iso = [sim.node_ids[i] for i in np.where(iso_any)[0] if model.wn.get_node(sim.node_ids[i]).node_type == "Junction"]
    out["isolated_junctions_count"] = len(iso)
    out["isolated_junctions"] = iso[:500]
    if iso:
        lost = sum(model.total_base_demand(n) for n in iso) * LPS
        out["notes"].append(f"{len(iso)} junction(s) were cut off from every source at some time "
                            f"({lost:.2f} L/s base demand). Their pressures are blanked (EPANET's values there are not physical).")
    if junctions:
        out["pressure_m"] = {
            "min": _argext(pressure, junctions, times, np.nanargmin),
            "max": _argext(pressure, junctions, times, np.nanargmax),
            "mean": round(float(np.nanmean(pressure)), 3),
        }
        neg = np.nanmin(pressure, axis=0) < 0
        out["junctions_with_negative_pressure"] = int(neg.sum())
        if neg.any() and s.demand_model == "DD":
            out["notes"].append("Negative pressures under demand-driven analysis: results there are not physical; "
                                "try pressure-driven (PDD) mode.")
        delivered = float(np.nansum(demand) * dt_h * 3.6) if dt_h else float(np.nansum(demand))
        out["delivered_volume_m3"] = round(delivered, 2) if dt_h else None
        out["peak_total_demand_lps"] = round(float(np.nanmax(np.nansum(demand, axis=1))), 3)
        if s.demand_model == "PDD":
            try:
                expected = wntr.metrics.expected_demand(wn).reindex(columns=junctions)
                expected = expected.reindex(index=res.node["demand"].index).to_numpy() * LPS
                ratio = float(np.nansum(demand) / np.nansum(expected)) if np.nansum(expected) > 0 else None
                out["demand_satisfaction_ratio"] = None if ratio is None else round(ratio, 4)
                short = np.nansum(expected, axis=0) - np.nansum(demand, axis=0)
                out["junctions_with_shortfall"] = int((short > 1e-3 * np.maximum(1, np.nansum(expected, axis=0))).sum())
            except Exception as error:  # keep the run usable
                out["notes"].append(f"Demand satisfaction not computed: {error}")

    pipes = list(model.wn.pipe_name_list)
    if pipes:
        p_idx = [sim.link_index[p] for p in pipes]
        vel = np.abs(sim.link_data["velocity"][:, p_idx])
        out["velocity_ms"] = {"max": _argext(vel, pipes, times, np.nanargmax),
                              "mean": round(float(np.nanmean(vel)), 4)}
        hl = np.abs(sim.link_data["headloss"][:, p_idx])
        out["headloss_m_per_km"] = {"max": _argext(hl, pipes, times, np.nanargmax)}
        flow = sim.link_data["flow"][:, p_idx]
        reversals = (np.sign(flow[1:]) * np.sign(flow[:-1]) < 0).any(axis=0) if len(times) > 1 else np.zeros(len(pipes), bool)
        out["pipes_with_flow_reversal"] = int(reversals.sum())

    tanks = []
    for t in model.wn.tank_name_list:
        tank = model.wn.get_node(t)
        level = sim.node_data["head"][:, sim.node_index[t]] - tank.elevation
        tanks.append({"id": t, "min_level_m": round(float(level.min()), 3), "max_level_m": round(float(level.max()), 3),
                      "final_level_m": round(float(level[-1]), 3), "capacity_min_max_m": [tank.min_level, tank.max_level],
                      "hit_empty": bool(level.min() <= tank.min_level + 1e-3), "hit_full": bool(level.max() >= tank.max_level - 1e-3)})
    out["tanks"] = tanks

    pumps = []
    if model.wn.pump_name_list:
        try:
            power = wntr.metrics.pump_power(res.link["flowrate"], res.node["head"], wn)
        except Exception:
            power = None
        for p in model.wn.pump_name_list:
            status = sim.link_data["status"][:, sim.link_index[p]]
            entry = {"id": p, "hours_running": round(float((status > 0).sum() * dt_h), 2)}
            if power is not None and p in power:
                kw = power[p].to_numpy() / 1000.0
                entry["energy_kwh"] = round(float(np.nansum(kw) * dt_h), 2)
                entry["max_power_kw"] = round(float(np.nanmax(kw)), 2)
            pumps.append(entry)
    out["pumps"] = pumps

    pstar = criteria.min_pressure_m if criteria and criteria.min_pressure_m is not None else None
    if pstar is not None and junctions:
        try:
            todini = wntr.metrics.todini_index(res.node["head"], res.node["pressure"], res.node["demand"],
                                               res.link["flowrate"], wn, pstar)
            out["todini_resilience"] = {"min": fnum(todini.min(), 4), "mean": fnum(todini.mean(), 4),
                                        "required_pressure_m": pstar}
        except Exception as error:
            out["notes"].append(f"Todini index not computed: {error}")
    if criteria is not None and criteria.is_set():
        out["criteria"] = _criteria_checks(model, sim, criteria)
    return out


def _criteria_checks(model: NetworkModel, sim: SimulationResult, c: DesignCriteria) -> dict[str, Any]:
    checks: dict[str, Any] = {"criteria": c.to_dict()}
    junctions = list(model.wn.junction_name_list)
    j_idx = [sim.node_index[j] for j in junctions]
    if junctions:
        p = sim.node_data["pressure"][:, j_idx]
        dem = np.array([model.total_base_demand(j) for j in junctions]) * LPS
        if c.min_pressure_m is not None:
            low = np.nanmin(p, axis=0) < c.min_pressure_m
            hours = (p < c.min_pressure_m).sum(axis=0)
            ids = [junctions[i] for i in np.where(low)[0]]
            checks["low_pressure"] = {"limit_m": c.min_pressure_m, "count": len(ids), "nodes": ids[:500],
                                      "share_of_junctions": round(len(ids) / len(junctions), 4),
                                      "base_demand_affected_lps": round(float(dem[low].sum()), 3),
                                      "worst_frames_below": int(hours.max()) if len(hours) else 0}
        if c.max_pressure_m is not None:
            high = np.nanmax(p, axis=0) > c.max_pressure_m
            ids = [junctions[i] for i in np.where(high)[0]]
            checks["high_pressure"] = {"limit_m": c.max_pressure_m, "count": len(ids), "nodes": ids[:500],
                                       "share_of_junctions": round(len(ids) / len(junctions), 4)}
    pipes = list(model.wn.pipe_name_list)
    if pipes:
        p_idx = [sim.link_index[x] for x in pipes]
        v = np.abs(sim.link_data["velocity"][:, p_idx])
        if c.max_velocity_ms is not None:
            ids = [pipes[i] for i in np.where(np.nanmax(v, axis=0) > c.max_velocity_ms)[0]]
            checks["high_velocity"] = {"limit_ms": c.max_velocity_ms, "count": len(ids), "links": ids[:500]}
        if c.min_velocity_ms is not None:
            ids = [pipes[i] for i in np.where(np.nanmax(v, axis=0) < c.min_velocity_ms)[0]]
            checks["low_velocity"] = {"limit_ms": c.min_velocity_ms, "count": len(ids), "links": ids[:500],
                                      "note": "Peak velocity never reaches the minimum (stagnation/water-age risk)."}
        if c.max_headloss_m_per_km is not None:
            hl = np.abs(sim.link_data["headloss"][:, p_idx])
            ids = [pipes[i] for i in np.where(np.nanmax(hl, axis=0) > c.max_headloss_m_per_km)[0]]
            checks["high_headloss"] = {"limit_m_per_km": c.max_headloss_m_per_km, "count": len(ids), "links": ids[:500]}
    return checks
