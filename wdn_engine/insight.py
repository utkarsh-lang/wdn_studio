"""Reasoning tools: multi-step what-if, pressure diagnosis, whole-network assessment.

These chain existing calculations deterministically, so small/free LLMs (and the offline rules) get
complete, verified answers in one call. Each step is reported through ``progress`` for live display.
"""

from __future__ import annotations

import warnings
from typing import Any, Callable

import numpy as np

from .hydraulics import SimSettings, compare
from .session import Session

Progress = Callable[[float, str], None]


def _noop(_: float, __: str) -> None:
    pass


def _matching_baseline(session: Session, duration_h: float, demand_model: str):
    for sim in reversed(list(session.sims.values())):
        st = sim.settings
        if (not st.closed_links and not st.leaks and st.demand_multiplier == 1 and st.demand_model == demand_model
                and abs(float(sim.times_h[-1]) - duration_h) < 1e-6 and len(sim.times_h) > 1):
            return sim
    return None


# --------------------------------------------------------------------------- 3.1 what-if
def what_if(session: Session, closed_links: list[str] | None = None, demand_multiplier: float = 1.0,
            leaks: list[dict[str, Any]] | None = None, duration_h: float | None = None, demand_model: str = "PDD",
            required_pressure_m: float | None = None, progress: Progress = _noop) -> dict[str, Any]:
    model = session.model
    closed = []
    for x in closed_links or []:
        lid = model.resolve_link(x)
        if lid is None:
            return {"success": False, "error": f"Unknown link '{x}'. Pumps and valves are links too (e.g. PUMP_1)."}
        closed.append(lid)
    if not closed and demand_multiplier == 1 and not leaks:
        return {"success": False, "error": "Describe a change: links to close (pipe/pump/valve), a demand multiplier or a leak."}
    req = float(required_pressure_m if required_pressure_m is not None else (session.criteria.min_pressure_m or 20.0))
    file_h = model.wn.options.time.duration / 3600.0
    dur = float(duration_h) if duration_h else (min(file_h, 168.0) if file_h >= 24 else 24.0)
    steps: list[str] = []

    progress(0.1, "Step 1/4 · Finding or running the baseline…")
    base = _matching_baseline(session, dur, demand_model)
    if base is None:
        base = session.simulate(SimSettings(duration_h=dur, demand_model=demand_model, required_pressure_m=req,
                                            label="what-if baseline"))
        steps.append(f"Ran a {dur:g} h {demand_model} baseline ({base.id}).")
    else:
        steps.append(f"Reused baseline run {base.id} ({dur:g} h, {demand_model}).")

    change = []
    if closed:
        change.append("closed " + ", ".join(f"{model.wn.get_link(l).link_type.lower()} {l}" for l in closed))
    if demand_multiplier != 1:
        change.append(f"demand ×{demand_multiplier:g}")
    if leaks:
        change.append("leak " + ", ".join(f"{l.get('diameter_mm', 10):g} mm at {l.get('element')}" for l in leaks))
    progress(0.4, f"Step 2/4 · Simulating the scenario ({'; '.join(change)})…")
    scen = session.simulate(SimSettings.from_dict({"duration_h": dur, "demand_model": demand_model, "required_pressure_m": req,
                                                   "closed_links": closed, "demand_multiplier": demand_multiplier,
                                                   "leaks": leaks or [], "label": "what-if: " + "; ".join(change)}))
    steps.append(f"Simulated the scenario ({scen.id}).")

    progress(0.7, "Step 3/4 · Comparing pressures and supply…")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        cmp_min = compare(base, scen, "pressure", "min")
        juncs = list(model.wn.junction_name_list)
        jb = [base.node_index[j] for j in juncs]
        pb, ps = base.node_data["pressure"][:, jb], scen.node_data["pressure"][:, jb]
        n = min(len(pb), len(ps))
        newly_low = (np.nanmin(ps[:n], 0) < req) & ~(np.nanmin(pb[:n], 0) < req)
        cut = scen.isolated[:, jb].any(0) & ~base.isolated[:, jb].any(0)
        drops = sorted(((k, v) for k, v in cmp_min["values"].items()
                        if v is not None and v < -0.01 and model.wn.get_node(k).node_type == "Junction"), key=lambda kv: kv[1])[:10]
        out_supply = None
        if demand_model == "PDD":
            from wntr.metrics import expected_demand  # noqa: F401  (summary already has the ratio)
            out_supply = {"baseline_delivered_pct": round(100 * (base.summary.get("demand_satisfaction_ratio") or 1), 2),
                          "scenario_delivered_pct": round(100 * (scen.summary.get("demand_satisfaction_ratio") or 1), 2)}
            ds = scen.node_data["demand"][:n, jb]
            db = base.node_data["demand"][:n, jb]
            dt = float(np.diff(scen.times_h).mean()) * 3600 if len(scen.times_h) > 1 else 0.0
            out_supply["volume_not_delivered_m3"] = round(float(np.nansum(np.clip(db * demand_multiplier - ds, 0, None))) * dt / 1000, 1) if dt else None
            short_nodes = np.nansum(np.clip(db * demand_multiplier - ds, 0, None), 0) > 1e-3
            out_supply["junctions_with_shortfall"] = int(short_nodes.sum())

    tank_change = []
    for tb, ts in zip(base.summary.get("tanks", []), scen.summary.get("tanks", [])):
        tank_change.append({"id": tb["id"], "baseline_min_m": tb["min_level_m"], "scenario_min_m": ts["min_level_m"],
                            "empties_in_scenario": ts["hit_empty"] and not tb["hit_empty"]})
    pump_change = []
    for pb_, ps_ in zip(base.summary.get("pumps", []), scen.summary.get("pumps", [])):
        pump_change.append({"id": pb_["id"], "baseline_kwh": pb_.get("energy_kwh"), "scenario_kwh": ps_.get("energy_kwh"),
                            "baseline_hours": pb_["hours_running"], "scenario_hours": ps_["hours_running"]})
    progress(0.9, "Step 4/4 · Summarising impacts…")
    severity = "none"
    if cut.any() or newly_low.sum() > 0.05 * len(juncs) or (out_supply and out_supply["scenario_delivered_pct"] < 95):
        severity = "major"
    elif newly_low.any() or (cmp_min["max_decrease"]["value"] or 0) < -5:
        severity = "moderate"
    elif (cmp_min["max_decrease"]["value"] or 0) < -0.5:
        severity = "minor"
    steps.append("Compared minimum pressures, supply, tanks and pumps.")
    return {
        "success": True, "analysis": "what_if", "scenario": "; ".join(change), "severity": severity,
        "base_sim_id": base.id, "scenario_sim_id": scen.id, "duration_h": dur, "demand_model": demand_model,
        "required_pressure_m": req, "steps": steps,
        "junctions_newly_below_required": int(newly_low.sum()),
        "junctions_newly_cut_off": int(cut.sum()), "cut_off_ids": [juncs[i] for i in np.where(cut)[0]][:200],
        "pressure_change_min": {k: cmp_min[k] for k in ("changed_count", "max_decrease", "max_increase", "mean_abs_change")},
        "worst_junctions": [{"id": k, "change_m": v} for k, v in drops],
        "supply": out_supply, "tanks": tank_change, "pumps": pump_change,
        "scenario_notes": [x for x in scen.summary.get("notes", []) if "Closed links" not in x],
    }


# ---------------------------------------------------------------------- 3.2 diagnosis
def diagnose_pressure(session: Session, node: str, time: Any = None) -> dict[str, Any]:
    """Why is pressure (low) at a node? Follow the dominant supply path back to a source and
    split the source head into elevation, pipe friction, valve and pump contributions."""
    model = session.model
    nid = model.resolve_node(node)
    if nid is None or model.wn.get_node(nid).node_type != "Junction":
        return {"success": False, "error": f"'{node}' is not a junction."}
    from .tools import _ensure_sim
    notes = _ensure_sim(session)
    sim = session.sim
    from .temporal import clock, parse_time
    i = sim.node_index[nid]
    if not np.isfinite(sim.node_data["pressure"][:, i]).any():
        alt = next((x for x in reversed(list(session.sims.values()))
                    if np.isfinite(x.node_data["pressure"][:, i]).any()), None)
        if alt is None:
            return {"success": False, "error": f"Junction {nid} is cut off from every source in all available runs."}
        notes.append(f"In the shown run ({sim.id}) {nid} is cut off from every source, so run {alt.id} "
                     f"({alt.settings.label or 'earlier run'}) was used for the diagnosis.")
        sim = alt
    if time in (None, "", "worst"):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            t = int(np.nanargmin(sim.node_data["pressure"][:, i]))
        when = "its lowest pressure"
    else:
        t, _, _ = parse_time(session, sim, time)
        when = "the requested time"
    head = sim.node_data["head"][t]
    flow = sim.link_data["flow"][t]
    wn = model.wn
    incoming: dict[str, list[tuple[str, str, float]]] = {}
    for k, lid in enumerate(sim.link_ids):
        q = flow[k]
        if not np.isfinite(q) or abs(q) < 1e-6:
            continue
        link = wn.get_link(lid)
        up, down = (link.start_node_name, link.end_node_name) if q > 0 else (link.end_node_name, link.start_node_name)
        incoming.setdefault(down, []).append((lid, up, abs(float(q))))
    path, links, cur, seen = [nid], [], nid, {nid}
    sources = set(model.sources)
    while cur not in sources and len(path) < 2000:
        options = [o for o in incoming.get(cur, []) if o[1] not in seen]
        if not options:
            break
        lid, up, q = max(options, key=lambda o: o[2])
        links.append({"id": lid, "from": up, "to": cur, "flow_lps": round(q, 3)})
        seen.add(up)
        path.append(up)
        cur = up
    reached = cur if cur in sources else None
    comp = {"pipe_friction_m": 0.0, "valve_loss_m": 0.0, "pump_gain_m": 0.0}
    rows = []
    for L in links:
        link = wn.get_link(L["id"])
        dh = float(head[sim.node_index[L["from"]]] - head[sim.node_index[L["to"]]])
        k = sim.link_index[L["id"]]
        row = {"id": L["id"], "type": link.link_type, "head_loss_m": round(dh, 3), "flow_lps": L["flow_lps"]}
        if link.link_type == "Pipe":
            comp["pipe_friction_m"] += dh
            row.update({"diameter_mm": round(link.diameter * 1000, 1), "length_m": round(link.length, 1),
                        "velocity_ms": round(abs(float(sim.link_data["velocity"][t, k])), 3),
                        "headloss_m_per_km": round(abs(float(sim.link_data["headloss"][t, k])), 3)})
        elif link.link_type == "Pump":
            comp["pump_gain_m"] += -dh
        else:
            comp["valve_loss_m"] += dh
            row["valve_type"] = str(getattr(link, "valve_type", ""))
            row["setting"] = getattr(link, "initial_setting", None)
        rows.append(row)
    elev = float(wn.get_node(nid).elevation)
    p = float(sim.node_data["pressure"][t, i])
    src_head = float(head[sim.node_index[reached]]) if reached else None
    j_idx = [sim.node_index[j] for j in wn.junction_name_list]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        median_p = float(np.nanmedian(sim.node_data["pressure"][t, j_idx]))
        median_elev = float(np.median([wn.get_node(j).elevation for j in wn.junction_name_list]))
    req = session.criteria.min_pressure_m
    causes = []
    if src_head is not None:
        static = src_head - elev
        losses = comp["pipe_friction_m"] + comp["valve_loss_m"]
        if req is not None and static < req:
            causes.append(f"High ground: even with no losses, the source head ({src_head:.1f} m) is only {static:.1f} m above this "
                          f"junction ({elev:.1f} m), less than the required {req:g} m.")
        elif elev > median_elev + 0.25 * max(1.0, src_head - median_elev):
            causes.append(f"Elevation: the junction sits {elev - median_elev:.1f} m above the network median elevation.")
        if comp["valve_loss_m"] > 0.3 * max(losses, 1e-6) and comp["valve_loss_m"] > 2:
            v = max((r for r in rows if r["type"] == "Valve"), key=lambda r: r["head_loss_m"])
            causes.append(f"Pressure-reducing valve {v['id']} removes {v['head_loss_m']:.1f} m on the supply path"
                          + (f" (setting {v['setting']})" if v.get("setting") is not None else "") + ".")
        if comp["pipe_friction_m"] > 0.2 * max(static, 1e-6) and comp["pipe_friction_m"] > 2:
            worst = sorted((r for r in rows if r["type"] == "Pipe"), key=lambda r: r["head_loss_m"], reverse=True)[:3]
            causes.append("Pipe friction: " + f"{comp['pipe_friction_m']:.1f} m lost along the path, mostly in "
                          + ", ".join(f"{w['id']} ({w['head_loss_m']:.2f} m, {w['diameter_mm']:g} mm, {w['velocity_ms']:.2f} m/s)" for w in worst) + ".")
        if not causes:
            causes.append("No single dominant cause: the pressure follows from source head minus elevation, with modest losses.")
    else:
        causes.append("Could not trace back to a source at this time step (flow may be near zero or the node is cut off).")
    pipes_by_loss = sorted((r for r in rows if r["type"] == "Pipe"), key=lambda r: r["head_loss_m"], reverse=True)[:5]
    return {
        "success": True, "analysis": "diagnose_pressure", "node": nid, "time_h": round(float(sim.times_h[t]), 3),
        "clock": clock(session, float(sim.times_h[t])), "when": when, "time_index": t, "pressure_m": round(p, 3),
        "elevation_m": round(elev, 3), "network_median_pressure_m": round(median_p, 3),
        "source": reached, "source_head_m": None if src_head is None else round(src_head, 3),
        "static_pressure_m": None if src_head is None else round(src_head - elev, 3),
        "components": {k: round(v, 3) for k, v in comp.items()},
        "path_nodes": path, "path_links": [r["id"] for r in rows], "path_length": len(rows),
        "biggest_pipe_losses": pipes_by_loss, "valves_on_path": [r for r in rows if r["type"] != "Pipe"],
        "likely_causes": causes, "notes": notes, "sim_id": sim.id,
        "method": "Dominant supply path = at each node the incoming link carrying the most flow at that time step; "
                  "head losses are head differences between the link's ends.",
    }


# ---------------------------------------------------------------------- 3.2 assessment
def assess_network(session: Session, progress: Progress = _noop) -> dict[str, Any]:
    """Structured strengths / weaknesses using the app's analyses and transparent thresholds."""
    from . import topology as T
    from .resilience import demand_growth, pipe_criticality
    from .tools import _ensure_sim
    from .validation import validate
    model = session.model
    progress(0.05, "Model check…")
    val = validate(model)
    progress(0.15, "Topology…")
    ch = T.characterization(model)
    progress(0.3, "Hydraulics…")
    notes = _ensure_sim(session)
    sim = session.sim
    s = sim.summary
    # reuse results the user already has, so one report never shows two different numbers for the same thing
    progress(0.5, "Criticality (N-1)…")
    crit = session.results.get("pipe_criticality") or pipe_criticality(session)
    progress(0.8, "Demand growth…")
    growth = session.results.get("demand_growth") or demand_growth(session, verify_eps=False)
    session.results.setdefault("pipe_criticality", {k: v for k, v in crit.items() if k != "all"})
    session.results.setdefault("demand_growth", growth)
    pmin = s.get("pressure_m", {}).get("min", {}).get("value")
    pmax = s.get("pressure_m", {}).get("max", {}).get("value")
    from .tables import link_table
    lt = link_table(session)
    pipes = lt[lt["type"] == "Pipe"]
    stagnant = float((pipes["max_velocity_ms"] < 0.1).mean()) if "max_velocity_ms" in pipes else None
    fast = int((pipes["max_velocity_ms"] > 2.0).sum()) if "max_velocity_ms" in pipes else None
    findings: list[dict[str, Any]] = []

    def add(kind, area, text, evidence):
        findings.append({"kind": kind, "area": area, "text": text, "evidence": evidence})

    c = val["counts"]
    add("weakness" if c["error"] else "strength", "Model quality",
        f"{c['error']} errors and {c['warning']} warnings in the model check." if c["error"] or c["warning"] else "Model check found no errors or warnings.",
        "validation_report")
    bf = ch["branched_fraction"]
    if bf > 0.3:
        add("weakness", "Redundancy", f"{bf:.0%} of links are single-link bridges — large branched parts with no alternative supply route.", "topology")
    elif bf < 0.1:
        add("strength", "Redundancy", f"Well looped: only {bf:.0%} of links are bridges (meshedness {ch['meshedness_coefficient']}).", "topology")
    else:
        add("note", "Redundancy", f"Partly looped: {bf:.0%} of links are bridges; {ch['number_of_dead_ends']} dead ends.", "topology")
    req = session.criteria.min_pressure_m
    if pmin is not None:
        if req is not None:
            low = (s.get("criteria") or {}).get("low_pressure", {}).get("count")
            add("weakness" if low else "strength", "Pressure",
                f"Minimum pressure {pmin:.1f} m; {low} junctions fall below your {req:g} m limit." if low
                else f"All junctions stay above your {req:g} m limit (minimum {pmin:.1f} m).", "simulation")
        else:
            add("note", "Pressure", f"Pressure ranges {pmin:.1f}–{pmax:.1f} m (no design criteria set, so not judged).", "simulation")
        if pmax is not None and pmax > 60:
            add("note", "Pressure", f"Maximum pressure {pmax:.1f} m — high pressure raises leakage and burst risk; check PRV zones.", "simulation")
    if stagnant is not None:
        if stagnant > 0.4:
            add("weakness", "Water quality", f"{stagnant:.0%} of pipes never exceed 0.1 m/s — risk of stagnation and old water.", "query_table")
        else:
            add("strength", "Water quality", f"Only {stagnant:.0%} of pipes stay below 0.1 m/s.", "query_table")
    if fast:
        add("weakness", "Velocity", f"{fast} pipes exceed 2 m/s at peak — high headloss and transient risk.", "query_table")
    if crit.get("success"):
        top = crit["top"][0] if crit["top"] else None
        total = crit["total_demand_lps"]
        big = [r for r in crit["top"] if r["shortfall_pct"] >= 5]
        if big:
            add("weakness", "Criticality", f"{len(big)} single pipe failures each cut ≥ 5% of peak demand; worst {top['id']} "
                f"({top['shortfall_pct']:.1f}%).", "pipe_criticality")
        else:
            add("strength", "Criticality", f"No single pipe failure cuts more than 5% of peak demand ({total:.1f} L/s).", "pipe_criticality")
    if growth.get("capacity_factor") and not isinstance(growth.get("capacity_factor"), str):
        f = growth["capacity_factor"]
        add("strength" if f >= 1.5 else "weakness", "Capacity",
            f"Demand can grow {f:g}× before pressure drops below {growth['min_pressure_m']:g} m.", "demand_growth")
    tr = s.get("todini_resilience")
    if tr:
        add("strength" if tr["mean"] >= 0.5 else "weakness", "Resilience index", f"Todini index mean {tr['mean']:.2f} (min {tr['min']:.2f}).", "simulation")
    for t in s.get("tanks", []):
        if t["hit_empty"]:
            add("weakness", "Storage", f"Tank {t['id']} empties during the run.", "tank_events")
    next_steps = ["Set design criteria (Criteria tab) so pressures and velocities can be judged." if req is None else None,
                  "Run fire-flow checks at critical hydrants.", "Explore valve isolation segments for maintenance planning.",
                  "Run water-quality (chlorine / source share) analyses." if stagnant and stagnant > 0.2 else None]
    return {"success": True, "analysis": "assess_network", "findings": findings, "notes": notes,
            "strengths": sum(f["kind"] == "strength" for f in findings), "weaknesses": sum(f["kind"] == "weakness" for f in findings),
            "next_steps": [x for x in next_steps if x],
            "thresholds": "Bridges > 30% = weakly looped; < 10% = well looped. Stagnation: peak velocity < 0.1 m/s on > 40% of pipes. "
                          "Velocity > 2 m/s flagged. Single failures cutting ≥ 5% of peak demand flagged. Growth factor < 1.5 flagged. "
                          "Todini mean ≥ 0.5 counted as a strength. These are rules of thumb — adjust to your context."}
