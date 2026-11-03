"""Word / PDF report generator.

The report is first assembled as a neutral list of blocks (headings, paragraphs, bullet lists, tables,
figures), then rendered to .docx (python-docx) or .pdf (reportlab).  Every number comes from the
engine; the optional LLM discussion is number-checked and labelled.
"""

from __future__ import annotations

import io
import time
import warnings
from dataclasses import dataclass, field
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402

from . import topology as T  # noqa: E402
from .session import Session  # noqa: E402
from .validation import validate  # noqa: E402

BLUE, ORANGE, INK, MUTED, GRID = "#2a78d6", "#eb6834", "#0b0b0b", "#52514e", "#dedcd6"
SEQ = matplotlib.colors.LinearSegmentedColormap.from_list("wdn_seq", ["#cde2fb", "#86b6ef", "#3987e5", "#256abf", "#184f95", "#0d366b"])


@dataclass
class Doc:
    title: str
    subtitle: str = ""
    blocks: list[tuple] = field(default_factory=list)

    def h(self, level: int, text: str): self.blocks.append(("h", level, text))
    def p(self, text: str, italic: bool = False): self.blocks.append(("p", text, italic))
    def bullets(self, items: list[str]): self.blocks.append(("ul", [i for i in items if i]))
    def table(self, headers: list[str], rows: list[list[Any]], caption: str = ""): self.blocks.append(("table", headers, rows, caption))
    def fig(self, png: bytes, caption: str): self.blocks.append(("img", png, caption))


def _f(v: Any, d: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, np.integer)):
        return f"{int(v):,}"
    try:
        return f"{float(v):,.{d}f}"
    except (TypeError, ValueError):
        return str(v)


# ------------------------------------------------------------------- figures
def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=170, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def _style(ax):
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def map_figure(session: Session, node_values: dict[str, float] | None = None, link_values: dict[str, float] | None = None,
               label: str = "", highlight_links: list[str] | None = None, highlight_nodes: list[str] | None = None) -> bytes:
    m = session.model
    pos = m.layout
    fig, ax = plt.subplots(figsize=(7.2, 5.2))
    segs, vals, widths = [], [], []
    dmin, dmax = 1e9, 0
    for l in m.links():
        if l.get("diameter_mm"):
            dmin, dmax = min(dmin, l["diameter_mm"]), max(dmax, l["diameter_mm"])
    for l in m.links():
        pts = [pos[l["from"]]] + [tuple(v) for v in l.get("vertices", [])] + [pos[l["to"]]]
        segs.append(pts)
        vals.append((link_values or {}).get(l["id"], np.nan))
        d = l.get("diameter_mm") or dmin
        widths.append(0.5 + 2.0 * ((d - dmin) / (dmax - dmin)) ** 0.5 if dmax > dmin else 1.0)
    lc = LineCollection(segs, linewidths=widths, colors="#9a9891", zorder=1)
    if link_values:
        arr = np.array(vals, dtype=float)
        lc.set_array(np.ma.masked_invalid(arr))
        lc.set_cmap(SEQ)
        finite = arr[np.isfinite(arr)]
        if finite.size:
            nz = finite[finite != 0]
            if nz.size and nz.size < 0.5 * finite.size:   # mostly zeros (criticality): scale on non-zero values
                lc.set_clim(0, np.percentile(nz, 98) or 1)
            else:
                lc.set_clim(np.nanpercentile(finite, 2), np.nanpercentile(finite, 98) or 1)
        fig.colorbar(lc, ax=ax, shrink=0.7, label=label)
    ax.add_collection(lc)
    j = [n for n in m.nodes() if n["type"] == "Junction"]
    if node_values:
        xs = [n["x"] for n in j]
        ys = [n["y"] for n in j]
        cs = np.array([node_values.get(n["id"], np.nan) for n in j], dtype=float)
        ok = np.isfinite(cs)
        sc = ax.scatter(np.array(xs)[ok], np.array(ys)[ok], c=cs[ok], s=7, cmap=SEQ, zorder=2, linewidths=0)
        if (~ok).any():
            ax.scatter(np.array(xs)[~ok], np.array(ys)[~ok], s=9, facecolors="none", edgecolors="#d03b3b", linewidths=0.8, zorder=2)
        fig.colorbar(sc, ax=ax, shrink=0.7, label=label)
    else:
        ax.scatter([n["x"] for n in j], [n["y"] for n in j], s=2, color=MUTED, zorder=2)
    if highlight_links:
        hs = [[pos[m.wn.get_link(lid).start_node_name], pos[m.wn.get_link(lid).end_node_name]] for lid in highlight_links if m.resolve_link(lid)]
        ax.add_collection(LineCollection(hs, linewidths=3, colors=ORANGE, zorder=3))
    if highlight_nodes:
        ax.scatter([pos[n][0] for n in highlight_nodes if n in pos], [pos[n][1] for n in highlight_nodes if n in pos], s=40,
                   facecolors="none", edgecolors=ORANGE, linewidths=1.5, zorder=4)
    for n in m.nodes():
        if n["type"] in ("Reservoir", "Tank"):
            ax.scatter([n["x"]], [n["y"]], marker="s" if n["type"] == "Reservoir" else "^", s=60, color=BLUE, edgecolors="white", zorder=5)
            ax.annotate(n["id"], (n["x"], n["y"]), xytext=(4, 4), textcoords="offset points", fontsize=7, color=INK)
    ax.set_aspect("equal")
    ax.autoscale()
    ax.axis("off")
    return _png(fig)


def hist_figure(values: np.ndarray, xlabel: str, limit: float | None = None, limit_label: str = "") -> bytes:
    fig, ax = plt.subplots(figsize=(6.4, 2.8))
    v = values[np.isfinite(values)]
    ax.hist(v, bins=24, color=BLUE, edgecolor="white", linewidth=1.0)
    if limit is not None:
        ax.axvline(limit, color="#d03b3b", linestyle="--", linewidth=1.2)
        ax.text(limit, ax.get_ylim()[1] * 0.95, f" {limit_label}", color="#d03b3b", fontsize=8, va="top")
    ax.set_xlabel(xlabel, fontsize=9, color=MUTED)
    ax.set_ylabel("count", fontsize=9, color=MUTED)
    _style(ax)
    return _png(fig)


def series_figure(xs, series: list[tuple[str, np.ndarray]], ylabel: str, limits: list[tuple[float, str]] | None = None) -> bytes:
    fig, ax = plt.subplots(figsize=(6.4, 2.6))
    for i, (name, ys) in enumerate(series):
        ax.plot(xs, ys, color=[BLUE, ORANGE][i % 2], linewidth=1.6, linestyle="-" if i == 0 else "--", label=name)
    for y, lab in limits or []:
        ax.axhline(y, color="#d03b3b", linestyle="--", linewidth=1.0)
        ax.text(xs[-1], y, f"{lab} ", color="#d03b3b", fontsize=7, ha="right", va="bottom")
    ax.set_xlabel("time (h)", fontsize=9, color=MUTED)
    ax.set_ylabel(ylabel, fontsize=9, color=MUTED)
    if len(series) > 1:
        ax.legend(frameon=False, fontsize=8)
    _style(ax)
    return _png(fig)


# ------------------------------------------------------------------- content
def build_document(session: Session, style: str = "technical", discussion: dict[str, Any] | None = None,
                   author: str = "", include: dict[str, bool] | None = None) -> Doc:
    """style: 'technical' (engineering report) or 'thesis' (Introduction/Methodology/Results/Discussion/Conclusions)."""
    from .tools import _ensure_sim
    inc = {"maps": True, "resilience": True, "assessment": True, **(include or {})}
    m = session.model
    s = m.summary()
    _ensure_sim(session)
    sim = session.sim
    sm = sim.summary
    crit = session.criteria
    val = validate(m)
    ch = T.characterization(m)
    ps = T.pipe_statistics(m)
    thesis = style == "thesis"
    doc = Doc(f"{'Hydraulic and reliability analysis' if thesis else 'Network analysis report'}: {s['name']}",
              f"{author + ' · ' if author else ''}Generated by WDN Studio on {time.strftime('%d %B %Y')}")
    sec = iter(range(1, 50))

    def H1(text):
        doc.h(1, f"{next(sec)}. {text}")

    # --------------------------------------------------------- 1 introduction / network
    H1("Introduction" if thesis else "Network description")
    doc.p(f"The {s['name']} network contains {s['nodes']:,} nodes ({s['junctions']:,} junctions, {s['reservoirs']} reservoir(s), "
          f"{s['tanks']} tank(s)) and {s['links']:,} links ({s['pipes']:,} pipes totalling {_f(s['total_pipe_length_km'], 2)} km, "
          f"{s['pumps']} pump(s), {s['valves']} valve(s)). The total base demand is {_f(s['total_base_demand_lps'])} L/s. "
          f"The model uses {s['headloss_formula']} headloss (roughness = {s['roughness_meaning']}) and was supplied in "
          f"{s['inp_flow_units']} flow units.")
    doc.table(["", "min", "median", "mean", "max"],
              [["Pipe length (m)"] + [_f(ps["length_m"].get(k)) for k in ("min", "median", "mean", "max")],
               ["Diameter (mm)"] + [_f(ps["diameter_mm"].get(k)) for k in ("min", "median", "mean", "max")],
               ["Roughness"] + [_f(ps["roughness"].get(k)) for k in ("min", "median", "mean", "max")]], "Table: pipe statistics")
    if inc["maps"]:
        doc.fig(map_figure(session), f"Figure: layout of {s['name']} (line width ∝ diameter; squares = reservoirs, triangles = tanks).")

    # --------------------------------------------------------- 2 methodology (thesis) / model check
    if thesis:
        H1("Methodology")
        doc.bullets([
            "Hydraulic simulation with EPANET 2.2 through the WNTR library (Python), "
            f"{'pressure-driven' if sim.settings.demand_model == 'PDD' else 'demand-driven'} demand, "
            f"{_f(sm['duration_h'], 1)} h extended period at {_f(sm['report_step_min'], 0)} min reporting steps.",
            "Graph analysis of the network topology with NetworkX (loops, bridges, articulation points, dead ends, centrality).",
            "Reliability analyses on a peak-hour snapshot: the network frozen at the hour of highest demand of a baseline run "
            "(demands, reservoir heads, tank levels and pump/valve statuses fixed), solved repeatedly with the EPANET toolkit. "
            "Snapshot pressures reproduce the full simulation at that hour (verified within 0.01 m).",
            "Pipe criticality (N-1): each pipe closed in turn, pressure-driven demand, ranking by undeliverable demand.",
            "Service assessment against user-defined criteria: " + (", ".join(f"{k.replace('_', ' ')} = {v:g}" for k, v in crit.to_dict().items()
                                                                     if v is not None and k != "label") or "none set") + ".",
            "All numbers in this report are computed by the engine; any LLM-written text is checked number by number.",
        ])
    H1("Model check")
    c = val["counts"]
    doc.p(f"The automatic model check found {c['error']} error(s), {c['warning']} warning(s) and {c['info']} note(s).")
    if val["issues"]:
        doc.table(["severity", "issue", "count"], [[i["severity"], i["message"], i["count"]] for i in val["issues"][:12]])

    # --------------------------------------------------------- results: topology
    H1("Results" if thesis else "Topology and redundancy")
    if thesis:
        doc.h(2, "Topology and redundancy")
    doc.bullets([
        f"Components: {ch['number_of_components']} ({'connected' if ch['connected'] else 'disconnected'}); "
        f"independent loops: {ch['cycle_rank_unique_connections']:,}; meshedness coefficient {_f(ch['meshedness_coefficient'], 3)}.",
        f"Single-link bridges: {ch['number_of_bridges']:,} ({ch['branched_fraction']:.1%} of links); articulation points: "
        f"{ch['number_of_articulation_points']:,}; dead-end junctions: {ch['number_of_dead_ends']:,}.",
        f"Junctions with no open path to a source: {ch['source_connectivity']['number_unsupplied']}.",
        f"Average degree {_f(ch['average_degree'], 2)}; degree distribution {ch['degree_distribution']}.",
    ])

    # --------------------------------------------------------- results: hydraulics
    doc.h(2 if thesis else 1, ("" if thesis else f"{next(sec)}. ") + "Hydraulic performance")
    p_ = sm.get("pressure_m", {})
    v_ = sm.get("velocity_ms", {})
    doc.p(f"Simulation {sim.id}: {sm['demand_model']}, {_f(sm['duration_h'], 1)} h, {sm['frames']} time steps"
          + (f", demand ×{sim.settings.demand_multiplier:g}" if sim.settings.demand_multiplier != 1 else "")
          + (f", closed links {', '.join(sim.settings.closed_links)}" if sim.settings.closed_links else "") + ".")
    rows = [["Minimum pressure (m)", _f(p_.get("min", {}).get("value")), f"{p_.get('min', {}).get('element', '')} at {_f(p_.get('min', {}).get('time_h'), 1)} h"],
            ["Maximum pressure (m)", _f(p_.get("max", {}).get("value")), str(p_.get("max", {}).get("element", ""))],
            ["Mean pressure (m)", _f(p_.get("mean")), ""],
            ["Maximum velocity (m/s)", _f(v_.get("max", {}).get("value"), 3), str(v_.get("max", {}).get("element", ""))],
            ["Junctions with negative pressure", _f(sm.get("junctions_with_negative_pressure")), ""],
            ["Pipes with flow reversal", _f(sm.get("pipes_with_flow_reversal")), ""]]
    for t in sm.get("tanks", []):
        rows.append([f"Tank {t['id']} level (m)", f"{_f(t['min_level_m'])}–{_f(t['max_level_m'])}", "empties" if t["hit_empty"] else ""])
    for pmp in sm.get("pumps", []):
        rows.append([f"Pump {pmp['id']} running (h) / energy (kWh)", f"{_f(pmp['hours_running'], 1)} / {_f(pmp.get('energy_kwh'))}", ""])
    if sm.get("todini_resilience"):
        rows.append(["Todini resilience index (mean / min)", f"{_f(sm['todini_resilience']['mean'], 3)} / {_f(sm['todini_resilience']['min'], 3)}", ""])
    doc.table(["indicator", "value", "where / when"], rows, "Table: hydraulic key performance indicators")
    j = [sim.node_index[n] for n in m.wn.junction_name_list]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        minp = np.nanmin(sim.node_data["pressure"][:, j], axis=0)
    if inc["maps"]:
        doc.fig(map_figure(session, node_values=dict(zip(m.wn.junction_name_list, minp)), label="minimum pressure (m)"),
                "Figure: minimum pressure at each junction over the run (hollow red = cut off from every source).")
    doc.fig(hist_figure(minp, "minimum pressure (m)", crit.min_pressure_m, f"min {crit.min_pressure_m:g} m" if crit.min_pressure_m else ""),
            "Figure: distribution of junction minimum pressures.")
    if len(sim.times_h) > 1:
        tot = np.nansum(sim.node_data["demand"][:, j], axis=1)
        doc.fig(series_figure(sim.times_h, [("total demand", tot)], "L/s"), "Figure: total network demand over the run.")
        for tname in m.wn.tank_name_list:
            tank = m.wn.get_node(tname)
            lvl = sim.node_data["head"][:, sim.node_index[tname]] - tank.elevation
            doc.fig(series_figure(sim.times_h, [(tname, lvl)], "level (m)", [(tank.min_level, "min"), (tank.max_level, "max")]),
                    f"Figure: tank {tname} level.")
    if crit.is_set():
        checks = sim.evaluate_criteria(m, crit)
        names = {"low_pressure": "below minimum pressure", "high_pressure": "above maximum pressure", "high_velocity": "above maximum velocity",
                 "low_velocity": "peak velocity below minimum", "high_headloss": "above maximum headloss"}
        doc.table(["criterion", "limit", "elements failing"],
                  [[names[k], _f(next((x for kk, x in v.items() if kk.startswith("limit")), None)), _f(v["count"])]
                   for k, v in checks.items() if k in names], f"Table: compliance with design criteria ({crit.label})")

    # --------------------------------------------------------- results: resilience
    R = session.results
    if inc["resilience"] and any(k in R for k in ("pipe_criticality", "valve_segments", "fire_flow", "demand_growth", "leak_scenario",
                                                    "water_quality_chlorine", "water_quality_source_shares", "what_if")):
        doc.h(2 if thesis else 1, ("" if thesis else f"{next(sec)}. ") + "Reliability and resilience")
        if "pipe_criticality" in R:
            r = R["pipe_criticality"]
            doc.p(f"Pipe criticality (N-1): {r['elements_tested']:,} links were closed one at a time at the peak hour (required pressure "
                  f"{r['required_pressure_m']:g} m, peak demand {_f(r['total_demand_lps'])} L/s). {r['critical_count']} links cause a supply "
                  f"shortfall or push junctions below the required pressure; {r['no_significant_impact']} have no significant impact.")
            doc.table(["rank", "link", "shortfall (L/s)", "% of demand", "junctions < required", "max drop (m)", "bridge"],
                      [[t["rank"], t["id"], _f(t["demand_shortfall_lps"], 3), _f(t["shortfall_pct"]), t["junctions_newly_below_req"],
                        _f(t["max_pressure_drop_m"], 1), "yes" if t.get("is_bridge") else ""] for t in r["top"][:10]],
                      "Table: most critical links")
            lay = session.layers.get("criticality")
            if inc["maps"] and lay:
                doc.fig(map_figure(session, link_values=lay["values"], label="shortfall if closed (L/s)",
                                   highlight_links=[t["id"] for t in r["top"][:5] if t["demand_shortfall_lps"] > 0]),
                        "Figure: demand shortfall caused by closing each pipe (top 5 in orange).")
        if "valve_segments" in R:
            r = R["valve_segments"]
            doc.p(f"Valve isolation segments: {r['segments']:,} segments from {r['valves']:,} valves; {r['assumption']} "
                  f"{r['segments_cutting_off_others']} segments also cut off other areas when isolated.")
            doc.table(["segment", "nodes", "valves to close", "junctions without water", "demand lost (L/s)"],
                      [[t["segment"], t["nodes"], t["valves_to_close"], t["junctions_without_water"],
                        _f(t.get("demand_shortfall_lps", t["base_demand_lost_lps"]), 3)] for t in r["top"][:8]])
        if "fire_flow" in R:
            r = R["fire_flow"]
            if "weakest" in r:
                doc.p(f"Fire flow at {r['residual_pressure_m']:g} m residual: median {_f(r['median_lps'])} L/s, lowest {_f(r['min_lps'])} L/s over "
                      f"{r['nodes_tested']:,} junctions" + (f"; {r['below_required']} cannot supply {r['required_flow_lps']:g} L/s." if r.get("required_flow_lps") else "."))
            else:
                doc.p(f"Fire flow at {r['id']}: up to {_f(r['available_lps'])} L/s while keeping {r['residual_pressure_m']:g} m.")
        if "demand_growth" in R and R["demand_growth"].get("capacity_factor") not in (None,):
            r = R["demand_growth"]
            if isinstance(r.get("capacity_factor"), (int, float)):
                doc.p(f"Demand growth capacity: demand can grow to {r['capacity_factor']:g}× today's peak (+{r['growth_headroom_pct']:g}%) before "
                      f"the minimum pressure falls below {r['min_pressure_m']:g} m; first junctions to fail: {', '.join(r['first_failing_nodes'][:6])}.")
        if "what_if" in R:
            r = R["what_if"]
            doc.p(f"What-if ({r['scenario']}): impact {r['severity']}; {r['junctions_newly_cut_off']} junctions cut off, "
                  f"{r['junctions_newly_below_required']} newly below {r['required_pressure_m']:g} m"
                  + (f"; demand delivered {_f(r['supply']['baseline_delivered_pct'], 1)}% → {_f(r['supply']['scenario_delivered_pct'], 1)}%." if r.get("supply") else "."))
        if "leak_scenario" in R:
            r = R["leak_scenario"]
            lk = r.get("leakage") or {}
            doc.p(f"Leak scenario ({_f(r['diameter_mm'], 0)} mm at {', '.join(r['elements'])}): mean leak flow {_f(lk.get('mean_lps'), 3)} L/s, "
                  f"{_f(lk.get('volume_m3'), 1)} m³ lost over {_f(r['duration_h'], 0)} h ({_f(lk.get('share_of_supply_pct'), 1)}% of supply); "
                  f"largest mean-pressure drop {_f(r['pressure_change']['max_decrease']['value'], 3)} m.")
        if "water_quality_chlorine" in R:
            cl = R["water_quality_chlorine"].get("chlorine_mgl") or {}
            doc.p(f"Chlorine residual (second half of the run): mean {_f(cl.get('mean_in_second_half'), 3)} mg/L, lowest "
                  f"{_f(cl.get('min_in_second_half'), 3)} mg/L; {cl.get('junctions_below_0_2')} junctions below 0.2 mg/L.")
        if "water_quality_source_shares" in R:
            r = R["water_quality_source_shares"]
            doc.table(["source", "type", "junctions mostly supplied", "junctions receiving > 5%"],
                      [[x["source"], x["type"], x["junctions_mostly_supplied"], x["junctions_over_5pct"]] for x in r["sources"]],
                      "Table: share of water from each source")

    # --------------------------------------------------------- assessment + discussion
    assessment = R.get("assess_network")
    if inc["assessment"] and assessment is None:
        from .insight import assess_network
        assessment = assess_network(session)
        R["assess_network"] = assessment
    H1("Discussion" if thesis else "Assessment")
    if assessment:
        for kind, label in (("weakness", "Weaknesses"), ("strength", "Strengths"), ("note", "Notes")):
            items = [f"{f['area']}: {f['text']}" for f in assessment["findings"] if f["kind"] == kind]
            if items:
                doc.h(3, label)
                doc.bullets(items)
    if discussion and discussion.get("text"):
        doc.h(3, "Interpretation")
        for para in [x.strip() for x in discussion["text"].split("\n\n") if x.strip()]:
            doc.p(para.replace("**", ""))
        v = discussion.get("verification") or {}
        doc.p(f"Text written by {discussion.get('model', 'an LLM')} from the verified results above; {v.get('numbers_checked', 0)} numbers "
              f"checked against the engine" + (f", {len(v.get('unverified', []))} could not be matched (marked ⚠)." if v.get("unverified") else ", all matched."),
              italic=True)
    if thesis:
        H1("Conclusions")
        concl = [f["text"] for f in (assessment or {}).get("findings", []) if f["kind"] == "weakness"][:4]
        doc.bullets(concl or ["No major weaknesses were identified with the thresholds used."])
        if assessment and assessment.get("next_steps"):
            doc.p("Recommended further work:")
            doc.bullets(assessment["next_steps"])

    # --------------------------------------------------------- assumptions
    H1("Assumptions and limitations")
    notes = [n for n in sm.get("notes", [])]
    doc.bullets(notes + [
        "Results depend on the input model (demands, patterns, roughness, controls); no calibration against field data was performed.",
        "Snapshot-based analyses freeze tank levels at the peak hour, so slower effects of failures (tanks draining) are not captured.",
        "Valve isolation segments use an assumed valve layout unless valves are defined in the model." if "valve_segments" in R else None,
        f"Assessment thresholds: {assessment['thresholds']}" if assessment else None,
        "Software: WDN Studio (WNTR / EPANET 2.2, NetworkX).",
    ])
    return doc


# ------------------------------------------------------------------- renderers
def to_docx(doc: Doc) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor, Cm
    d = Document()
    st = d.styles["Normal"]
    st.font.name = "Calibri"
    st.font.size = Pt(10.5)
    for sec in d.sections:
        sec.left_margin = sec.right_margin = Cm(2.2)
    t = d.add_heading(doc.title, 0)
    if doc.subtitle:
        p = d.add_paragraph(doc.subtitle)
        p.runs[0].font.color.rgb = RGBColor(0x52, 0x51, 0x4E)
    for b in doc.blocks:
        kind = b[0]
        if kind == "h":
            d.add_heading(b[2], level=min(b[1], 3))
        elif kind == "p":
            p = d.add_paragraph(b[1])
            if b[2]:
                for r in p.runs:
                    r.italic = True
        elif kind == "ul":
            for item in b[1]:
                d.add_paragraph(item, style="List Bullet")
        elif kind == "table":
            headers, rows, caption = b[1], b[2], b[3]
            tbl = d.add_table(rows=1, cols=len(headers))
            tbl.style = "Light Grid Accent 1"
            for i, h in enumerate(headers):
                tbl.rows[0].cells[i].text = str(h)
            for row in rows:
                cells = tbl.add_row().cells
                for i, v in enumerate(row):
                    cells[i].text = str(v)
            if caption:
                cp = d.add_paragraph(caption)
                cp.runs[0].italic = True
                cp.runs[0].font.size = Pt(9)
        elif kind == "img":
            d.add_picture(io.BytesIO(b[1]), width=Cm(15.5))
            d.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
            cp = d.add_paragraph(b[2])
            cp.runs[0].italic = True
            cp.runs[0].font.size = Pt(9)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def to_pdf(doc: Doc) -> bytes:
    from xml.sax.saxutils import escape
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Image, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.lib.utils import ImageReader
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontName="Helvetica", fontSize=9.5, leading=13)
    small = ParagraphStyle("s", parent=body, fontSize=8, leading=10, textColor=colors.HexColor("#52514e"))
    cap = ParagraphStyle("c", parent=small, fontName="Helvetica-Oblique")
    hs = {1: ParagraphStyle("h1", parent=ss["Heading1"], fontSize=15, spaceBefore=12, textColor=colors.HexColor("#0b0b0b")),
          2: ParagraphStyle("h2", parent=ss["Heading2"], fontSize=12.5, spaceBefore=8),
          3: ParagraphStyle("h3", parent=ss["Heading3"], fontSize=10.5, spaceBefore=6)}

    def safe(t: str) -> str:   # reportlab's base fonts lack some glyphs
        return escape(str(t)).replace("−", "-").replace("≥", "&gt;=").replace("≤", "&lt;=").replace("∝", "~").replace("⚠", "(!)").replace("³", "3").replace("→", "-&gt;")

    story = [Paragraph(safe(doc.title), ss["Title"]), Paragraph(safe(doc.subtitle), small), Spacer(1, 10)]
    width = A4[0] - 4 * cm
    for b in doc.blocks:
        kind = b[0]
        if kind == "h":
            story.append(Paragraph(safe(b[2]), hs[min(b[1], 3)]))
        elif kind == "p":
            story.append(Paragraph(f"<i>{safe(b[1])}</i>" if b[2] else safe(b[1]), body))
            story.append(Spacer(1, 4))
        elif kind == "ul":
            story.append(ListFlowable([ListItem(Paragraph(safe(i), body), leftIndent=10) for i in b[1]], bulletType="bullet", start="•", leftIndent=12))
            story.append(Spacer(1, 4))
        elif kind == "table":
            headers, rows, caption = b[1], b[2], b[3]
            data = [[Paragraph(f"<b>{safe(h)}</b>", small) for h in headers]] + [[Paragraph(safe(v), small) for v in r] for r in rows]
            t = Table(data, repeatRows=1, colWidths=[width / len(headers)] * len(headers))
            t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0efec")),
                                   ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#dedcd6")),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            story += [t] + ([Paragraph(safe(caption), cap)] if caption else []) + [Spacer(1, 8)]
        elif kind == "img":
            ir = ImageReader(io.BytesIO(b[1]))
            iw, ih = ir.getSize()
            w = min(width, 15.5 * cm)
            story += [Image(io.BytesIO(b[1]), width=w, height=w * ih / iw), Paragraph(safe(b[2]), cap), Spacer(1, 8)]
    buf = io.BytesIO()

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#8a8984"))
        canvas.drawString(2 * cm, 1.2 * cm, "WDN Studio report")
        canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm, f"page {d.page}")
        canvas.restoreState()

    SimpleDocTemplate(buf, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=1.8 * cm, bottomMargin=2 * cm,
                      title=doc.title).build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def llm_discussion(session: Session, facts: dict[str, Any], provider: str | None, model: str | None, api_key: str | None) -> dict[str, Any] | None:
    """Ask the configured LLM for a short engineering discussion of verified facts; check its numbers."""
    from . import llm
    from .verify import check, evidence_numbers
    import json as _json
    cands = llm._candidates(provider, model, api_key, None, None)
    if not cands:
        return None
    c = cands[0]
    prompt = ("Write a concise engineering discussion (3 short paragraphs, plain prose, no headings, no bullet lists) of this water "
              "distribution network analysis for a technical report. Use ONLY numbers that appear in the facts; do not invent values. "
              "Interpret what the results mean for reliability, pressure service and water quality, and note limitations.\n\nFACTS:\n"
              + _json.dumps(facts, default=str)[:12000])
    try:
        data = llm._post(c["url"], c["key"], {"model": c["model"], "temperature": 0.3,
                                              "messages": [{"role": "user", "content": prompt}]})
    except Exception as error:  # report still builds without it
        return {"text": "", "error": str(error)}
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    v = check(text, evidence_numbers(facts))
    for tok in v["unverified"]:
        text = text.replace(tok, f"{tok} ⚠")
    return {"text": text.strip(), "model": f"{c['label']} ({c['model']})", "verification": v}


def build_report(session: Session, fmt: str = "docx", style: str = "technical", author: str = "",
                 llm_cfg: dict[str, Any] | None = None, include: dict[str, bool] | None = None, progress=lambda p, m: None) -> tuple[bytes, str]:
    progress(0.1, "Collecting results…")
    discussion = None
    if llm_cfg and llm_cfg.get("provider") not in (None, "", "rules"):
        progress(0.2, "Preparing the assessment…")
        from .insight import assess_network
        if "assess_network" not in session.results:
            session.results["assess_network"] = assess_network(session)
        facts = {"summary": session.model.summary(), "simulation": {k: v for k, v in session.sim.summary.items() if k != "settings"},
                 "results": {k: v for k, v in session.results.items()}}
        progress(0.35, "Asking the LLM for a discussion (numbers will be checked)…")
        discussion = llm_discussion(session, facts, llm_cfg.get("provider"), llm_cfg.get("model"), llm_cfg.get("api_key"))
    progress(0.5, "Drawing figures and tables…")
    doc = build_document(session, style, discussion, author, include)
    progress(0.85, f"Writing {fmt.upper()}…")
    data = to_pdf(doc) if fmt == "pdf" else to_docx(doc)
    return data, ("application/pdf" if fmt == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
