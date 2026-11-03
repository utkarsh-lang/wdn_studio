"""Benchmark question set with engine-computed ground truth.

Questions are generated per network from templates; the specific elements (highest junction, longest
pipe, critical pipe …) are chosen programmatically, so the set works for any EPANET model and no answer
is typed in by hand.  Each item says which numbers / IDs / keywords a correct answer must contain and
which tools count as a correct tool choice.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from wdn_engine import insight as I
from wdn_engine import resilience as R
from wdn_engine import spatial as SP
from wdn_engine import temporal as TM
from wdn_engine import topology as T
from wdn_engine.hydraulics import SimSettings
from wdn_engine.model import NetworkModel
from wdn_engine.session import Session
from wdn_engine.tables import query

CATEGORIES = ["lookup", "pipes", "topology", "simulation", "time", "combined", "spatial", "resilience", "what_if",
              "diagnosis", "definition", "unanswerable"]
REFUSAL = ["not available", "not in the model", "no information", "not included", "does not contain", "doesn't contain",
           "not stored", "no data", "isn't available", "is not available", "not part of", "cannot be determined",
           "can't be determined", "not provided", "not specified", "unknown", "no such", "does not include", "doesn't include",
           "not recorded", "not contain"]


@dataclass
class Item:
    id: str
    network: str
    category: str
    question: str
    numbers: list[dict[str, Any]] = field(default_factory=list)   # {"value", "rel", "abs", "alts", "label"}
    ids: list[str] = field(default_factory=list)                  # all must appear
    keywords: list[list[str]] = field(default_factory=list)       # each group: at least one must appear
    tools: list[str] = field(default_factory=list)                # any of these = correct tool choice
    no_numbers: bool = False                                      # unanswerable: must not invent numbers
    split: str = "dev"                                            # dev = used while building the rules; heldout = paraphrase never tuned on

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def N(value: float, rel: float = 0.01, abs_: float = 0.0, label: str = "", alts: list[float] | None = None) -> dict[str, Any]:
    return {"value": float(value), "rel": rel, "abs": abs_, "label": label, "alts": [float(a) for a in (alts or [])]}


def C(value: int, label: str = "") -> dict[str, Any]:   # exact count
    return {"value": float(value), "rel": 0.0, "abs": 0.0, "label": label, "alts": []}


def build(model: NetworkModel, prefix: str) -> list[Item]:
    warnings.simplefilter("ignore")
    wn = model.wn
    name = model.name
    s0 = model.summary()
    items: list[Item] = []
    k = iter(range(1, 999))

    def add(cat, q, alt=None, **kw):
        i = next(k)
        items.append(Item(f"{prefix}-{i:03d}", name, cat, q, **kw))
        if alt:  # held-out paraphrase: same expected answer, wording never used to develop the rules
            items.append(Item(f"{prefix}-{i:03d}H", name, cat, alt, split="heldout", **kw))

    juncs = list(wn.junction_name_list)
    pipes = model.pipes()
    elev = {j: wn.get_node(j).elevation for j in juncs}
    j_high = max(juncs, key=lambda j: elev[j])
    p_long = max(pipes, key=lambda p: p["length_m"])
    dem = {j: model.total_base_demand(j) * 1000 for j in juncs}
    j_dem = max(juncs, key=lambda j: dem[j])
    src = (list(wn.reservoir_name_list) or list(wn.tank_name_list))[0]

    # ---------------------------------------------------------------- lookup
    add("lookup", "How many junctions does the network have?", alt="Count the junction nodes in this model.", numbers=[C(s0["junctions"])], tools=["network_summary", "query_table"])
    add("lookup", "How many tanks and how many reservoirs are there?", numbers=[C(s0["tanks"]), C(s0["reservoirs"])], tools=["network_summary", "query_table"])
    add("lookup", "What is the total length of all pipes?", alt="What's the combined length of the pipe network?", numbers=[N(s0["total_pipe_length_km"], alts=[s0["total_pipe_length_km"] * 1000])],
        tools=["network_summary", "pipe_statistics", "query_table"])
    add("lookup", "What is the total base demand of the network in L/s?", alt="Sum of all base demands in the model?", numbers=[N(s0["total_base_demand_lps"])], tools=["network_summary", "query_table"])
    add("lookup", f"What is the elevation of junction {j_high}?", alt=f"Tell me the ground elevation at {j_high}.", numbers=[N(elev[j_high])], tools=["element_info", "query_table"])
    add("lookup", f"What is the diameter of pipe {p_long['id']} in mm?", alt=f"How wide is pipe {p_long['id']}?", numbers=[N(p_long["diameter_mm"])], tools=["element_info", "query_table"])
    add("lookup", f"How long is pipe {p_long['id']}?", numbers=[N(p_long["length_m"])], tools=["element_info", "query_table"])
    add("lookup", f"What is the base demand at junction {j_dem}?", numbers=[N(dem[j_dem])], tools=["element_info", "query_table"])

    # ---------------------------------------------------------------- pipes / filters
    ps = T.pipe_statistics(model)
    d_med = ps["diameter_mm"]["median"]
    l_p90 = float(round(ps["length_m"]["p90"], -1)) or ps["length_m"]["p90"]
    n_small = sum(1 for p in pipes if p["diameter_mm"] < d_med - 0.5)
    n_long = sum(1 for p in pipes if p["length_m"] > l_p90)
    # bounds placed between diameter classes so inch-converted sizes (e.g. 457.2 mm) are never ambiguous
    ds = sorted({p["diameter_mm"] for p in pipes})
    d1 = math.floor(ds[0]) - 1 if ds[0] > 1 else 0
    d2 = math.floor(ds[len(ds) // 2]) + 1
    n_between = sum(1 for p in pipes if d1 <= p["diameter_mm"] <= d2)
    add("pipes", "What is the average pipe diameter in mm?", alt="Mean diameter of the pipes?", numbers=[N(ps["diameter_mm"]["mean"])], tools=["pipe_statistics", "query_table"])
    add("pipes", f"How many pipes have a diameter smaller than {d_med:g} mm?", alt=f"Count pipes under {d_med:g} mm diameter.", numbers=[C(n_small)], tools=["filter_pipes", "query_table"])
    add("pipes", f"How many pipes are longer than {l_p90:g} m?", alt=f"Give me the number of pipes exceeding {l_p90:g} m in length.", numbers=[C(n_long)], tools=["filter_pipes", "query_table"])
    add("pipes", f"How many pipes have a diameter between {d1:g} and {d2:g} mm?", numbers=[C(n_between)], tools=["filter_pipes", "query_table"])
    add("pipes", "Which is the longest pipe?", alt="Name the pipe with the greatest length.", ids=[p_long["id"]], tools=["pipe_statistics", "query_table", "filter_pipes", "rank_elements"])
    add("pipes", "What is the smallest pipe diameter in the network?", numbers=[N(ps["diameter_mm"]["min"])], tools=["pipe_statistics", "query_table"])

    # ---------------------------------------------------------------- topology
    ch = T.characterization(model)
    add("topology", "How many dead-end junctions are there?", alt="How many junctions have only one connection?", numbers=[C(ch["number_of_dead_ends"])], tools=["topology_characterization", "query_table"])
    add("topology", "How many single-link bridges does the network have?", alt="How many links would split the network if removed individually?", numbers=[C(ch["number_of_bridges"])], tools=["topology_characterization", "link_metric"])
    add("topology", "How many articulation points are there?", alt="Number of cut vertices in the graph?", numbers=[C(ch["number_of_articulation_points"])], tools=["topology_characterization", "node_metric", "query_table"])
    add("topology", "How many independent loops does the network have?", numbers=[C(ch["cycle_rank_unique_connections"], "loops"),
                                                                                 ], tools=["topology_characterization"])
    add("topology", "Is every junction connected to a source? How many junctions have no path to a reservoir or tank?",
        numbers=[C(ch["source_connectivity"]["number_unsupplied"])], tools=["topology_characterization", "validation_report"])
    sp = T.shortest_path(model, src, j_high, True)
    if sp.get("success"):
        add("topology", f"What is the shortest pipe distance in metres from {src} to {j_high}?", alt=f"Distance along the pipes between {src} and {j_high}?", numbers=[N(sp["total_length_m"])], tools=["shortest_path"])
    bt = T.node_metric(model, "betweenness_centrality")
    add("topology", "Which node has the highest betweenness centrality?", alt="Most central node by betweenness?", ids=[bt["top"][0]["id"]], tools=["node_metric", "rank_elements"])

    # ---------------------------------------------------------------- simulation (default full-duration run)
    s = Session(model)
    sim = s.simulate(SimSettings())
    sm = sim.summary
    dur = sm["duration_h"]
    pmin = sm["pressure_m"]["min"]
    vmax = sm["velocity_ms"]["max"]
    add("simulation", "Which junction has the lowest pressure in a simulation of the full file duration, and what is that pressure?", alt="Where is pressure worst over the whole simulation, and how low does it get?",
        numbers=[N(pmin["value"])], ids=[pmin["element"]], tools=["rank_elements", "simulation_results", "run_simulation", "query_table"])
    add("simulation", "What is the highest pressure anywhere in the network during the full-duration simulation?",
        numbers=[N(sm["pressure_m"]["max"]["value"])], tools=["rank_elements", "simulation_results", "run_simulation", "query_table"])
    add("simulation", "Which pipe has the highest velocity in the full-duration simulation and what is it?", alt="Peak velocity in the full simulation: which pipe and how fast?",
        numbers=[N(vmax["value"])], ids=[vmax["element"]], tools=["rank_elements", "simulation_results", "run_simulation", "query_table"])
    add("simulation", "What is the mean junction pressure over the full-duration simulation?", alt="Average network pressure in the full run?", numbers=[N(sm["pressure_m"]["mean"])],
        tools=["simulation_results", "run_simulation", "query_table"])
    for t in sm.get("tanks", [])[:1]:
        add("simulation", f"What are the lowest and highest levels of tank {t['id']} in the full-duration simulation?",
            numbers=[N(t["min_level_m"]), N(t["max_level_m"])], tools=["tank_events", "simulation_results", "run_simulation"])
    for p in sm.get("pumps", [])[:1]:
        add("simulation", f"For how many hours does pump {p['id']} run in the full-duration simulation?",
            numbers=[N(p["hours_running"], rel=0.02)], tools=["simulation_results", "run_simulation"])

    # ---------------------------------------------------------------- time
    v8 = TM.value_at(s, pmin["element"], "08:00", "pressure")
    if v8.get("success"):
        add("time", f"What is the pressure at junction {pmin['element']} at 08:00 on day 1?", alt=f"Pressure at {pmin['element']} at 8 am on the first day?", numbers=[N(v8["values"]["pressure"])],
            tools=["value_at_time"])
    we = TM.when_extreme(s, "demand", "max")
    if we.get("success"):
        hhmm = we["clock"].split(", ")[1]
        add("time", "At what time is the total network demand highest?", alt="Time of peak total consumption?", ids=[hhmm], numbers=[N(we["value"])], tools=["when_extreme"])
    wp = TM.when_extreme(s, "pressure", "min", pmin["element"])
    if wp.get("success"):
        add("time", f"When is the pressure at junction {pmin['element']} lowest (day and time)?", ids=[wp["clock"].split(", ")[1]],
            tools=["when_extreme", "value_at_time"])

    # ---------------------------------------------------------------- combined (query_table)
    e_med = float(np.median(list(elev.values())))
    from wdn_engine.tables import node_table, link_table
    nt = node_table(s)
    jt = nt[nt["type"] == "Junction"]
    p_med = float(round(jt["min_pressure_m"].median()))
    e_thr = float(round(e_med))
    cnt = int(((jt["elevation_m"] > e_thr) & (jt["min_pressure_m"] < p_med)).sum())
    add("combined", f"How many junctions are above {e_thr:g} m elevation and have a minimum pressure below {p_med:g} m?", alt=f"Count junctions higher than {e_thr:g} m elevation whose pressure ever falls under {p_med:g} m.",
        numbers=[C(cnt)], tools=["query_table"])
    lt = link_table(s)
    pt = lt[lt["type"] == "Pipe"]
    v_thr = float(round(pt["max_velocity_ms"].quantile(0.75), 2))
    cnt2 = int(((pt["diameter_mm"] <= d_med + 0.5) & (pt["max_velocity_ms"] > v_thr)).sum())
    add("combined", f"How many pipes with a diameter of at most {d_med:g} mm have a peak velocity above {v_thr:g} m/s?",
        numbers=[C(cnt2)], tools=["query_table"])
    rev_len = float(pt.loc[pt["flow_reverses"], "length_m"].sum())
    add("combined", "What is the total length of pipes whose flow direction reverses during the full-duration simulation?",
        numbers=[N(rev_len, rel=0.01, alts=[rev_len / 1000])], tools=["query_table"])
    lo, hi = float(round(jt["min_pressure_m"].quantile(0.25))), float(round(jt["min_pressure_m"].quantile(0.5)))
    cnt3 = int(((jt["min_pressure_m"] >= lo) & (jt["min_pressure_m"] <= hi)).sum())
    add("combined", f"How many junctions have a minimum pressure between {lo:g} and {hi:g} m?", numbers=[C(cnt3)], tools=["query_table"])

    # ---------------------------------------------------------------- spatial
    d_all = sorted(SP.nodes_within(s, src, 1e9)["nodes"], key=lambda r: r["distance_m"])
    if len(d_all) > 12:
        radius = float(math.ceil(d_all[9]["distance_m"] / 10.0) * 10)
        nw = SP.nodes_within(s, src, radius)
        add("spatial", f"How many nodes are within {radius:g} m of {src}, measured along the pipes?", alt=f"How many nodes lie no more than {radius:g} m from {src} along the network?", numbers=[C(nw["count"])], tools=["nodes_within"])
    up = SP.trace_flow(s, pmin["element"], "upstream")
    if up.get("success"):
        add("spatial", f"Which reservoirs or tanks supply junction {pmin['element']} (following the average flow)?", alt=f"Where does the water at {pmin['element']} come from?",
            ids=list(up["sources_reached"]), tools=["trace_flow", "diagnose_pressure"])

    # ---------------------------------------------------------------- resilience
    crit = R.pipe_criticality(s, 20.0)
    top = crit["top"][0]
    if top["demand_shortfall_lps"] > 0:
        add("resilience", "Which single pipe is the most critical (largest demand shortfall if it is closed, 20 m required pressure)?", alt="If one pipe fails, which pipe hurts supply the most (20 m required pressure)?",
            ids=[top["id"]], tools=["pipe_criticality"])
    g = R.demand_growth(s, 20.0, verify_eps=False)
    if isinstance(g.get("capacity_factor"), (int, float)):
        add("resilience", "By what factor can demand grow before the minimum pressure drops below 20 m?", alt="How much headroom for demand growth is there before pressure falls under 20 m?",
            numbers=[N(g["capacity_factor"], rel=0.02)], tools=["demand_growth"])
    ff = R.fire_flow(s, j_high, 14.0)
    if ff.get("success"):
        add("resilience", f"How much fire flow (L/s) is available at junction {j_high} with 14 m residual pressure?", alt=f"Fire flow capacity at {j_high} keeping 14 m residual?",
            numbers=[N(ff["available_lps"], rel=0.02)], tools=["fire_flow"])
    vs = R.valve_segments(s, "strategic", 2, hydraulic=False)
    add("resilience", "Assuming 2 valves per junction, how many valve isolation segments does the network have?",
        numbers=[C(vs["segments"])], tools=["valve_segments"])

    # ---------------------------------------------------------------- what-if
    if top["demand_shortfall_lps"] > 0:
        w = I.what_if(Session(model), closed_links=[top["id"]])
        add("what_if", f"What happens if pipe {top['id']} fails? How many junctions lose their water supply completely?", alt=f"Suppose pipe {top['id']} bursts — how many junctions end up with no water at all?",
            numbers=[C(w["junctions_newly_cut_off"])], tools=["what_if", "run_simulation", "compare_runs"])
    if wn.pump_name_list:
        pid = wn.pump_name_list[0]
        w2 = I.what_if(Session(model), closed_links=[pid])
        empt = [t["id"] for t in w2["tanks"] if t["empties_in_scenario"]]
        kw = [["empt", "drain", "runs dry", "0.0 m", "0 m"]] if empt else [["does not empty", "doesn't empty", "never empties", "not empty", "stays above"]]
        add("what_if", f"If pump {pid} fails, does any tank run empty?", alt=f"Would a tank drain completely if pump {pid} stopped working?", keywords=kw, ids=empt, tools=["what_if", "run_simulation", "tank_events"])

    # ---------------------------------------------------------------- diagnosis
    d = I.diagnose_pressure(Session(model), pmin["element"])
    if d.get("success") and d["source"]:
        cause = d["likely_causes"][0].lower()
        words = (["elevation", "high ground", "height", "elevated"] if ("elevation" in cause or "high ground" in cause)
                 else ["friction", "headloss", "head loss"] if "friction" in cause else ["valve", "prv"] if "valve" in cause
                 else ["elevation", "friction", "valve"])
        add("diagnosis", f"Why is the pressure low at junction {pmin['element']}?", alt=f"What causes the low pressure at {pmin['element']}?", keywords=[words], tools=["diagnose_pressure"])

    # ---------------------------------------------------------------- definitions (built-in glossary)
    add("definition", "What is pressure-driven analysis (PDD)?", alt="Explain pressure-driven demand.", keywords=[["pressure"], ["demand", "delivered"]], tools=["explain_term"])
    add("definition", "What is a bridge in a water network graph?", alt="Define a bridge link.", keywords=[["remov", "split", "disconnect", "isolat"]], tools=["explain_term"])
    add("definition", "What does the Todini resilience index measure?", keywords=[["surplus", "excess", "spare"], ["power", "energy", "pressure", "head"]],
        tools=["explain_term"])

    # ---------------------------------------------------------------- unanswerable (data not in an EPANET model)
    add("unanswerable", f"What material is pipe {p_long['id']} made of?", alt=f"Who manufactured pipe {p_long['id']}?", keywords=[REFUSAL], no_numbers=True)
    add("unanswerable", f"In which year was pipe {p_long['id']} installed?", keywords=[REFUSAL], no_numbers=True)
    add("unanswerable", "How many customers are connected to the network?", alt="How much would it cost to replace the longest pipe?", keywords=[REFUSAL], no_numbers=True)
    return items


def build_all(models: dict[str, NetworkModel]) -> list[Item]:
    out: list[Item] = []
    for prefix, m in models.items():
        out += build(m, prefix)
    return out
