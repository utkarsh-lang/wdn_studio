"""Element tables + a safe structured query engine (no free-form code).

Every node/link becomes a row with static attributes, topology flags, cached
graph metrics and — when a simulation exists — per-element simulation
statistics over the run.  ``query`` filters, sorts and aggregates these rows
from a JSON spec, so an LLM can ask arbitrary combined questions while every
number is still computed here.
"""

from __future__ import annotations

import warnings
from typing import Any

import numpy as np
import pandas as pd

from . import topology as T
from .session import Session

OPS = {"<", "<=", ">", ">=", "=", "!=", "between", "in", "contains", "is_true", "is_false"}
AGG = {"count", "sum", "mean", "median", "min", "max"}
TABLE_ALIASES = {"nodes": "nodes", "node": "nodes", "junctions": "nodes", "junction": "nodes",
                 "links": "links", "link": "links", "pipes": "links", "pipe": "links"}

NODE_COLUMNS_DOC = ("id, type (Junction/Reservoir/Tank), elevation_m, base_demand_lps, demand_categories, degree, "
                    "is_dead_end, is_articulation, x, y; after a simulation: min_pressure_m, mean_pressure_m, "
                    "max_pressure_m, min_head_m, max_head_m, max_demand_lps, mean_demand_lps, max_age_h, "
                    "cut_off_from_source; plus any computed metric (e.g. betweenness_centrality)")
LINK_COLUMNS_DOC = ("id, type (Pipe/Pump/Valve), from, to, length_m, diameter_mm, roughness, status, check_valve, "
                    "is_bridge; after a simulation: max_abs_flow_lps, mean_flow_lps, max_velocity_ms, "
                    "mean_velocity_ms, min_velocity_ms, max_headloss_m_per_km, flow_reverses, hours_closed; plus any "
                    "computed metric (e.g. edge_betweenness)")


def _metric_count(model, kind: str) -> int:
    return sum(1 for k in model._cache if isinstance(k, tuple) and k and k[0] == kind)


def node_table(session: Session) -> pd.DataFrame:
    model, sim = session.model, session.sim
    key = ("node_table", sim.id if sim else None, _metric_count(model, "node_metric"))
    if key in model._cache:
        return model._cache[key]
    df = pd.DataFrame(model.nodes()).drop(columns=["init_level_m", "min_level_m", "max_level_m", "diameter_m"], errors="ignore")
    char = T.characterization(model)
    _, g = model.graphs()
    df["degree"] = [g.degree(n) for n in df["id"]]
    dead, arts = set(char["dead_end_junctions"]), set(char["articulation_points"])
    df["is_dead_end"] = df["id"].isin(dead)
    df["is_articulation"] = df["id"].isin(arts)
    for k, res in list(model._cache.items()):
        if isinstance(k, tuple) and len(k) == 3 and k[0] == "node_metric" and res.get("success"):
            col = res["metric"] + ("_weighted" if k[2] else "")
            df[col] = df["id"].map(res["values"])
    if sim is not None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            idx = [sim.node_index[n] for n in df["id"]]
            p, h, d = (sim.node_data[v][:, idx] for v in ("pressure", "head", "demand"))
            df["min_pressure_m"], df["mean_pressure_m"], df["max_pressure_m"] = np.nanmin(p, 0), np.nanmean(p, 0), np.nanmax(p, 0)
            df["min_head_m"], df["max_head_m"] = np.nanmin(h, 0), np.nanmax(h, 0)
            df["max_demand_lps"], df["mean_demand_lps"] = np.nanmax(d, 0), np.nanmean(d, 0)
            if "age" in sim.node_data:
                df["max_age_h"] = np.nanmax(sim.node_data["age"][:, idx], 0)
            df["cut_off_from_source"] = sim.isolated[:, idx].any(0)
        # pressure columns are only meaningful for junctions
        for col in ("min_pressure_m", "mean_pressure_m", "max_pressure_m"):
            df.loc[df["type"] != "Junction", col] = np.nan
    model._cache[key] = df
    return df


def link_table(session: Session) -> pd.DataFrame:
    model, sim = session.model, session.sim
    key = ("link_table", sim.id if sim else None, _metric_count(model, "link_metric"))
    if key in model._cache:
        return model._cache[key]
    df = pd.DataFrame([{k: v for k, v in l.items() if k != "vertices"} for l in model.links()])
    bridges = {frozenset(b) for b in T._bridges(model)}
    df["is_bridge"] = [frozenset((a, b)) in bridges for a, b in zip(df["from"], df["to"])]
    for k, res in list(model._cache.items()):
        if isinstance(k, tuple) and len(k) == 3 and k[0] == "link_metric" and res.get("success"):
            df[res["metric"] + ("_weighted" if k[2] else "")] = df["id"].map(res["values"])
    if sim is not None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            idx = [sim.link_index[l] for l in df["id"]]
            q, v, hl, st = (sim.link_data[x][:, idx] for x in ("flow", "velocity", "headloss", "status"))
            df["max_abs_flow_lps"], df["mean_flow_lps"] = np.nanmax(np.abs(q), 0), np.nanmean(q, 0)
            df["max_velocity_ms"], df["mean_velocity_ms"] = np.nanmax(np.abs(v), 0), np.nanmean(np.abs(v), 0)
            df["min_velocity_ms"] = np.nanmin(np.abs(v), 0)
            df["max_headloss_m_per_km"] = np.nanmax(np.abs(hl), 0)
            df["flow_reverses"] = (np.sign(q[1:]) * np.sign(q[:-1]) < 0).any(0) if len(q) > 1 else False
            step_h = float(np.diff(sim.times_h).mean()) if len(sim.times_h) > 1 else 0.0
            df["hours_closed"] = (st == 0).sum(0) * step_h
    model._cache[key] = df
    return df


def _mask(df: pd.DataFrame, f: dict[str, Any]) -> pd.Series:
    col, op, val = f.get("column"), str(f.get("op", "=")).strip(), f.get("value")
    if col not in df.columns:
        raise KeyError(col)
    if op not in OPS:
        raise ValueError(f"Unknown operator '{op}'. Use one of: {', '.join(sorted(OPS))}.")
    s = df[col]
    if op == "is_true":
        return s.fillna(False).astype(bool)
    if op == "is_false":
        return ~s.fillna(False).astype(bool)
    if op == "contains":
        return s.astype(str).str.contains(str(val), case=False, regex=False)
    if op == "in":
        vals = val if isinstance(val, list) else [val]
        return s.astype(str).str.casefold().isin([str(v).casefold() for v in vals])
    if pd.api.types.is_bool_dtype(s) or not pd.api.types.is_numeric_dtype(s):
        if op in ("=", "!="):
            if isinstance(val, bool) or str(val).lower() in ("true", "false"):
                target = val if isinstance(val, bool) else str(val).lower() == "true"
                m = s.fillna(False).astype(bool) == target
            else:
                m = s.astype(str).str.casefold() == str(val).casefold()
            return m if op == "=" else ~m
        raise ValueError(f"Column '{col}' is not numeric; use =, !=, in or contains.")
    s = pd.to_numeric(s, errors="coerce")
    if op == "between":
        lo, hi = (val if isinstance(val, (list, tuple)) else [None, None])[:2]
        return (s >= float(lo)) & (s <= float(hi))
    x = float(val)
    tol = 1e-9 if col != "diameter_mm" else 0.5
    return {"<": s < x - 1e-12, "<=": s <= x + 1e-12, ">": s > x + 1e-12, ">=": s >= x - 1e-12,
            "=": (s - x).abs() <= tol, "!=": (s - x).abs() > tol}[op]


def query(session: Session, table: str, filters: list[dict[str, Any]] | None = None, sort_by: str | None = None,
          descending: bool = True, limit: int = 20, columns: list[str] | None = None,
          aggregate: dict[str, Any] | None = None) -> dict[str, Any]:
    t = TABLE_ALIASES.get(str(table).lower())
    if t is None:
        return {"success": False, "error": "table must be 'nodes' or 'links'."}
    df = node_table(session) if t == "nodes" else link_table(session)
    filters = list(filters or [])
    # Convenience: 'pipes'/'junctions' imply a type filter.
    implied = {"pipes": "Pipe", "pipe": "Pipe", "junctions": "Junction", "junction": "Junction"}.get(str(table).lower())
    if implied and not any(f.get("column") == "type" for f in filters):
        filters.insert(0, {"column": "type", "op": "=", "value": implied})
    try:
        mask = pd.Series(True, index=df.index)
        for f in filters:
            mask &= _mask(df, f).fillna(False)
    except KeyError as missing:
        sim_hint = "" if session.sim else " Simulation columns need a simulation first (run_simulation)."
        return {"success": False, "error": f"Unknown column {missing}. Available: {', '.join(df.columns)}.{sim_hint}"}
    except (ValueError, TypeError) as error:
        return {"success": False, "error": str(error)}
    sub = df[mask]
    out: dict[str, Any] = {"success": True, "table": t, "filters": filters, "total_matches": int(len(sub)),
                           "total_rows": int(len(df))}
    if session.sim is not None:
        out["simulation"] = {"sim_id": session.sim.id, "note": "Simulation columns are statistics over the whole run."}
    if aggregate:
        func, col = str(aggregate.get("func", "count")).lower(), aggregate.get("column")
        if func not in AGG:
            return {"success": False, "error": f"aggregate.func must be one of {', '.join(sorted(AGG))}."}
        if func == "count":
            value = int(len(sub))
        else:
            if col not in df.columns:
                return {"success": False, "error": f"Unknown aggregate column '{col}'."}
            series = pd.to_numeric(sub[col], errors="coerce").dropna()
            value = None if series.empty else float(getattr(series, func)())
            if func in ("min", "max") and not series.empty:
                out["aggregate_element"] = str(sub.loc[series.idxmin() if func == "min" else series.idxmax(), "id"])
        out["aggregate"] = {"func": func, "column": col, "value": value}
    if sort_by:
        if sort_by not in df.columns:
            return {"success": False, "error": f"Unknown sort column '{sort_by}'."}
        sub = sub.sort_values(sort_by, ascending=not descending, na_position="last")
    limit = max(1, min(int(limit or 20), 500))
    keep = ["id", "type"] + [f["column"] for f in filters if f.get("column") not in ("id", "type")]
    if sort_by:
        keep.append(sort_by)
    if aggregate and aggregate.get("column"):
        keep.append(aggregate["column"])
    keep += list(columns or [])
    keep = [c for i, c in enumerate(keep) if c in df.columns and c not in keep[:i]]
    rows = sub[keep].head(limit).replace({np.nan: None})
    out["columns"] = keep
    out["rows"] = [{k: (round(v, 4) if isinstance(v, float) else (bool(v) if isinstance(v, (np.bool_,)) else v))
                    for k, v in r.items()} for r in rows.to_dict("records")]
    out["matched_ids"] = sub["id"].astype(str).tolist()[:2000]
    return out
