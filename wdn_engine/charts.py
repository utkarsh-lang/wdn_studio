"""Data for the charts panel: distributions and time series of a run (optionally vs a second run)."""

from __future__ import annotations

import warnings
from typing import Any

import numpy as np

from .model import NetworkModel


def _series(arr: np.ndarray) -> list[float | None]:
    return [None if not np.isfinite(v) else round(float(v), 4) for v in arr]


def _hist(values_list: list[np.ndarray], bins: int = 24) -> dict[str, Any]:
    allv = np.concatenate([v[np.isfinite(v)] for v in values_list if v.size]) if values_list else np.array([])
    if allv.size == 0:
        return {"edges": [], "counts": [[] for _ in values_list]}
    lo, hi = float(np.min(allv)), float(np.max(allv))
    if hi - lo < 1e-9:
        hi = lo + 1.0
    edges = np.linspace(lo, hi, bins + 1)
    return {"edges": [round(float(e), 4) for e in edges],
            "counts": [np.histogram(v[np.isfinite(v)], bins=edges)[0].astype(int).tolist() for v in values_list]}


def _run_arrays(model: NetworkModel, sim) -> dict[str, Any]:
    j = [sim.node_index[n] for n in model.wn.junction_name_list]
    p = [sim.link_index[x] for x in model.wn.pipe_name_list]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        pres = sim.node_data["pressure"][:, j]
        out = {
            "min_pressure": np.nanmin(pres, axis=0),
            "max_velocity": np.nanmax(np.abs(sim.link_data["velocity"][:, p]), axis=0) if p else np.array([]),
            "total_demand": np.nansum(sim.node_data["demand"][:, j], axis=1),
            "p_low": np.nanpercentile(pres, 5, axis=1), "p_med": np.nanpercentile(pres, 50, axis=1),
            "p_high": np.nanpercentile(pres, 95, axis=1), "p_min": np.nanmin(pres, axis=1),
        }
    return out


def chart_data(model: NetworkModel, sim, other=None) -> dict[str, Any]:
    a = _run_arrays(model, sim)
    b = _run_arrays(model, other) if other is not None else None
    runs = [sim.id] + ([other.id] if other is not None else [])
    out: dict[str, Any] = {
        "runs": runs, "single_step": len(sim.times_h) <= 1,
        "times_h": [round(float(t), 4) for t in sim.times_h],
        "other_times_h": [round(float(t), 4) for t in other.times_h] if other is not None else None,
        "pressure_hist": {"unit": "m", "title": "Minimum pressure at junctions over the run", **_hist([a["min_pressure"]] + ([b["min_pressure"]] if b else []))},
        "velocity_hist": {"unit": "m/s", "title": "Peak velocity in pipes over the run", **_hist([a["max_velocity"]] + ([b["max_velocity"]] if b else []))},
        "total_demand": {"unit": "L/s", "series": [_series(a["total_demand"])] + ([_series(b["total_demand"])] if b else [])},
        "pressure_band": {"unit": "m", "p5": _series(a["p_low"]), "p50": _series(a["p_med"]), "p95": _series(a["p_high"]), "min": _series(a["p_min"])},
        "tanks": [], "pumps": [],
    }
    for t in model.wn.tank_name_list:
        tank = model.wn.get_node(t)
        lvl = sim.node_data["head"][:, sim.node_index[t]] - tank.elevation
        entry = {"id": t, "unit": "m", "min": tank.min_level, "max": tank.max_level, "series": [_series(lvl)]}
        if other is not None:
            entry["series"].append(_series(other.node_data["head"][:, other.node_index[t]] - tank.elevation))
        out["tanks"].append(entry)
    for pid in model.wn.pump_name_list:
        k = sim.link_index[pid]
        entry = {"id": pid, "flow": [_series(sim.link_data["flow"][:, k])], "status": [_series(sim.link_data["status"][:, k])]}
        if other is not None:
            entry["flow"].append(_series(other.link_data["flow"][:, other.link_index[pid]]))
            entry["status"].append(_series(other.link_data["status"][:, other.link_index[pid]]))
        out["pumps"].append(entry)
    return out
