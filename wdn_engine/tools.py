"""Typed tool registry shared by the LLM assistant, the rule-based router and the API.

Every tool returns ``(result, actions)``: ``result`` is a JSON-safe dict of
verified numbers; ``actions`` tell the UI what to show (highlight, colour, select).
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any, Callable

from . import topology as T
from .criteria import DesignCriteria
from .hydraulics import SimSettings
from .session import Session
from .validation import validate

Action = dict[str, Any]

# Live progress hook: background chat/analysis jobs set this so long tools can report their steps.
PROGRESS: contextvars.ContextVar[Callable[[float, str], None]] = contextvars.ContextVar("progress", default=lambda p, m: None)
ToolOut = tuple[dict[str, Any], list[Action]]

COLOR_ATTRIBUTES = {
    # static
    "diameter": "link", "length": "link", "roughness": "link",
    "elevation": "node", "base_demand": "node",
    # simulation
    "pressure": "node", "head": "node", "demand": "node", "age": "node",
    "flow": "link", "velocity": "link", "headloss": "link",
}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., ToolOut]

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


def _obj(props: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props or {}, "required": required or []}


def _highlight(nodes=None, links=None, label="") -> Action:
    return {"type": "highlight", "nodes": list(nodes or []), "links": list(links or []), "label": label}


# ------------------------------------------------------------------ tools
def network_summary(s: Session) -> ToolOut:
    return {"success": True, **s.model.summary()}, []


def validation_report(s: Session) -> ToolOut:
    rep = validate(s.model)
    return rep, []


def pipe_statistics(s: Session) -> ToolOut:
    return T.pipe_statistics(s.model), [{"type": "color", "attribute": "diameter"}]


def filter_pipes(s: Session, conditions: list[dict[str, Any]]) -> ToolOut:
    conds = []
    for c in conditions or []:
        field = {"diameter": "diameter_mm", "length": "length_m"}.get(c.get("field"), c.get("field"))
        conds.append(T.PipeCondition(str(field), str(c.get("op", "")).strip(), float(c.get("value"))))
    res = T.filter_pipes(s.model, conds)
    if not res.get("success"):
        return res, []
    return res, [_highlight(links=[p["id"] for p in res["pipes"]], label=" and ".join(res["conditions"]))]


def element_info(s: Session, kind: str, id: str) -> ToolOut:  # noqa: A002 - schema name
    kind = "link" if str(kind).lower() in {"link", "pipe", "pump", "valve"} else "node"
    res = T.node_info(s.model, id) if kind == "node" else T.link_info(s.model, id)
    if not res.get("success"):
        # The user may have named the wrong kind.
        alt = T.link_info(s.model, id) if kind == "node" else T.node_info(s.model, id)
        if alt.get("success"):
            res, kind = alt, ("link" if kind == "node" else "node")
        else:
            return res, []
    sim = s.sim
    if sim:
        eid = res["id"]
        if kind == "node":
            p = sim.frame("pressure", stat="min")["values"].get(eid)
            pmax = sim.frame("pressure", stat="max")["values"].get(eid)
            res["simulated"] = {"min_pressure_m": p, "max_pressure_m": pmax, "sim_id": sim.id}
        else:
            res["simulated"] = {"max_abs_flow_lps": sim.frame("flow", stat="absmax")["values"].get(eid),
                                "max_abs_velocity_ms": sim.frame("velocity", stat="absmax")["values"].get(eid),
                                "sim_id": sim.id}
    return res, [{"type": "select", "kind": kind, "id": res["id"]}]


def topology_characterization(s: Session, focus: str = "overall") -> ToolOut:
    res = dict(T.characterization(s.model))
    focus = (focus or "overall").lower()
    res["focus"] = focus
    actions: list[Action] = []
    if focus in {"dead_ends", "dead ends"}:
        actions.append(_highlight(nodes=res["dead_end_junctions"], label="Dead-end junctions"))
    elif focus in {"bridges", "critical_links"}:
        actions.append(_highlight(links=[b["link_id"] for b in res["bridges"]], label="Single-link bridges"))
    elif focus in {"articulation_points", "articulation"}:
        actions.append(_highlight(nodes=res["articulation_points"], label="Articulation points"))
    elif focus in {"unsupplied", "connectivity"}:
        actions.append(_highlight(nodes=res["source_connectivity"]["unsupplied_junctions"], label="No path to a source"))
    return res, actions


def node_metric(s: Session, metric: str, weighted: bool = False) -> ToolOut:
    res = T.node_metric(s.model, metric, weighted)
    if not res.get("success"):
        return res, []
    return res, [{"type": "color", "metric": res["metric"], "target": "node", "weighted": weighted},
                 _highlight(nodes=[r["id"] for r in res["top"][:10]], label=f"Top 10 {res['metric']}")]


def link_metric(s: Session, metric: str, weighted: bool = False) -> ToolOut:
    res = T.link_metric(s.model, metric, weighted)
    if not res.get("success"):
        return res, []
    return res, [{"type": "color", "metric": res["metric"], "target": "link", "weighted": weighted},
                 _highlight(links=[r["id"] for r in res["top"][:10]], label=f"Top 10 {res['metric']}")]


def shortest_path(s: Session, source: str, target: str, weighted: bool = True) -> ToolOut:
    res = T.shortest_path(s.model, source, target, weighted)
    if not res.get("success"):
        return res, []
    return res, [_highlight(nodes=res["path_nodes"], links=res["path_links"], label=f"Path {res['source']} → {res['target']}")]


def distance_statistics(s: Session, weighted: bool = False) -> ToolOut:
    return T.distance_statistics(s.model, weighted), []


def run_simulation(s: Session, duration_h: float | None = None, demand_model: str = "DD",
                   demand_multiplier: float = 1.0, closed_links: list[str] | None = None,
                   water_age: bool = False, required_pressure_m: float = 20.0) -> ToolOut:
    settings = SimSettings.from_dict({"duration_h": duration_h, "demand_model": demand_model,
                                      "demand_multiplier": demand_multiplier, "closed_links": closed_links or [],
                                      "water_age": water_age, "required_pressure_m": required_pressure_m})
    sim = s.simulate(settings)
    actions: list[Action] = [{"type": "simulation", "sim_id": sim.id},
                             {"type": "color", "attribute": "pressure", "stat": "min"}]
    if closed_links:
        actions.append(_highlight(links=[s.model.resolve_link(l) for l in closed_links], label="Closed in scenario"))
    return {"success": True, **sim.summary}, actions


def simulation_results(s: Session) -> ToolOut:
    notes = _ensure_sim(s)
    actions = [{"type": "simulation", "sim_id": s.sim.id}] if notes else []
    out = {"success": True, **s.sim.summary}
    out["notes"] = notes + list(out.get("notes", []))
    return out, actions


def compare_runs(s: Session, variable: str = "pressure", stat: str = "min", base_sim_id: str | None = None,
                 scenario_sim_id: str | None = None) -> ToolOut:
    from .hydraulics import compare
    if not s.sim or len(s.sims) < 2:
        return {"success": False, "error": "Run at least two simulations (e.g. a baseline and a scenario) to compare."}, []
    if scenario_sim_id:
        if scenario_sim_id not in s.sims:
            return {"success": False, "error": f"Run '{scenario_sim_id}' is not available any more."}, []
        s.active_sim_id = scenario_sim_id
    ids = list(s.sims)
    if base_sim_id:
        base = s.sims.get(base_sim_id)
    else:
        i = ids.index(s.active_sim_id)
        base = s.sims[ids[i - 1]] if i > 0 else s.sims[ids[-1]]
    if base is None or base.id == s.sim.id:
        return {"success": False, "error": "Choose a different base run to compare with."}, []
    res = compare(base, s.sim, variable, None if stat in (None, "", "none") else stat)
    return res, [{"type": "compare", "base": base.id, "scenario": s.sim.id, "var": variable, "stat": stat},
                 _highlight(nodes=res["lost_supply"] if res["kind"] == "node" else [],
                            links=res["lost_supply"] if res["kind"] == "link" else [], label="Lost supply")
                 if res["lost_supply_count"] else {"type": "noop"}]


RANK_VARS = {"pressure": "node", "head": "node", "demand": "node", "age": "node", "chlorine": "node", "trace": "node", "leak": "node",
             "flow": "link", "velocity": "link", "headloss": "link"}


def _ensure_sim(s: Session, need_age: bool = False) -> list[str]:
    """Run a default simulation when none exists (or water age is needed but missing)."""
    notes = []
    if s.sim is None or (need_age and "age" not in s.sim.node_data):
        file_h = s.model.wn.options.time.duration / 3600.0
        dur = max(file_h, 72.0) if need_age else (file_h if file_h > 0 else 24.0)
        s.simulate(SimSettings(duration_h=dur, water_age=need_age))
        notes.append(f"No suitable simulation existed, so I ran one ({dur:g} h, demand-driven"
                     + (", with water age" if need_age else "") + ").")
    return notes


def rank_elements(s: Session, variable: str, order: str = "lowest", n: int = 10, stat: str | None = None) -> ToolOut:
    variable = variable.lower().replace("water ", "")
    if variable not in RANK_VARS:
        return {"success": False, "error": f"variable must be one of {', '.join(RANK_VARS)}"}, []
    notes = _ensure_sim(s, need_age=variable == "age")
    sim = s.sim
    kind = RANK_VARS[variable]
    single = len(sim.times_h) <= 1
    if stat in (None, "", "auto"):
        if variable in ("flow", "velocity", "headloss"):
            stat = "absmax"
        else:
            stat = "min" if order == "lowest" else "max"
    frame = sim.frame(variable, 0 if single else None, None if single else stat)
    model = s.model
    rows = []
    for eid, v in frame["values"].items():
        if v is None:
            continue
        el = model.wn.get_node(eid) if kind == "node" else model.wn.get_link(eid)
        if kind == "node" and el.node_type != "Junction":
            continue
        if kind == "link" and el.link_type != "Pipe":
            continue
        rows.append({"id": eid, "value": v})
    rows.sort(key=lambda r: r["value"], reverse=(order != "lowest"))
    n = max(1, min(int(n or 10), 100))
    top = rows[:n]
    res = {"success": True, "variable": variable, "unit": frame["unit"], "order": order,
           "statistic": "single time step" if single else frame["label"], "sim_id": sim.id,
           "elements": top, "count_considered": len(rows), "notes": notes,
           "duration_h": round(float(sim.times_h[-1]), 3) if len(sim.times_h) else 0.0}
    actions = [{"type": "simulation", "sim_id": sim.id}] if notes else []
    actions += [{"type": "color", "attribute": variable, "stat": "none" if single else stat},
                _highlight(nodes=[r["id"] for r in top] if kind == "node" else [],
                           links=[r["id"] for r in top] if kind == "link" else [],
                           label=f"{order} {variable}")]
    return res, actions


# ----------------------------------------------------- query, time, space
def query_table(s: Session, table: str, filters: list[dict[str, Any]] | None = None, sort_by: str | None = None,
                descending: bool = True, limit: int = 20, columns: list[str] | None = None,
                aggregate: dict[str, Any] | None = None) -> ToolOut:
    from .tables import query
    sim_cols = {"pressure", "head", "demand_lps", "age", "flow", "velocity", "headloss", "reverses", "cut_off", "hours_closed"}
    used = [f.get("column", "") for f in (filters or [])] + [sort_by or "", (aggregate or {}).get("column") or ""]
    notes = _ensure_sim(s) if any(any(k in c for k in sim_cols) for c in used if c and c != "base_demand_lps") else []
    res = query(s, table, filters, sort_by, descending, limit, columns, aggregate)
    if not res.get("success"):
        return res, []
    res["notes"] = notes
    kind = res["table"]
    ids = res.pop("matched_ids")
    actions: list[Action] = [{"type": "simulation", "sim_id": s.sim.id}] if notes else []
    if ids and len(ids) < res["total_rows"]:
        actions.append(_highlight(nodes=ids if kind == "nodes" else [], links=ids if kind == "links" else [],
                                  label=f"Query: {res['total_matches']} {kind}"))
    return res, actions


def value_at_time(s: Session, element_id: str, time: Any, variable: str | None = None) -> ToolOut:
    from .temporal import value_at
    notes = _ensure_sim(s)
    res = value_at(s, element_id, time, variable)
    if not res.get("success"):
        return res, []
    res["notes"] = notes
    return res, ([{"type": "simulation", "sim_id": s.sim.id}] if notes else []) + [
        {"type": "time", "index": res["time_index"]}, {"type": "select", "kind": res["kind"], "id": res["element"]}]


def when_extreme(s: Session, variable: str, extreme: str = "max", element_id: str | None = None) -> ToolOut:
    from .temporal import when_extreme as we
    notes = _ensure_sim(s)
    if s.sim and len(s.sim.times_h) < 2:
        notes += _rerun_extended(s)
    res = we(s, variable, extreme, element_id)
    if not res.get("success"):
        return res, []
    res["notes"] = notes
    acts: list[Action] = ([{"type": "simulation", "sim_id": s.sim.id}] if notes else []) + [{"type": "time", "index": res["time_index"]}]
    if res.get("element"):
        acts.append({"type": "select", "kind": "node" if res["variable"] in ("pressure", "head", "age") else "link", "id": res["element"]})
    elif res["scope"] != "network":
        acts.append({"type": "select", "kind": "node" if s.model.resolve_node(res["scope"]) else "link", "id": res["scope"]})
    return res, acts


def _rerun_extended(s: Session) -> list[str]:
    file_h = s.model.wn.options.time.duration / 3600.0
    s.simulate(SimSettings(duration_h=file_h if file_h > 0 else 24.0))
    return [f"The shown run was steady-state, so I ran a {file_h if file_h > 0 else 24:g} h extended-period simulation."]


def tank_events(s: Session, tank_id: str | None = None) -> ToolOut:
    from .temporal import tank_events as te
    notes = _ensure_sim(s)
    res = te(s, tank_id)
    if res.get("success"):
        res["notes"] = notes
    acts = [{"type": "simulation", "sim_id": s.sim.id}] if notes else []
    if res.get("success") and res["tanks"]:
        acts.append({"type": "select", "kind": "node", "id": res["tanks"][0]["id"]})
    return res, acts


def nodes_within(s: Session, center: str, distance_m: float, method: str = "network") -> ToolOut:
    from .spatial import nodes_within as nw
    res = nw(s, center, distance_m, method)
    if not res.get("success"):
        return res, []
    ids = res.pop("node_ids")
    return res, [_highlight(nodes=ids + [res["center"]], label=f"Within {res['distance_m']:g} m of {res['center']}")]


def trace_flow(s: Session, element: str, direction: str = "downstream", use_current_time: bool = False,
               time_index: int | None = None) -> ToolOut:
    from .spatial import trace_flow as tf
    notes = _ensure_sim(s)
    res = tf(s, element, direction, time_index if use_current_time or time_index is not None else None)
    if not res.get("success"):
        return res, []
    res["notes"] = notes
    nodes, links = res.pop("node_ids"), res.pop("link_ids")
    res["sample_nodes"] = nodes[:30]
    acts = [{"type": "simulation", "sim_id": s.sim.id}] if notes else []
    acts.append(_highlight(nodes=nodes, links=links, label=f"{res['direction'].capitalize()} of {res['element']}"))
    return res, acts


# ---------------------------------------------------------- engineering analyses
def _layer_actions(s: Session, name: str, top_ids: list[str], kind: str, label: str) -> list[Action]:
    acts: list[Action] = [{"type": "layer", "name": name}]
    if top_ids:
        acts.append(_highlight(nodes=top_ids if kind == "node" else [], links=top_ids if kind == "link" else [], label=label))
    return acts


def pipe_criticality(s: Session, required_pressure_m: float | None = None, include: str = "pipes",
                     time_h: float | None = None, top_n: int = 10) -> ToolOut:
    from .resilience import pipe_criticality as pc
    res = pc(s, required_pressure_m, include, time_h)
    if not res.get("success"):
        return res, []
    res.pop("all", None)
    top = [r["id"] for r in res["top"][:max(1, min(int(top_n or 10), 25))] if r.get("demand_shortfall_lps", 0) > 0 or r.get("max_pressure_drop_m", 0) > 0.5]
    return res, _layer_actions(s, "criticality", top, "link", f"Top {len(top)} critical links")


def valve_segments(s: Session, placement: str = "strategic", n: int = 2, hydraulic: bool = True) -> ToolOut:
    from .resilience import valve_segments as vs
    res = vs(s, placement, n, hydraulic=hydraulic)
    if not res.get("success"):
        return res, []
    res.pop("all", None)
    worst = s.segment_rows.get(res["top"][0]["segment"]) if res["top"] else None
    acts: list[Action] = [{"type": "layer", "name": "segments"}]
    if worst:
        acts.append(_highlight(nodes=worst["_nodes"], links=worst["_links"], label=f"Most critical segment #{worst['segment']}"))
    return res, acts


def fire_flow(s: Session, node: str | None = None, residual_pressure_m: float = 14.0, required_flow_lps: float | None = None,
              all_nodes: bool = False, system_min_pressure_m: float | None = None) -> ToolOut:
    from .resilience import fire_flow as ff
    res = ff(s, node, residual_pressure_m, required_flow_lps, all_nodes=all_nodes, system_min_pressure=system_min_pressure_m)
    if not res.get("success"):
        return res, []
    if all_nodes:
        res.pop("all", None)
        return res, _layer_actions(s, "fire_flow", [r["id"] for r in res["weakest"][:10]], "node", "Weakest fire-flow nodes")
    return res, [{"type": "select", "kind": "node", "id": res["id"]}]


def demand_growth(s: Session, min_pressure_m: float | None = None, verify_eps: bool = True) -> ToolOut:
    from .resilience import demand_growth as dg
    res = dg(s, min_pressure_m, verify_eps=verify_eps)
    acts: list[Action] = []
    if res.get("first_failing_nodes"):
        acts.append(_highlight(nodes=res["first_failing_nodes"], label="First nodes to fail with growth"))
    return res, acts


def leak_scenario(s: Session, elements: list[str], diameter_mm: float = 10.0, duration_h: float | None = None,
                  demand_model: str = "PDD") -> ToolOut:
    from .hydraulics import compare
    file_h = s.model.wn.options.time.duration / 3600.0
    dur = float(duration_h) if duration_h else (min(file_h, 168.0) if file_h >= 24 else 24.0)
    base = None
    for sim in reversed(list(s.sims.values())):
        st = sim.settings
        if (not st.leaks and not st.closed_links and st.demand_multiplier == 1 and st.demand_model == demand_model
                and abs(float(sim.times_h[-1]) - dur) < 1e-6):
            base = sim
            break
    notes = []
    if base is None:
        base = s.simulate(SimSettings(duration_h=dur, demand_model=demand_model, label="baseline for leak"))
        notes.append(f"Ran a {dur:g} h {demand_model} baseline (no leak) to compare against.")
    leak = s.simulate(SimSettings(duration_h=dur, demand_model=demand_model,
                                  leaks=[{"element": e, "diameter_mm": float(diameter_mm)} for e in elements],
                                  label="leak " + ",".join(elements)))
    cmp = compare(base, leak, "pressure", "mean")
    res = {"success": True, "analysis": "leak_scenario", "elements": elements, "diameter_mm": float(diameter_mm),
           "duration_h": dur, "demand_model": demand_model, "base_sim_id": base.id, "leak_sim_id": leak.id,
           "leakage": leak.summary.get("leakage"), "pressure_change": {k: cmp[k] for k in (
               "changed_count", "max_decrease", "mean_abs_change", "tolerance")},
           "sensor_hint": [k for k, v in sorted(((k, v) for k, v in cmp["values"].items() if v is not None), key=lambda kv: kv[1])[:10]],
           "notes": notes + [n for n in leak.summary.get("notes", []) if n.startswith("Leaks")]}
    acts: list[Action] = [{"type": "simulation", "sim_id": leak.id},
                          {"type": "compare", "base": base.id, "scenario": leak.id, "var": "pressure", "stat": "mean"},
                          _highlight(nodes=(leak.summary.get("leakage") or {}).get("nodes", []), label="Leak location")]
    return res, acts


def water_quality(s: Session, mode: str = "chlorine", duration_h: float | None = None, chlorine_mgl: float = 1.0,
                  bulk_coeff_per_day: float = -0.5, wall_coeff_m_per_day: float = 0.0, source: str | None = None) -> ToolOut:
    file_h = s.model.wn.options.time.duration / 3600.0
    dur = float(duration_h) if duration_h else max(72.0, min(file_h, 168.0))
    mode = mode.lower()
    if mode in ("source", "sources", "source_shares", "share", "shares"):
        from .resilience import source_shares
        res = source_shares(s, dur)
        if res.get("success"):
            first = res["layers"][0]
            return res, [{"type": "layer", "name": first}]
        return res, []
    if mode not in ("chlorine", "age", "trace"):
        return {"success": False, "error": "mode must be chlorine, age, trace or sources."}, []
    sim = s.simulate(SimSettings(duration_h=dur, quality=mode, chlorine_mgl=chlorine_mgl, bulk_coeff_per_day=bulk_coeff_per_day,
                                 wall_coeff_m_per_day=wall_coeff_m_per_day, trace_node=source, label=f"quality {mode}"))
    out = {"success": True, "analysis": "water_quality", "mode": mode, **sim.summary}
    stat = "min" if mode == "chlorine" else "max" if mode == "age" else "mean"
    return out, [{"type": "simulation", "sim_id": sim.id}, {"type": "color", "attribute": mode, "stat": stat}]


# ----------------------------------------------------------- reasoning tools
def what_if(s: Session, closed_links: list[str] | None = None, demand_multiplier: float = 1.0,
            leaks: list[dict[str, Any]] | None = None, duration_h: float | None = None, demand_model: str = "PDD",
            required_pressure_m: float | None = None) -> ToolOut:
    from .insight import what_if as wi
    res = wi(s, closed_links, float(demand_multiplier or 1.0), leaks, duration_h, demand_model, required_pressure_m, PROGRESS.get())
    if not res.get("success"):
        return res, []
    acts: list[Action] = [{"type": "simulation", "sim_id": res["scenario_sim_id"]},
                          {"type": "compare", "base": res["base_sim_id"], "scenario": res["scenario_sim_id"], "var": "pressure", "stat": "min"}]
    worst = [w["id"] for w in res["worst_junctions"]] + res["cut_off_ids"][:100]
    if worst:
        acts.append(_highlight(nodes=worst, label="Most affected junctions"))
    return res, acts


def diagnose_pressure(s: Session, node: str, time: str | None = None) -> ToolOut:
    from .insight import diagnose_pressure as dp
    res = dp(s, node, time)
    if not res.get("success"):
        return res, []
    acts: list[Action] = [_highlight(nodes=res["path_nodes"], links=res["path_links"], label=f"Supply path to {res['node']}"),
                          {"type": "time", "index": res["time_index"]}, {"type": "select", "kind": "node", "id": res["node"]}]
    return res, acts


def assess_network(s: Session) -> ToolOut:
    from .insight import assess_network as an
    res = an(s, PROGRESS.get())
    return res, []


def explain_term(s: Session, term: str) -> ToolOut:
    from .glossary import lookup
    hit = lookup(term)
    if not hit:
        return {"success": False, "error": f"No built-in definition for '{term}'."}, []
    key, entry = hit
    return {"success": True, "term": key, **entry}, []


def edit_element(s: Session, kind: str, id: str, changes: dict[str, Any]) -> ToolOut:  # noqa: A002
    from .edits import apply_edit
    res = apply_edit(s, kind, id, changes or {})
    return res, ([{"type": "edited"}, {"type": "select", "kind": "link" if kind in ("link", "pipe", "pump", "valve") else "node", "id": id}]
                 if res.get("success") else [])


def undo_edit(s: Session) -> ToolOut:
    from .edits import undo_edit as ue
    res = ue(s)
    return res, ([{"type": "edited"}] if res.get("success") else [])


CHECKS = {"low_pressure": "nodes", "high_pressure": "nodes", "high_velocity": "links",
          "low_velocity": "links", "high_headloss": "links"}


def criteria_violations(s: Session, check: str) -> ToolOut:
    if check not in CHECKS:
        return {"success": False, "error": f"check must be one of {', '.join(CHECKS)}"}, []
    if not s.criteria.is_set():
        return {"success": False, "error": "No design criteria are set. Ask the user for limits or call set_design_criteria."}, []
    if not s.sim:
        return {"success": False, "error": "Run a simulation first."}, []
    checks = s.sim.evaluate_criteria(s.model, s.criteria)
    if check not in checks:
        return {"success": False, "error": f"The limit for '{check}' is not set in the current criteria."}, []
    res = {"success": True, "check": check, **checks[check]}
    ids = res.get(CHECKS[check], [])
    act = _highlight(nodes=ids if CHECKS[check] == "nodes" else [], links=ids if CHECKS[check] == "links" else [],
                     label=check.replace("_", " "))
    return res, [act]


def set_design_criteria(s: Session, min_pressure_m: float | None = None, max_pressure_m: float | None = None,
                        min_velocity_ms: float | None = None, max_velocity_ms: float | None = None,
                        max_headloss_m_per_km: float | None = None) -> ToolOut:
    data = s.criteria.to_dict()
    for key, val in dict(min_pressure_m=min_pressure_m, max_pressure_m=max_pressure_m, min_velocity_ms=min_velocity_ms,
                         max_velocity_ms=max_velocity_ms, max_headloss_m_per_km=max_headloss_m_per_km).items():
        if val is not None:
            data[key] = val
    data["label"] = "Custom"
    s.set_criteria(DesignCriteria.from_dict(data))
    return {"success": True, "criteria": s.criteria.to_dict()}, [{"type": "criteria"}]


def set_map_colour(s: Session, attribute: str, stat: str = "none") -> ToolOut:
    attribute = attribute.lower()
    if attribute not in COLOR_ATTRIBUTES:
        return {"success": False, "error": f"attribute must be one of {', '.join(COLOR_ATTRIBUTES)}"}, []
    return {"success": True, "attribute": attribute}, [{"type": "color", "attribute": attribute, "stat": stat}]


REGISTRY: dict[str, Tool] = {t.name: t for t in [
    Tool("network_summary", "Counts of nodes/links by type, total pipe length, total base demand (L/s), units, headloss formula, duration.", _obj(), network_summary),
    Tool("validation_report", "Model health checks: bad pipe data, disconnected or unsupplied nodes, closed links, roughness outliers, elevation issues.", _obj(), validation_report),
    Tool("pipe_statistics", "Distribution of pipe length (m), diameter (mm) and roughness; length by diameter class.", _obj(), pipe_statistics),
    Tool("filter_pipes", "Find pipes matching ALL conditions. Units: diameter_mm in millimetres, length_m in metres, roughness raw. Convert user units first (1 inch = 25.4 mm).",
         _obj({"conditions": {"type": "array", "items": _obj({
             "field": {"type": "string", "enum": ["diameter_mm", "length_m", "roughness"]},
             "op": {"type": "string", "enum": ["<", "<=", ">", ">=", "=", "!="]},
             "value": {"type": "number"}}, ["field", "op", "value"])}}, ["conditions"]), filter_pipes),
    Tool("element_info", "Properties of one node or link by ID (plus simulated min/max values if a simulation exists).",
         _obj({"kind": {"type": "string", "enum": ["node", "link"]}, "id": {"type": "string"}}, ["kind", "id"]), element_info),
    Tool("topology_characterization", "Graph characterization: components, loops, meshedness, dead ends, bridges, articulation points, source connectivity. Use focus to highlight a subset on the map.",
         _obj({"focus": {"type": "string", "enum": ["overall", "dead_ends", "bridges", "articulation_points", "unsupplied"]}}), topology_characterization),
    Tool("node_metric", "Compute a node metric for every node, colour the map by it and return the top nodes.",
         _obj({"metric": {"type": "string", "enum": list(T.NODE_METRICS)}, "weighted": {"type": "boolean", "description": "Use pipe length"}}, ["metric"]), node_metric),
    Tool("link_metric", "Compute a link metric (edge betweenness or bridge flag), colour links by it.",
         _obj({"metric": {"type": "string", "enum": list(T.LINK_METRICS)}, "weighted": {"type": "boolean"}}, ["metric"]), link_metric),
    Tool("shortest_path", "Shortest open path between two nodes (by length or hops) and highlight it.",
         _obj({"source": {"type": "string"}, "target": {"type": "string"}, "weighted": {"type": "boolean", "description": "true=by length, false=by hops"}}, ["source", "target"]), shortest_path),
    Tool("distance_statistics", "Graph diameter and average shortest path (hops or metres).",
         _obj({"weighted": {"type": "boolean"}}), distance_statistics),
    Tool("run_simulation", "Run an EPANET hydraulic simulation (optionally a scenario) and return KPIs: pressures, velocities, tanks, pumps, criteria checks. duration_h omitted = file duration.",
         _obj({"duration_h": {"type": "number"}, "demand_model": {"type": "string", "enum": ["DD", "PDD"]},
               "demand_multiplier": {"type": "number"}, "closed_links": {"type": "array", "items": {"type": "string"}},
               "water_age": {"type": "boolean"}, "required_pressure_m": {"type": "number", "description": "PDD only"}}), run_simulation),
    Tool("simulation_results", "KPIs of the most recent simulation (runs a default one if none exists): pressure extremes, velocities, tanks, pumps, criteria.", _obj(), simulation_results),
    Tool("rank_elements", "List the junctions/pipes with the lowest or highest simulated value (pressure, head, demand, water age, flow, velocity, headloss), highlight them and colour the map. Runs a simulation automatically if needed. Flow/velocity/headloss use the largest absolute value over the period.",
         _obj({"variable": {"type": "string", "enum": list(RANK_VARS)}, "order": {"type": "string", "enum": ["lowest", "highest"]},
               "n": {"type": "integer"}, "stat": {"type": "string", "enum": ["auto", "min", "max", "mean", "absmax"]}}, ["variable"]), rank_elements),
    Tool("compare_runs", "Compare two simulations (default: shown run vs the previous run): change per element, biggest drops/increases, elements that lost supply. Colours the map by the change. Run IDs and their #numbers are listed under RUNS in the context.",
         _obj({"variable": {"type": "string", "enum": ["pressure", "head", "demand", "flow", "velocity", "headloss"]},
               "stat": {"type": "string", "enum": ["none", "min", "max", "mean", "absmax"]},
               "base_sim_id": {"type": "string"}, "scenario_sim_id": {"type": "string"}}), compare_runs),
    Tool("query_table", "Filter/sort/count/aggregate element tables for combined questions (e.g. pipes < 100 mm with max velocity > 0.5 m/s). "
         "Table 'nodes' (or 'junctions') columns: " + __import__("wdn_engine.tables", fromlist=["x"]).NODE_COLUMNS_DOC + ". Table 'links' (or 'pipes') columns: " + __import__("wdn_engine.tables", fromlist=["x"]).LINK_COLUMNS_DOC +
         ". Simulation columns = run statistics (auto-runs a simulation). Filters ANDed; ops < <= > >= = != between in contains is_true is_false.",
         _obj({"table": {"type": "string", "enum": ["nodes", "junctions", "links", "pipes"]},
               "filters": {"type": "array", "items": _obj({"column": {"type": "string"}, "op": {"type": "string"},
                                                           "value": {"description": "number, string, boolean or [lo, hi]"}}, ["column", "op"])},
               "sort_by": {"type": "string"}, "descending": {"type": "boolean"}, "limit": {"type": "integer"},
               "columns": {"type": "array", "items": {"type": "string"}, "description": "extra columns to return"},
               "aggregate": _obj({"func": {"type": "string", "enum": ["count", "sum", "mean", "median", "min", "max"]},
                                  "column": {"type": "string"}}, ["func"])}, ["table"]), query_table),
    Tool("value_at_time", "Simulated value(s) of one node/link at a given time. time: hours from start (8.5), clock ('08:00', '8 am') or 'day 2 08:00'. variable optional (pressure, head, demand, age, flow, velocity, headloss).",
         _obj({"element_id": {"type": "string"}, "time": {"type": "string"}, "variable": {"type": "string"}}, ["element_id", "time"]), value_at_time),
    Tool("when_extreme", "WHEN a variable is highest/lowest — for the whole network (total demand, worst pressure, fastest pipe) or for one element (element_id), incl. tank_level. Returns time and clock.",
         _obj({"variable": {"type": "string", "enum": ["demand", "pressure", "head", "age", "flow", "velocity", "headloss", "tank_level"]},
               "extreme": {"type": "string", "enum": ["max", "min"]}, "element_id": {"type": "string"}}, ["variable"]), when_extreme),
    Tool("tank_events", "Tank behaviour over the run: min/max level and when, whether/when it empties or fills, net change, fill/drain cycles.",
         _obj({"tank_id": {"type": "string"}}), tank_events),
    Tool("nodes_within", "Nodes within a distance (m) of a node — along pipes ('network', default) or straight line ('straight').",
         _obj({"center": {"type": "string"}, "distance_m": {"type": "number"}, "method": {"type": "string", "enum": ["network", "straight"]}}, ["center", "distance_m"]), nodes_within),
    Tool("trace_flow", "Everything downstream (supplied by) or upstream (supplying) of a node/pipe, following simulated flow directions (mean over the run, or the current map time if use_current_time). Gives node/pipe counts, base demand served and sources reached.",
         _obj({"element": {"type": "string"}, "direction": {"type": "string", "enum": ["downstream", "upstream"]},
               "use_current_time": {"type": "boolean"}, "time_index": {"type": "integer"}}, ["element"]), trace_flow),
    Tool("pipe_criticality", "N-1 criticality: closes every pipe (or every link with include='all') one at a time at the peak-demand hour (pressure-driven) and ranks them by demand that can no longer be delivered, junctions pushed below the required pressure and the largest pressure drop. Colours pipes by impact. Takes a few seconds.",
         _obj({"required_pressure_m": {"type": "number"}, "include": {"type": "string", "enum": ["pipes", "all"]},
               "time_h": {"type": "number", "description": "snapshot time; default = hour of peak demand"}, "top_n": {"type": "integer"}}), pipe_criticality),
    Tool("valve_segments", "Isolation segments for an ASSUMED valve layout (placement 'strategic' with n valves per junction, or 'random' with n valves) and the impact of isolating each segment (junctions without water, demand lost).",
         _obj({"placement": {"type": "string", "enum": ["strategic", "random"]}, "n": {"type": "integer"}, "hydraulic": {"type": "boolean"}}), valve_segments),
    Tool("fire_flow", "Available fire flow (L/s) at a junction while keeping residual_pressure_m (default 14 m) there; optionally check a required flow, or map all junctions (all_nodes=true, slower).",
         _obj({"node": {"type": "string"}, "residual_pressure_m": {"type": "number"}, "required_flow_lps": {"type": "number"},
               "all_nodes": {"type": "boolean"}, "system_min_pressure_m": {"type": "number"}}), fire_flow),
    Tool("demand_growth", "How much can demand grow before the minimum pressure limit is breached? Returns the capacity multiplier, first failing nodes and a pressure-vs-growth curve; verified with a full run.",
         _obj({"min_pressure_m": {"type": "number"}, "verify_eps": {"type": "boolean"}}), demand_growth),
    Tool("leak_scenario", "Simulate leak(s) at junctions or pipes (orifice of diameter_mm) and compare with a no-leak baseline: leak flow and volume, pressure signature (largest drops = best sensor locations).",
         _obj({"elements": {"type": "array", "items": {"type": "string"}}, "diameter_mm": {"type": "number"},
               "duration_h": {"type": "number"}, "demand_model": {"type": "string", "enum": ["DD", "PDD"]}}, ["elements"]), leak_scenario),
    Tool("water_quality", "Water-quality run: mode 'chlorine' (residual chlorine with decay), 'age' (water age), 'trace' (share of water from one source) or 'sources' (share from every reservoir/tank).",
         _obj({"mode": {"type": "string", "enum": ["chlorine", "age", "trace", "sources"]}, "duration_h": {"type": "number"},
               "chlorine_mgl": {"type": "number"}, "bulk_coeff_per_day": {"type": "number"}, "wall_coeff_m_per_day": {"type": "number"},
               "source": {"type": "string"}}), water_quality),
    Tool("what_if", "Complete what-if study in one call: runs (or reuses) a baseline, simulates the change (close pipes/pumps/valves, scale demand, add leaks), compares, and reports severity, junctions newly below the required pressure, junctions cut off, supply delivered, worst junctions, tank and pump changes. Prefer this for 'what happens if…' questions.",
         _obj({"closed_links": {"type": "array", "items": {"type": "string"}}, "demand_multiplier": {"type": "number"},
               "leaks": {"type": "array", "items": _obj({"element": {"type": "string"}, "diameter_mm": {"type": "number"}}, ["element"])},
               "duration_h": {"type": "number"}, "demand_model": {"type": "string", "enum": ["PDD", "DD"]},
               "required_pressure_m": {"type": "number"}}), what_if),
    Tool("diagnose_pressure", "Explain WHY pressure is low (or high) at a junction: traces the dominant supply path back to its source at the worst (or given) time and splits the source head into elevation, pipe friction (biggest-loss pipes), valve losses and pump gain; returns likely causes.",
         _obj({"node": {"type": "string"}, "time": {"type": "string", "description": "optional; default = time of lowest pressure"}}, ["node"]), diagnose_pressure),
    Tool("assess_network", "Overall engineering assessment (strengths, weaknesses, next steps) using model check, topology, simulation, N-1 criticality and demand growth with transparent thresholds. Use for 'is this network well designed / what are its problems'.",
         _obj(), assess_network),
    Tool("explain_term", "Built-in definition of a hydraulic/graph term or app option (e.g. PDD, betweenness, Todini, valve segment, snapshot, fire flow). Use instead of your own memory for definitions.",
         _obj({"term": {"type": "string"}}, ["term"]), explain_term),
    Tool("edit_element", "Change the network model (only when the user explicitly asks): pipe diameter_mm/length_m/roughness/minor_loss/status, junction elevation_m/base_demand_lps, tank levels, reservoir base_head_m, valve setting/status, pump status/speed. Re-run a simulation afterwards to see the effect.",
         _obj({"kind": {"type": "string", "enum": ["node", "link"]}, "id": {"type": "string"},
               "changes": {"type": "object", "description": "field -> new value, e.g. {\"diameter_mm\": 150}"}}, ["kind", "id", "changes"]), edit_element),
    Tool("undo_edit", "Undo the most recent model edit.", _obj(), undo_edit),
    Tool("criteria_violations", "List and highlight elements violating the user's design criteria in the latest simulation.",
         _obj({"check": {"type": "string", "enum": list(CHECKS)}}, ["check"]), criteria_violations),
    Tool("set_design_criteria", "Set service limits given by the user (only those mentioned).",
         _obj({k: {"type": "number"} for k in ("min_pressure_m", "max_pressure_m", "min_velocity_ms", "max_velocity_ms", "max_headloss_m_per_km")}), set_design_criteria),
    Tool("set_map_colour", "Colour the map by an attribute. Simulation attributes need a simulation. stat: min|max|mean|absmax|none (none = current time step).",
         _obj({"attribute": {"type": "string", "enum": list(COLOR_ATTRIBUTES)}, "stat": {"type": "string", "enum": ["none", "min", "max", "mean", "absmax"]}}, ["attribute"]), set_map_colour),
]}


REPORTABLE = {"pipe_criticality", "valve_segments", "fire_flow", "demand_growth", "leak_scenario", "water_quality",
              "what_if", "assess_network", "topology_characterization", "diagnose_pressure", "node_metric", "compare_runs"}


def call_tool(session: Session, name: str, args: dict[str, Any] | None) -> ToolOut:
    tool = REGISTRY.get(name)
    if tool is None:
        return {"success": False, "error": f"Unknown tool '{name}'."}, []
    try:
        res, acts = tool.fn(session, **(args or {}))
        if name in REPORTABLE and res.get("success"):
            key = name if name != "water_quality" else f"water_quality_{res.get('mode') or res.get('analysis')}"
            session.results[key] = {k: v for k, v in res.items() if k not in ("values", "all")}
        return res, acts
    except TypeError as error:
        return {"success": False, "error": f"Bad arguments for {name}: {error}"}, []
    except Exception as error:  # surfaced to the user/LLM, never crashes the chat
        return {"success": False, "error": str(error)}, []


def compact(obj: Any, max_list: int = 20, depth: int = 0) -> Any:
    """Shrink a result for an LLM prompt: drop per-element maps, truncate lists."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in {"values"}:
                continue
            out[k] = compact(v, max_list, depth + 1)
        return out
    if isinstance(obj, list):
        items = [compact(v, max_list, depth + 1) for v in obj[:max_list]]
        if len(obj) > max_list:
            items.append(f"... {len(obj) - max_list} more (total {len(obj)})")
        return items
    if isinstance(obj, float):
        return round(obj, 6)
    return obj
