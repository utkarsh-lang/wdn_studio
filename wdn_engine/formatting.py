"""Markdown rendering of verified tool results (used when no LLM is configured)."""

from __future__ import annotations

from typing import Any


def _n(v: Any, d: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, (int,)) and not isinstance(v, bool):
        return f"{v:,}"
    try:
        return f"{float(v):,.{d}f}"
    except (TypeError, ValueError):
        return str(v)


def _ids(ids: list[Any], limit: int = 25) -> str:
    if not ids:
        return "none"
    shown = ", ".join(f"`{i}`" for i in ids[:limit])
    return shown + (f" … (+{len(ids) - limit} more)" if len(ids) > limit else "")


def _table(rows: list[dict[str, Any]], cols: list[str], limit: int = 15) -> str:
    if not rows:
        return ""
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows[:limit]:
        lines.append("| " + " | ".join(_n(r.get(c), 3) if isinstance(r.get(c), float) else str(r.get(c, "")) for c in cols) + " |")
    if len(rows) > limit:
        lines.append(f"\n_Showing {limit} of {len(rows)} rows — all are highlighted on the map._")
    return "\n".join(lines)


def format_result(tool: str, r: dict[str, Any]) -> str:
    if not r.get("success", True):
        return f"⚠️ {r.get('error', 'The analysis failed.')}"
    f = globals().get(f"_fmt_{tool}")
    if f:
        return f(r)
    return "```json\n" + str(r)[:3000] + "\n```"


def _fmt_network_summary(r):
    return (f"**{r['name']}** — {r['nodes']:,} nodes ({r['junctions']:,} junctions, {r['reservoirs']} reservoirs, "
            f"{r['tanks']} tanks) and {r['links']:,} links ({r['pipes']:,} pipes, {r['pumps']} pumps, {r['valves']} valves).\n\n"
            f"- Total pipe length: **{_n(r['total_pipe_length_km'], 2)} km**\n"
            f"- Total base demand: **{_n(r['total_base_demand_lps'])} L/s** (all demand categories)\n"
            f"- File units: {r['inp_flow_units']} · headloss: {r['headloss_formula']} (roughness = {r['roughness_meaning']})\n"
            f"- Simulation duration in file: {_n(r['duration_h'], 1)} h · coordinates: {r['coordinates']}")


def _fmt_validation_report(r):
    c = r["counts"]
    lines = [f"**Model check:** {c['error']} error(s), {c['warning']} warning(s), {c['info']} note(s)."]
    for i in r["issues"]:
        icon = {"error": "🔴", "warning": "🟠", "info": "🔵"}[i["severity"]]
        lines.append(f"- {icon} {i['message']}" + (f" ({i['count']}: {_ids(i['elements'], 8)})" if i["count"] else ""))
    if not r["issues"]:
        lines.append("No problems found.")
    return "\n".join(lines)


def _fmt_pipe_statistics(r):
    L, D, R = r["length_m"], r["diameter_mm"], r["roughness"]
    rows = [
        f"| Length (m) | {_n(L.get('min'))} | {_n(L.get('median'))} | {_n(L.get('mean'))} | {_n(L.get('max'))} |",
        f"| Diameter (mm) | {_n(D.get('min'))} | {_n(D.get('median'))} | {_n(D.get('mean'))} | {_n(D.get('max'))} |",
        f"| Roughness | {_n(R.get('min'))} | {_n(R.get('median'))} | {_n(R.get('mean'))} | {_n(R.get('max'))} |",
    ]
    classes = ", ".join(f"{_n(c['diameter_mm'], 0)} mm: {c['count']}" for c in r["length_by_diameter"][:12])
    return (f"**{r['number_of_pipes']:,} pipes, {_n(r['total_length_km'])} km total.**\n\n"
            "| | min | median | mean | max |\n|---|---|---|---|---|\n" + "\n".join(rows) +
            f"\n\nRoughness is {r['roughness_meaning']}. Pipes per diameter class: {classes}.")


def _fmt_filter_pipes(r):
    return (f"**{r['number_of_matching_pipes']:,} pipe(s)** match {' and '.join(r['conditions'])} "
            f"({r['share_of_pipes']:.1%} of pipes, {_n(r['matching_length_km'], 3)} km).\n\n" +
            _table(r["pipes"], ["id", "from", "to", "length_m", "diameter_mm", "roughness"]))


def _fmt_element_info(r):
    skip = {"success", "connected_links", "x", "y", "vertices", "simulated"}
    lines = [f"**{r['type']} `{r['id']}`**", ""]
    lines += [f"- {k.replace('_', ' ')}: {_n(v) if isinstance(v, float) else v}" for k, v in r.items() if k not in skip and k not in {"id", "type"}]
    if r.get("connected_links"):
        lines.append(f"- connected links: {_ids([c['id'] for c in r['connected_links']])}")
    if r.get("simulated"):
        lines.append(f"- simulated: " + ", ".join(f"{k.replace('_', ' ')} {_n(v)}" for k, v in r["simulated"].items() if k != "sim_id"))
    return "\n".join(lines)


def _fmt_topology_characterization(r):
    sc = r["source_connectivity"]
    focus = r.get("focus", "overall")
    if focus in {"dead_ends", "dead ends"}:
        n = r["number_of_dead_ends"]
        return (f"**{n:,} dead-end junction(s)** — junctions with only one neighbour (reservoirs/tanks excluded). "
                + ("They are highlighted on the map. Dead ends have no alternative supply route and tend to have low velocity "
                   "and older water." if n else "✅ None found: every junction has at least two neighbours."))
    if focus in {"bridges", "critical_links"}:
        n = r["number_of_bridges"]
        return (f"**{n:,} single-link bridge(s)** — pipes whose individual failure splits the network "
                f"({r['branched_fraction']:.1%} of links). " +
                ("Highlighted on the map; longest first: " + _ids([b["link_id"] for b in r["bridges"]], 10)
                 if n else "✅ None found: every pipe has an alternative route (parallel pipes are not counted as bridges)."))
    if focus in {"articulation_points", "articulation"}:
        n = r["number_of_articulation_points"]
        return (f"**{n:,} articulation point(s)** — nodes whose loss splits the network. " +
                ("Highlighted on the map." if n else "✅ None found: the network is fully looped (biconnected)."))
    if focus in {"unsupplied", "connectivity"}:
        n = sc["number_unsupplied"]
        if not n:
            return ("✅ **0 junctions without a source.** Every junction has an open path to at least one reservoir or tank "
                    f"({len(sc['sources'])} source(s): {_ids(sc['sources'], 10)}). Uses the initial link status from the file.")
        return (f"⚠️ **{n:,} junction(s) have no open path to any reservoir or tank** ({_n(sc['unsupplied_base_demand_lps'])} L/s "
                "of base demand cannot be supplied). They are highlighted on the map — usually a missing pipe or a link "
                "closed by mistake.")
    return (f"**Network characterization**\n\n"
            f"- {r['nodes']:,} nodes, {r['physical_links']:,} links ({r['parallel_link_excess']} parallel duplicates)\n"
            f"- Components: {r['number_of_components']} ({'connected' if r['connected'] else 'disconnected'})\n"
            f"- Independent loops: {r['cycle_rank_unique_connections']:,} · meshedness: {_n(r['meshedness_coefficient'], 4)} "
            f"· average degree: {_n(r['average_degree'], 3)}\n"
            f"- Dead-end junctions: {r['number_of_dead_ends']:,} · articulation points: {r['number_of_articulation_points']:,} "
            f"· single-link bridges: {r['number_of_bridges']:,} ({r['branched_fraction']:.1%} of links)\n"
            f"- Largest biconnected (looped) block: {r['largest_biconnected_component']:,} nodes\n"
            f"- Junctions with no open path to a source: **{sc['number_unsupplied']}**\n"
            f"- Degree distribution: `{r['degree_distribution']}`")


def _fmt_node_metric(r):
    rows = "\n".join(f"{i}. `{t['id']}` — {t['value']:.5g} (degree {t['degree']})" for i, t in enumerate(r["top"][:10], 1))
    approx = " _(sampled approximation)_" if r["approximate"] else ""
    return (f"**{r['metric'].replace('_', ' ').title()}** — {r['description']} Weighting: {r['weight_meaning']}.{approx}\n\n"
            f"Network mean: {r['mean']:.5g}. The map is coloured by this metric. Top nodes:\n\n{rows}")


def _fmt_link_metric(r):
    rows = "\n".join(f"{i}. `{t['id']}` — {t['value']:.5g}" for i, t in enumerate(r["top"][:10], 1))
    return f"**{r['metric'].replace('_', ' ').title()}** — {r['description']}\n\nTop links:\n\n{rows}"


def _fmt_shortest_path(r):
    return (f"**Path `{r['source']}` → `{r['target']}`** ({r['weighted_by']}): {r['number_of_links']} links, "
            f"**{_n(r['total_length_m'])} m**.\n\n{' → '.join(map(str, r['path_nodes'][:60]))}"
            + (" …" if len(r["path_nodes"]) > 60 else ""))


def _fmt_distance_statistics(r):
    return (f"**Graph diameter:** {_n(r['largest_component_diameter'])} {r['unit']} · "
            f"**average shortest path:** {_n(r['largest_component_average_shortest_path'])} {r['unit']} "
            f"(largest component, {r['weighted_by']}). {r['note']}")


def _fmt_run_simulation(r):
    p, v = r.get("pressure_m", {}), r.get("velocity_ms", {})
    lines = [f"**Simulation `{r['sim_id']}`** — {r['demand_model']}, {_n(r['duration_h'], 1)} h, "
             f"{r['frames']} time steps of {_n(r['report_step_min'], 0)} min (solved in {_n(r['run_time_s'])} s).", ""]
    if p:
        lines.append(f"- Pressure: min **{_n(p['min'].get('value'))} m** at `{p['min'].get('element')}` "
                     f"(t={_n(p['min'].get('time_h'), 1)} h), max {_n(p['max'].get('value'))} m at `{p['max'].get('element')}`, mean {_n(p['mean'])} m")
        lines.append(f"- Junctions that ever go negative: {r.get('junctions_with_negative_pressure', 0)}")
        if r.get("leakage"):
            lk = r["leakage"]
            lines.append(f"- Leakage: mean **{_n(lk['mean_lps'], 3)} L/s**, {_n(lk['volume_m3'], 1)} m³ over the run ({_n(lk['share_of_supply_pct'], 1)}% of supply)")
        if r.get("isolated_junctions_count"):
            lines.append(f"- ⚠️ Junctions cut off from every source: **{r['isolated_junctions_count']}**")
    if v:
        lines.append(f"- Max velocity: {_n(v['max'].get('value'))} m/s in `{v['max'].get('element')}` · "
                     f"pipes with flow reversal: {r.get('pipes_with_flow_reversal', 0)}")
    if "demand_satisfaction_ratio" in r:
        lines.append(f"- Demand delivered: **{_n((r['demand_satisfaction_ratio'] or 0) * 100, 1)}%** of required")
    for t in r.get("tanks", []):
        lines.append(f"- Tank `{t['id']}` level {_n(t['min_level_m'])}–{_n(t['max_level_m'])} m"
                     + (" ⚠️ empties" if t["hit_empty"] else "") + (" (reaches full)" if t["hit_full"] else ""))
    for pm in r.get("pumps", []):
        lines.append(f"- Pump `{pm['id']}`: {_n(pm['hours_running'], 1)} h running, {_n(pm.get('energy_kwh'))} kWh")
    if r.get("todini_resilience"):
        tr = r["todini_resilience"]
        lines.append(f"- Todini resilience index (P* = {tr['required_pressure_m']} m): mean {_n(tr['mean'], 3)}, min {_n(tr['min'], 3)}")
    crit = r.get("criteria")
    if crit:
        parts = []
        for key, label in (("low_pressure", "below min pressure"), ("high_pressure", "above max pressure"),
                           ("high_velocity", "above max velocity"), ("low_velocity", "below min velocity"),
                           ("high_headloss", "above max headloss")):
            if key in crit:
                parts.append(f"{crit[key]['count']} {label}")
        lines.append(f"- Criteria ({crit['criteria']['label']}): " + "; ".join(parts))
    for note in r.get("notes", []):
        lines.append(f"- _{note}_")
    return "\n".join(lines)


_fmt_simulation_results = _fmt_run_simulation


def _fmt_compare_runs(r):
    u = r["unit"]
    lines = [f"**{r['label']}**", "",
             f"- Elements that changed by more than {r['tolerance']:g} {u}: **{r['changed_count']:,}**",
             (f"- Largest decrease: **{_n(r['max_decrease']['value'], 3)} {u}** at `{r['max_decrease']['element']}`"
              if (r['max_decrease']['value'] or 0) < 0 else "- No still-supplied element decreased"),
             f"- Largest increase: **{_n(r['max_increase']['value'], 3)} {u}** at `{r['max_increase']['element']}`",
             f"- Mean absolute change: {_n(r['mean_abs_change'], 4)} {u}"]
    if r["lost_supply_count"]:
        lines.append(f"- ⚠️ {r['lost_supply_count']} element(s) lost supply entirely in the scenario: {_ids(r['lost_supply'], 10)}")
    if r["changed_count"] == 0:
        lines.append("\nNo meaningful change — the closed link probably has a parallel or looped alternative, "
                     "or carried almost no flow.")
    return "\n".join(lines)


def _fmt_rank_elements(r):
    lines = [f"_{n}_" for n in r.get("notes", [])]
    lines.append(f"**{r['order'].capitalize()} {r['variable']}** ({r['statistic']}, {r['unit']}) — highlighted on the map:")
    lines.append("")
    lines += [f"{i}. `{e['id']}` — **{_n(e['value'], 3)} {r['unit']}**" for i, e in enumerate(r["elements"], 1)]
    if r["variable"] == "age" and r["elements"] and r.get("duration_h") and r["elements"][0]["value"] >= 0.98 * r["duration_h"]:
        lines.append("\nAn age equal to the run length means that water there was never refreshed during the run "
                     "(typically a zero-demand dead end). Longer runs give more settled ages.")
    return "\n".join(lines)


def _fmt_criteria_violations(r):
    ids = r.get("nodes") or r.get("links") or []
    if not r["count"]:
        return f"✅ No element fails `{r['check'].replace('_', ' ')}`."
    return f"**{r['count']:,} element(s)** fail `{r['check'].replace('_', ' ')}`. Highlighted on the map: {_ids(ids)}"


def _fmt_set_design_criteria(r):
    c = r["criteria"]
    return "Design criteria updated: " + ", ".join(f"{k} = {v}" for k, v in c.items() if v is not None and k != "label")


def _fmt_set_map_colour(r):
    return f"Map coloured by **{r['attribute']}**."


def _notes(r):
    return "".join(f"_{n}_\n\n" for n in r.get("notes", []) if n)


def _fmt_query_table(r):
    head = _notes(r)
    conds = " and ".join(f"{f['column']} {f.get('op', '=')} {f.get('value', '')}".strip() for f in r["filters"]) or "no filter"
    if r.get("aggregate"):
        a = r["aggregate"]
        val = a["value"]
        what = f"{a['func']} of {a['column']}" if a["func"] != "count" else "count"
        line = f"**{what} = {_n(val, 4) if isinstance(val, float) else val}**"
        if r.get("aggregate_element"):
            line += f" (at `{r['aggregate_element']}`)"
        head += f"{line} — {r['total_matches']:,} of {r['total_rows']:,} {r['table']} match: {conds}.\n\n"
    else:
        head += f"**{r['total_matches']:,} of {r['total_rows']:,} {r['table']}** match: {conds}.\n\n"
    return head + _table(r["rows"], r["columns"], 15)


def _fmt_value_at_time(r):
    vals = ", ".join(f"{k} **{_n(v, 3) if isinstance(v, (int, float)) else v}** {r['units'].get(k, '')}".rstrip()
                     for k, v in r["values"].items())
    return _notes(r) + f"`{r['element']}` at **{r['clock']}** (t = {_n(r['time_h'])} h): {vals}." + (f"\n\n_{r['note']}_" if r["note"] else "")


def _fmt_when_extreme(r):
    scope = "the whole network" if r["scope"] == "network" else f"`{r['scope']}`"
    label = {"total_demand": "total demand", "tank_level": "tank level"}.get(r["variable"], r["variable"])
    word = "highest" if r["extreme"] == "max" else "lowest"
    line = f"For {scope}, {label} is **{word} at {r['clock']}** (t = {_n(r['time_h'])} h): **{_n(r['value'], 3)} {r['unit']}**"
    if r.get("element"):
        line += f", at `{r['element']}`"
    line += f". Range over the run: {_n(r['series_range'][0], 3)}–{_n(r['series_range'][1], 3)} {r['unit']}. The map is set to that time."
    return _notes(r) + line


def _fmt_tank_events(r):
    lines = [_notes(r).strip()] if r.get("notes") else []
    for t in r["tanks"]:
        lines.append(f"**Tank `{t['id']}`** (limits {t['limits_m'][0]:g}–{t['limits_m'][1]:g} m): starts {_n(t['start_level_m'])} m, "
                     f"lowest **{_n(t['min_level_m'])} m** ({t['min_at']}), highest **{_n(t['max_level_m'])} m** ({t['max_at']}), "
                     f"ends {_n(t['final_level_m'])} m (net {t['net_change_m']:+.2f} m over {_n(r['duration_h'], 0)} h).")
        lines.append(("- ⚠️ Empties first at " + t["first_empty_at"]) if t["empties"] else "- Never empties.")
        lines.append(("- Reaches full first at " + t["first_full_at"]) if t["fills"] else "- Never reaches its maximum level.")
        lines.append(f"- Fill/drain direction changes: {t['fill_drain_reversals']}")
    return "\n".join(lines)


def _fmt_nodes_within(r):
    return (f"**{r['count']:,} nodes** ({r['junctions']:,} junctions, {_n(r['base_demand_lps'])} L/s base demand) lie within "
            f"**{r['distance_m']:g} m** of `{r['center']}`. _{r['note']}_ Highlighted on the map.\n\n" +
            _table(r["nodes"], ["id", "type", "distance_m"], 10))


def _fmt_trace_flow(r):
    srcs = ", ".join(f"`{x}`" for x in r["sources_reached"]) or "none"
    what = "supplied by (downstream of)" if r["direction"] == "downstream" else "supplying (upstream of)"
    return (_notes(r) + f"Following the {r['basis']}: **{r['junctions']:,} junctions and {r['links']:,} links** are {what} "
            f"`{r['element']}`, with **{_n(r['base_demand_lps'])} L/s** of base demand. Sources/tanks in the traced area: {srcs}. "
            "Highlighted on the map." + (f"\n\n_{r['note']}_" if r["note"] else ""))


def _fmt_pipe_criticality(r):
    lines = [f"**Pipe criticality (N-1)** — {r['elements_tested']:,} links tested in {r['run_time_s']:g} s; "
             f"required pressure {r['required_pressure_m']:g} m; peak demand {_n(r['total_demand_lps'])} L/s.", "",
             f"- Links whose loss causes a supply shortfall or pushes junctions below {r['required_pressure_m']:g} m: **{r['critical_count']:,}**",
             f"- Links with no significant impact (< 0.5 m drop, no shortfall): {r['no_significant_impact']:,}"]
    if r["unsolvable"]:
        lines.append(f"- EPANET could not solve {r['unsolvable']} closure(s)")
    lines += ["", "Most critical (map coloured by shortfall):", "",
              "| rank | link | shortfall L/s | % demand | junctions < req | max drop m | bridge |", "|---|---|---|---|---|---|---|"]
    for t in r["top"][:10]:
        lines.append(f"| {t['rank']} | `{t['id']}` | {_n(t['demand_shortfall_lps'], 3)} | {_n(t['shortfall_pct'], 2)} | "
                     f"{t['junctions_newly_below_req']} | {_n(t['max_pressure_drop_m'], 1)} | {'yes' if t['is_bridge'] else ''} |")
    lines += ["", f"_{r['method']} {r['note']}_"]
    return "\n".join(lines)


def _fmt_valve_segments(r):
    lines = [f"**Valve isolation segments** — {r['segments']:,} segments from {r['valves']:,} valves "
             f"(largest {r['largest_segment_nodes']} nodes, mean {r['mean_segment_nodes']}).", "",
             f"_{r['assumption']}_", "",
             f"- Segments whose isolation also cuts off other areas: **{r['segments_cutting_off_others']}**", "",
             "| segment | nodes | valves to close | junctions without water | demand lost L/s |", "|---|---|---|---|---|"]
    for t in r["top"][:8]:
        lost = t.get("demand_shortfall_lps", t["base_demand_lost_lps"])
        lines.append(f"| #{t['segment']}{' (source)' if t['contains_source'] else ''} | {t['nodes']} | {t['valves_to_close']} | "
                     f"{t['junctions_without_water']} | {_n(lost, 3)} |")
    lines += ["", f"_{r['note']}_ The most critical segment is highlighted."]
    return "\n".join(lines)


def _fmt_fire_flow(r):
    if "weakest" in r:
        lines = [f"**Available fire flow** at {r['residual_pressure_m']:g} m residual for {r['nodes_tested']:,} junctions: "
                 f"median **{_n(r['median_lps'])} L/s**, lowest **{_n(r['min_lps'])} L/s**."]
        if r.get("required_flow_lps"):
            lines.append(f"- Junctions that cannot supply {r['required_flow_lps']:g} L/s: **{r['below_required']}**")
        lines.append("\nWeakest (map coloured by available flow): " + ", ".join(f"`{w['id']}` {_n(w['available_lps'], 1)}" for w in r["weakest"][:10]))
    else:
        lines = [f"**Fire flow at `{r['id']}`**: up to **{_n(r['available_lps'])} L/s** while keeping {r['residual_pressure_m']:g} m "
                 f"(limited by {r.get('limited_by', 'pressure')})."]
        c = r.get("check")
        if c:
            lines.append(f"- With {c['required_flow_lps']:g} L/s: hydrant pressure **{_n(c['hydrant_pressure_m'])} m** "
                         f"({'meets' if c['meets_requirement'] else 'FAILS'} the residual), network minimum {_n(c['network_min_pressure_m'])} m "
                         f"at `{c['min_at']}`, {c['junctions_drop_over_5m']} junctions drop > 5 m.")
    lines.append(f"\n_{r['method']} {r['note']}_")
    return "\n".join(lines)


def _fmt_demand_growth(r):
    if r.get("capacity_factor") is None or isinstance(r.get("capacity_factor"), str):
        return f"**Demand growth:** {r.get('message', '')}\n\n_{r.get('note', '')}_"
    lines = [f"**Demand can grow to {r['capacity_factor']:g}× today's peak (+{r['growth_headroom_pct']:g}%)** before the minimum "
             f"pressure drops below {r['min_pressure_m']:g} m (today's minimum {_n(r['current_min_pressure_m'])} m at `{r['current_min_at']}`).",
             "", "First nodes to fail (highlighted): " + ", ".join(f"`{n}`" for n in r["first_failing_nodes"][:8]),
             "", "| demand × | min pressure m | at |", "|---|---|---|"]
    lines += [f"| {c['factor']:g} | {_n(c['min_pressure_m'])} | `{c['at']}` |" for c in r["curve"]]
    if r.get("eps_check"):
        e = r["eps_check"]
        lines.append(f"\nFull extended-period check at ×{e['demand_multiplier']:g}: minimum **{_n(e['min_pressure_m'])} m** at `{e['min_at']}`. _{e['note']}_")
    lines.append(f"\n_{r['method']} {r['note']}_")
    return "\n".join(lines)


def _fmt_leak_scenario(r):
    lk = r.get("leakage") or {}
    pc = r["pressure_change"]
    lines = [_notes(r).strip()] if r.get("notes") else []
    lines += [f"**Leak scenario** — {r['diameter_mm']:g} mm at {', '.join(f'`{e}`' for e in r['elements'])} ({r['demand_model']}, {r['duration_h']:g} h):",
              f"- Leak flow: mean **{_n(lk.get('mean_lps'), 3)} L/s** (max {_n(lk.get('max_lps'), 3)}), volume lost **{_n(lk.get('volume_m3'), 1)} m³** "
              f"({_n(lk.get('share_of_supply_pct'), 1)}% of supply)",
              f"- Mean pressure fell at **{pc['changed_count']:,}** junctions; largest drop **{_n(pc['max_decrease']['value'], 3)} m** at `{pc['max_decrease']['element']}`",
              "- Where a pressure sensor would notice it most: " + ", ".join(f"`{x}`" for x in r["sensor_hint"][:6]),
              "", "The map shows the pressure change (red = drop) vs the no-leak run."]
    return "\n".join(lines)


def _fmt_water_quality(r):
    if r.get("analysis") == "source_shares":
        lines = [f"**Where does the water come from?** ({_n(r['duration_h'], 0)} h trace runs)", "",
                 "| source | type | junctions mostly supplied | junctions receiving > 5% | demand mostly supplied L/s |", "|---|---|---|---|---|"]
        lines += [f"| `{x['source']}` | {x['type']} | {x['junctions_mostly_supplied']} | {x['junctions_over_5pct']} | {_n(x['base_demand_mostly_supplied_lps'])} |"
                  for x in r["sources"]]
        lines += ["", f"Junctions with mixed supply (≥ 20% from two or more sources): **{r['mixed_supply_junctions']}**.",
                  "Map coloured by the share from the first source; switch with Colour → Analysis layers.", "", f"_{r['note']}_"]
        return "\n".join(lines)
    head = _fmt_run_simulation(r)
    extra = []
    if r.get("chlorine_mgl"):
        c = r["chlorine_mgl"]
        extra.append(f"- Chlorine (second half of run): mean **{_n(c['mean_in_second_half'], 3)} mg/L**, lowest {_n(c['min_in_second_half'], 3)} mg/L; "
                     f"junctions below 0.2 mg/L: **{c['junctions_below_0_2']}**")
    if r.get("trace"):
        t = r["trace"]
        extra.append(f"- Water from `{t['source']}`: mostly (≥ 50%) at **{t['junctions_mostly_supplied_pct50']}** junctions, "
                     f"some (≥ 5%) at {t['junctions_any_supply_pct5']}")
    return head + ("\n" + "\n".join(extra) if extra else "")


def _fmt_what_if(r):
    icon = {"none": "✅", "minor": "🟡", "moderate": "🟠", "major": "🔴"}[r["severity"]]
    pc = r["pressure_change_min"]
    lines = [f"**What if: {r['scenario']}** — impact: {icon} **{r['severity']}** ({r['demand_model']}, {r['duration_h']:g} h, required pressure {r['required_pressure_m']:g} m)", ""]
    lines.append("Steps: " + " → ".join(r["steps"]))
    lines.append("")
    if r["junctions_newly_cut_off"]:
        lines.append(f"- ⚠️ **{r['junctions_newly_cut_off']}** junctions are cut off from every source")
    lines.append(f"- Junctions newly below {r['required_pressure_m']:g} m: **{r['junctions_newly_below_required']}**")
    lines.append(f"- Minimum pressure changed at {pc['changed_count']:,} junctions; largest drop **{_n(pc['max_decrease']['value'], 2)} m** at `{pc['max_decrease']['element']}`")
    if r.get("supply"):
        sp = r["supply"]
        lines.append(f"- Demand delivered: {_n(sp['baseline_delivered_pct'], 1)}% → **{_n(sp['scenario_delivered_pct'], 1)}%**"
                     + (f"; {_n(sp['volume_not_delivered_m3'], 1)} m³ not delivered, {sp['junctions_with_shortfall']} junctions short" if sp.get("volume_not_delivered_m3") else ""))
    for t in r["tanks"]:
        lines.append(f"- Tank `{t['id']}` lowest level {_n(t['baseline_min_m'])} → **{_n(t['scenario_min_m'])} m**" + (" — ⚠️ empties" if t["empties_in_scenario"] else ""))
    for p in r["pumps"]:
        lines.append(f"- Pump `{p['id']}`: {_n(p['baseline_hours'], 1)} → {_n(p['scenario_hours'], 1)} h running, {_n(p['baseline_kwh'])} → {_n(p['scenario_kwh'])} kWh")
    if r["worst_junctions"]:
        lines.append("- Most affected (min pressure change): " + ", ".join(f"`{w['id']}` {_n(w['change_m'], 2)} m" for w in r["worst_junctions"][:6]))
    for n in r.get("scenario_notes", [])[:3]:
        lines.append(f"- _{n}_")
    lines.append("\nThe map shows the change in minimum pressure (red = lower) and highlights the most affected junctions.")
    return "\n".join(lines)


def _fmt_diagnose_pressure(r):
    c = r["components"]
    lines = [_notes(r).strip()] if r.get("notes") else []
    lines += [f"**Why is pressure {_n(r['pressure_m'])} m at `{r['node']}`?** (at {r['clock']}, {r['when']}; network median {_n(r['network_median_pressure_m'])} m)", ""]
    if r["source"]:
        lines += [f"Supply path traced back to **`{r['source']}`** through {r['path_length']} links (highlighted):", "",
                  f"| | m |", "|---|---|",
                  f"| Head at `{r['source']}` | {_n(r['source_head_m'])} |",
                  f"| − junction elevation | {_n(r['elevation_m'])} |",
                  f"| = static pressure (no losses) | **{_n(r['static_pressure_m'])}** |",
                  f"| − pipe friction on the path | {_n(c['pipe_friction_m'])} |",
                  f"| − valve losses | {_n(c['valve_loss_m'])} |",
                  f"| + pump head gained | {_n(c['pump_gain_m'])} |",
                  f"| = pressure | **{_n(r['pressure_m'])}** |", ""]
    lines.append("**Likely cause(s):**")
    lines += [f"- {x}" for x in r["likely_causes"]]
    if r["biggest_pipe_losses"]:
        b = r["biggest_pipe_losses"][0]
        lines.append(f"\nLargest single pipe loss: `{b['id']}` {_n(b['head_loss_m'], 3)} m ({b['diameter_mm']:g} mm, {_n(b['velocity_ms'], 2)} m/s, {_n(b['headloss_m_per_km'], 2)} m/km).")
    lines.append(f"\n_{r['method']}_")
    return "\n".join(lines)


def _fmt_assess_network(r):
    icon = {"strength": "✅", "weakness": "⚠️", "note": "ℹ️"}
    lines = [f"**Network assessment** — {r['strengths']} strengths, {r['weaknesses']} weaknesses.", ""]
    for kind in ("weakness", "strength", "note"):
        for f in r["findings"]:
            if f["kind"] == kind:
                lines.append(f"- {icon[kind]} **{f['area']}:** {f['text']}")
    if r["next_steps"]:
        lines += ["", "**Suggested next steps:**"] + [f"- {x}" for x in r["next_steps"]]
    lines += ["", f"_Thresholds: {r['thresholds']}_"]
    return "\n".join(lines)


def _fmt_explain_term(r):
    return f"**{r['title']}** — {r['short']}\n\n{r['detail']}"


def _fmt_edit_element(r):
    lines = [f"✏️ Edited `{e['id']}` ({e['type']}): {e['field'].replace('_', ' ')} {e['old']} → **{e['new']}**" for e in r["edits"]]
    return "\n".join(lines) + f"\n\n_{r['note']}_ Total edits: {r['total_edits']} (undo with `undo last edit`)."


def _fmt_undo_edit(r):
    e = r["undone"]
    return f"↩️ Undid: `{e['id']}` {e['field'].replace('_', ' ')} back to **{e['old']}**. Remaining edits: {r['total_edits']}."
