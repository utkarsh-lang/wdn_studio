"""Time-based questions over a simulation: value at a time, when extremes happen, tank events."""

from __future__ import annotations

import re
import warnings
from typing import Any

import numpy as np

from .hydraulics import LINK_VARS, NODE_VARS, SimulationResult
from .session import Session

UNITS = NODE_VARS | LINK_VARS | {"tank_level": "m", "total_demand": "L/s"}


def _start_s(session: Session) -> float:
    return float(getattr(session.model.wn.options.time, "start_clocktime", 0) or 0)


def clock(session: Session, t_h: float) -> str:
    """Simulation time -> 'Day 2, 08:20' using the file's start clock time."""
    total_min = int(round((_start_s(session) + t_h * 3600) / 60))
    day, rem = divmod(total_min, 24 * 60)
    return f"Day {day + 1}, {rem // 60:02d}:{rem % 60:02d}"


def parse_time(session: Session, sim: SimulationResult, value: Any) -> tuple[int, float, str]:
    """Accept hours (8.5), 'HH:MM', '8 am', '8:30 pm', 'day 2 08:00', 't=10h'. Returns (index, t_h, note)."""
    times = sim.times_h
    note = ""
    if value is None or value == "":
        raise ValueError("Give a time, e.g. 8.5 (hours from start), '08:00' or 'day 2 8 am'.")
    if isinstance(value, (int, float)):
        t = float(value)
    else:
        text = str(value).strip().lower()
        day = 1
        m = re.search(r"day\s*(\d+)", text)
        if m:
            day = int(m.group(1))
            text = text.replace(m.group(0), " ")
        m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text) or re.search(r"\b(\d{1,2}):(\d{2})\b()", text)
        if m:
            hh, mm, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
            if ap == "pm" and hh < 12:
                hh += 12
            if ap == "am" and hh == 12:
                hh = 0
            target_s = (day - 1) * 86400 + hh * 3600 + mm * 60
            t = (target_s - _start_s(session)) / 3600.0
            if t < 0:
                t += 24
        else:
            m = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hours?)?\b", text)
            if not m:
                raise ValueError(f"Could not read the time '{value}'.")
            t = float(m.group(1))
    if t < times[0] - 1e-9 or t > times[-1] + 1e-9:
        raise ValueError(f"Time {t:.2f} h is outside the simulated period (0–{times[-1]:.2f} h). Run a longer simulation.")
    i = int(np.argmin(np.abs(times - t)))
    if abs(times[i] - t) > 1e-6:
        note = f"Nearest reported time step used ({times[i]:.2f} h; report step {np.diff(times).mean() * 60:.0f} min)."
    return i, float(times[i]), note


def value_at(session: Session, element_id: str, time: Any, variable: str | None = None) -> dict[str, Any]:
    sim = session.sim
    if sim is None:
        return {"success": False, "error": "Run a simulation first."}
    nid, lid = session.model.resolve_node(element_id), session.model.resolve_link(element_id)
    if not nid and not lid:
        return {"success": False, "error": f"Unknown element '{element_id}'."}
    try:
        i, t, note = parse_time(session, sim, time)
    except ValueError as error:
        return {"success": False, "error": str(error)}
    kind, eid = ("node", nid) if nid else ("link", lid)
    data, index = (sim.node_data, sim.node_index) if kind == "node" else (sim.link_data, sim.link_index)
    variables = [variable] if variable else [v for v in data if v != "status"]
    vals = {}
    for v in variables:
        if v not in data:
            return {"success": False, "error": f"'{v}' is not available for a {kind} in this run."}
        x = data[v][i, index[eid]]
        vals[v] = None if not np.isfinite(x) else round(float(x), 4)
    if kind == "link":
        st = data["status"][i, index[eid]]
        vals["status"] = {0: "Closed", 1: "Open", 2: "Active"}.get(int(st), str(st)) if np.isfinite(st) else None
    if kind == "node" and session.model.wn.get_node(eid).node_type == "Tank":
        vals["tank_level_m"] = round(float(sim.node_data["head"][i, index[eid]] - session.model.wn.get_node(eid).elevation), 4)
    return {"success": True, "element": eid, "kind": kind, "time_h": t, "clock": clock(session, t), "values": vals,
            "units": {k: UNITS.get(k, "m" if k.endswith("_m") else "") for k in vals}, "note": note, "sim_id": sim.id,
            "time_index": i}


def when_extreme(session: Session, variable: str, extreme: str = "max", element_id: str | None = None) -> dict[str, Any]:
    """When does a variable peak/bottom out — for one element or the whole network."""
    sim = session.sim
    if sim is None:
        return {"success": False, "error": "Run a simulation first."}
    if len(sim.times_h) < 2:
        return {"success": False, "error": "This run has a single time step; run an extended-period simulation (e.g. 24 h)."}
    model = session.model
    extreme = "min" if str(extreme).lower() in ("min", "lowest", "minimum", "low") else "max"
    variable = variable.lower()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        where = None
        if element_id:
            nid, lid = model.resolve_node(element_id), model.resolve_link(element_id)
            if not nid and not lid:
                return {"success": False, "error": f"Unknown element '{element_id}'."}
            if variable == "tank_level":
                if not nid or model.wn.get_node(nid).node_type != "Tank":
                    return {"success": False, "error": "tank_level needs a tank ID."}
                series = sim.node_data["head"][:, sim.node_index[nid]] - model.wn.get_node(nid).elevation
            elif nid and variable in sim.node_data:
                series = sim.node_data[variable][:, sim.node_index[nid]]
            elif lid and variable in sim.link_data:
                series = sim.link_data[variable][:, sim.link_index[lid]]
                if variable in ("flow", "velocity", "headloss"):
                    series = np.abs(series)
            else:
                return {"success": False, "error": f"'{variable}' is not available for '{element_id}'."}
            scope = nid or lid
        else:
            j_idx = [sim.node_index[j] for j in model.wn.junction_name_list]
            if variable in ("demand", "total_demand"):
                variable = "total_demand"
                series = np.nansum(sim.node_data["demand"][:, j_idx], axis=1)
            elif variable in ("pressure", "head", "age"):
                arr = sim.node_data.get(variable)
                if arr is None:
                    return {"success": False, "error": "Water age was not simulated in this run."}
                arr = arr[:, j_idx]
                series = np.nanmin(arr, axis=1) if extreme == "min" else np.nanmax(arr, axis=1)
                where = arr  # to name the node
            elif variable == "tank_level":
                if not model.wn.tank_name_list:
                    return {"success": False, "error": "The network has no tanks."}
                return when_extreme(session, "tank_level", extreme, model.wn.tank_name_list[0])
            elif variable in ("flow", "velocity", "headloss"):
                p_idx = [sim.link_index[p] for p in model.wn.pipe_name_list]
                arr = np.abs(sim.link_data[variable][:, p_idx])
                series = np.nanmax(arr, axis=1) if extreme == "max" else np.nanmin(arr, axis=1)
                where = arr
            else:
                return {"success": False, "error": f"Unknown variable '{variable}'."}
            scope = "network"
        if not np.isfinite(series).any():
            return {"success": False, "error": "No valid values for that variable."}
        i = int(np.nanargmin(series) if extreme == "min" else np.nanargmax(series))
        out = {"success": True, "variable": variable, "extreme": extreme, "scope": scope,
               "value": round(float(series[i]), 4), "unit": UNITS.get(variable, ""),
               "time_h": round(float(sim.times_h[i]), 4), "clock": clock(session, float(sim.times_h[i])),
               "time_index": i, "sim_id": sim.id,
               "series_range": [round(float(np.nanmin(series)), 4), round(float(np.nanmax(series)), 4)]}
        if where is not None:
            row = where[i]
            k = int(np.nanargmin(row) if extreme == "min" else np.nanargmax(row))
            ids = model.wn.junction_name_list if variable in ("pressure", "head", "age") else model.wn.pipe_name_list
            out["element"] = ids[k]
        return out


def tank_events(session: Session, tank_id: str | None = None) -> dict[str, Any]:
    sim = session.sim
    if sim is None:
        return {"success": False, "error": "Run a simulation first."}
    model = session.model
    tanks = [model.resolve_node(tank_id)] if tank_id else list(model.wn.tank_name_list)
    if not tanks or tanks[0] is None:
        return {"success": False, "error": "No such tank (or the network has no tanks)."}
    out = []
    for t in tanks:
        tank = model.wn.get_node(t)
        if tank.node_type != "Tank":
            return {"success": False, "error": f"'{t}' is not a tank."}
        lvl = sim.node_data["head"][:, sim.node_index[t]] - tank.elevation
        times = sim.times_h
        empty = np.where(lvl <= tank.min_level + 1e-3)[0]
        full = np.where(lvl >= tank.max_level - 1e-3)[0]
        trend = np.sign(np.diff(lvl)) if len(lvl) > 1 else np.array([])
        changes = int((trend[1:] * trend[:-1] < 0).sum()) if len(trend) > 1 else 0
        out.append({
            "id": t, "min_level_m": round(float(lvl.min()), 3), "min_at": clock(session, float(times[int(lvl.argmin())])),
            "max_level_m": round(float(lvl.max()), 3), "max_at": clock(session, float(times[int(lvl.argmax())])),
            "start_level_m": round(float(lvl[0]), 3), "final_level_m": round(float(lvl[-1]), 3),
            "limits_m": [tank.min_level, tank.max_level],
            "empties": bool(len(empty)), "first_empty_at": clock(session, float(times[empty[0]])) if len(empty) else None,
            "fills": bool(len(full)), "first_full_at": clock(session, float(times[full[0]])) if len(full) else None,
            "fill_drain_reversals": changes,
            "net_change_m": round(float(lvl[-1] - lvl[0]), 3),
        })
    return {"success": True, "tanks": out, "sim_id": sim.id, "duration_h": round(float(sim.times_h[-1]), 3)}
