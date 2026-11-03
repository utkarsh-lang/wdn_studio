"""Excel exports of network data and simulation results."""

from __future__ import annotations

import io
import warnings
from typing import Any

import numpy as np
import pandas as pd

from .criteria import DesignCriteria
from .hydraulics import SimulationResult
from .model import NetworkModel
from .validation import validate

MAX_CELLS_PER_SHEET = 600_000  # keeps the file small and the export fast


def _flatten(d: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            rows += _flatten(v, key + ".")
        elif isinstance(v, list):
            if v and isinstance(v[0], dict):
                continue  # exported as separate sheets
            rows.append((key, ", ".join(map(str, v[:50])) + (f" … ({len(v)} total)" if len(v) > 50 else "")))
        else:
            rows.append((key, v))
    return rows


def _write(sheets: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name[:31], index=False)
            ws = xw.sheets[name[:31]]
            ws.freeze_panes = "B2"
            for col in ws.iter_cols(min_row=1, max_row=1):
                ws.column_dimensions[col[0].column_letter].width = max(10, min(28, len(str(col[0].value)) + 2))
    return buf.getvalue()


def network_workbook(model: NetworkModel) -> bytes:
    summary = pd.DataFrame(_flatten(model.summary()), columns=["item", "value"])
    nodes = pd.DataFrame(model.nodes())
    links = pd.DataFrame([{k: v for k, v in l.items() if k != "vertices"} for l in model.links()])
    issues = pd.DataFrame([{"severity": i["severity"], "message": i["message"], "count": i["count"],
                            "elements": ", ".join(i["elements"][:200])} for i in validate(model)["issues"]])
    return _write({"Summary": summary, "Nodes": nodes, "Links": links, "Model check": issues})


def _timeseries(arr: np.ndarray, ids: list[str], times: np.ndarray, keep: list[int]) -> tuple[pd.DataFrame, str]:
    sub = arr[:, keep]
    step, note = 1, ""
    if sub.size > MAX_CELLS_PER_SHEET:
        step = int(np.ceil(sub.size / MAX_CELLS_PER_SHEET))
        note = f"every {step}th time step"
    cols = [f"{t:.2f} h" for t in times[::step]]
    df = pd.DataFrame(sub[::step].T.astype(float).round(4), columns=cols)
    df.insert(0, "id", [ids[i] for i in keep])
    return df, note


def simulation_workbook(model: NetworkModel, sim: SimulationResult, criteria: DesignCriteria) -> bytes:
    warnings.simplefilter("ignore", RuntimeWarning)
    s = sim.summary
    notes: list[str] = []
    sheets: dict[str, pd.DataFrame] = {}
    flat = [("network", model.name), ("simulation id", sim.id)] + _flatten({k: v for k, v in s.items() if k != "criteria"})
    sheets["Summary"] = pd.DataFrame(flat, columns=["item", "value"])

    junctions = [i for i, n in enumerate(sim.node_ids) if model.wn.get_node(n).node_type == "Junction"]
    nd = sim.node_data
    node_rows = pd.DataFrame({
        "id": [sim.node_ids[i] for i in junctions],
        "elevation_m": [model.wn.get_node(sim.node_ids[i]).elevation for i in junctions],
        "base_demand_lps": [model.total_base_demand(sim.node_ids[i]) * 1000 for i in junctions],
        "min_pressure_m": np.nanmin(nd["pressure"][:, junctions], axis=0),
        "mean_pressure_m": np.nanmean(nd["pressure"][:, junctions], axis=0),
        "max_pressure_m": np.nanmax(nd["pressure"][:, junctions], axis=0),
        "max_demand_lps": np.nanmax(nd["demand"][:, junctions], axis=0),
        "cut_off_from_source": sim.isolated[:, junctions].any(axis=0),
    })
    if "age" in nd:
        node_rows["max_age_h"] = np.nanmax(nd["age"][:, junctions], axis=0)
    if criteria.min_pressure_m is not None:
        node_rows["below_min_pressure"] = node_rows.min_pressure_m < criteria.min_pressure_m
    if criteria.max_pressure_m is not None:
        node_rows["above_max_pressure"] = node_rows.max_pressure_m > criteria.max_pressure_m
    sheets["Junction results"] = node_rows.round(4)

    ld = sim.link_data
    flow, vel, hl = ld["flow"], ld["velocity"], ld["headloss"]
    link_rows = pd.DataFrame({
        "id": sim.link_ids,
        "type": [model.wn.get_link(l).link_type for l in sim.link_ids],
        "max_abs_flow_lps": np.nanmax(np.abs(flow), axis=0),
        "mean_flow_lps": np.nanmean(flow, axis=0),
        "max_velocity_ms": np.nanmax(np.abs(vel), axis=0),
        "mean_velocity_ms": np.nanmean(np.abs(vel), axis=0),
        "max_headloss_m_per_km": np.nanmax(np.abs(hl), axis=0),
        "flow_reverses": (np.sign(flow[1:]) * np.sign(flow[:-1]) < 0).any(axis=0) if len(flow) > 1 else False,
        "hours_closed": (ld["status"] == 0).sum(axis=0) * (s.get("report_step_min", 0) / 60.0),
    })
    if criteria.max_velocity_ms is not None:
        link_rows["above_max_velocity"] = link_rows.max_velocity_ms > criteria.max_velocity_ms
    if criteria.min_velocity_ms is not None:
        link_rows["below_min_peak_velocity"] = link_rows.max_velocity_ms < criteria.min_velocity_ms
    if criteria.max_headloss_m_per_km is not None:
        link_rows["above_max_headloss"] = link_rows.max_headloss_m_per_km > criteria.max_headloss_m_per_km
    sheets["Link results"] = link_rows.round(4)

    if s.get("tanks"):
        sheets["Tanks"] = pd.DataFrame(s["tanks"])
    if s.get("pumps"):
        sheets["Pumps"] = pd.DataFrame(s["pumps"])
    if criteria.is_set():
        checks = sim.evaluate_criteria(model, criteria)
        rows = []
        for key, val in checks.items():
            if isinstance(val, dict) and "count" in val:
                ids = val.get("nodes") or val.get("links") or []
                limit = next((v for k, v in val.items() if k.startswith("limit")), None)
                rows.append({"check": key, "limit": limit, "count": val["count"], "elements": ", ".join(ids)})
        sheets["Criteria"] = pd.DataFrame(rows)

    if len(sim.times_h) > 1:
        all_links = list(range(len(sim.link_ids)))
        for title, arr, ids, keep in (("Pressure (m)", nd["pressure"], sim.node_ids, junctions),
                                      ("Demand (L per s)", nd["demand"], sim.node_ids, junctions),
                                      ("Flow (L per s)", flow, sim.link_ids, all_links),
                                      ("Velocity (m per s)", vel, sim.link_ids, all_links)) + \
                                     ((("Water age (h)", nd["age"], sim.node_ids, junctions),) if "age" in nd else ()):
            df, note = _timeseries(arr, ids, sim.times_h, keep)
            sheets[title] = df
            if note:
                notes.append(f"{title}: {note} to keep the file small.")
    if notes:
        sheets["Summary"] = pd.concat([sheets["Summary"], pd.DataFrame([("export note", n) for n in notes],
                                                                     columns=["item", "value"])])
    return _write(sheets)
