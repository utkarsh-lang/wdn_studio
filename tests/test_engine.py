"""Engine tests on bundled EPANET examples and (if present) L-TOWN."""

import os
from pathlib import Path

import pytest
import wntr

from wdn_engine import topology as T
from wdn_engine.criteria import DesignCriteria
from wdn_engine.hydraulics import SimSettings, run_simulation
from wdn_engine.model import NetworkModel
from wdn_engine.router import parse_pipe_conditions, route
from wdn_engine.session import Session
from wdn_engine.tools import call_tool
from wdn_engine.validation import validate

LIB = Path(wntr.__file__).parent / "library" / "networks"
LTOWN = Path(os.getenv("LTOWN_INP", Path(__file__).parent.parent / "samples" / "L-TOWN.inp"))


@pytest.fixture(scope="module")
def net3():
    return NetworkModel.from_inp(str(LIB / "Net3.inp"))


@pytest.fixture(scope="module")
def ltown():
    if not LTOWN.is_file():
        pytest.skip("L-TOWN.inp not available")
    return NetworkModel.from_inp(str(LTOWN))


def test_summary_units(net3):
    s = net3.summary()
    assert s["nodes"] == 97 and s["pipes"] == 117 and s["pumps"] == 2
    assert s["headloss_formula"] == "H-W"


def test_parallel_links_not_bridges():
    wn = wntr.network.WaterNetworkModel(str(LIB / "Net1.inp"))
    a, b = wn.get_link("12").start_node_name, wn.get_link("12").end_node_name
    wn.add_pipe("12dup", a, b, length=100, diameter=0.2, roughness=100)
    m = NetworkModel(wn)
    ids = {x["link_id"] for x in T.characterization(m)["bridges"]}
    assert "12" not in ids and "12dup" not in ids


def test_multi_category_demand(ltown):
    # n1 has three demand categories (0, 0, 0.66 m3/h); WNTR's base_demand reports only the first
    assert ltown.wn.get_node("n1").base_demand == 0
    assert ltown.total_base_demand("n1") == pytest.approx(0.66024 / 3600, rel=1e-4)


def test_dead_ends_exclude_sources(ltown):
    c = T.characterization(ltown)
    assert not set(c["dead_end_junctions"]) & set(ltown.sources)
    assert c["connected"] and c["source_connectivity"]["number_unsupplied"] == 0


def test_filter_defaults_and_tolerance(ltown):
    conds = parse_pipe_conditions("pipes with diameter less than 100")
    assert conds == [{"field": "diameter_mm", "op": "<", "value": 100.0}]  # mm, not metres
    assert parse_pipe_conditions("pipes smaller than 4 inch")[0]["value"] == pytest.approx(101.6)
    assert len(parse_pipe_conditions("diameter between 100 and 150 mm")) == 2
    eq = T.filter_pipes(ltown, [T.PipeCondition("diameter_mm", "=", 100)])
    assert eq["number_of_matching_pipes"] == 705


def test_router_does_not_misroute(ltown):
    s = Session(ltown)
    assert route(s, "what is the average pipe diameter?")[0][0] == "pipe_statistics"
    assert route(s, "which node has the highest degree?")[0][0] == "node_metric"
    assert route(s, "tell me about pipe p235")[0] == ("element_info", {"kind": "link", "id": "p235"})
    assert isinstance(route(s, "hello there"), str)


def test_weighted_metric_semantics(net3):
    ev = T.node_metric(net3, "eigenvector_centrality", weighted=True)
    assert ev["weight_meaning"] == "1/length as strength"
    bt = T.node_metric(net3, "betweenness_centrality", weighted=True)
    assert bt["weight_meaning"] == "length as distance"
    assert len(bt["values"]) == net3.wn.num_nodes


def test_validation_runs(net3):
    v = validate(net3)
    assert {"error", "warning", "info"} == set(v["counts"])


def test_simulation_kpis_and_scenario(ltown):
    crit = DesignCriteria(min_pressure_m=30)
    sim = run_simulation(ltown, SimSettings(duration_h=24), crit)
    s = sim.summary
    assert s["frames"] > 2 and s["pressure_m"]["min"]["value"] > 0
    assert s["criteria"]["low_pressure"]["count"] > 0
    # headloss is m/km: check against head difference for one pipe
    p = ltown.wn.get_link("p100")
    i, a, b = sim.link_index["p100"], sim.node_index[p.start_node_name], sim.node_index[p.end_node_name]
    dh = abs(sim.node_data["head"][5, a] - sim.node_data["head"][5, b])
    assert abs(sim.link_data["headloss"][5, i]) * p.length / 1000 == pytest.approx(dh, rel=0.02, abs=1e-4)
    closed = run_simulation(ltown, SimSettings(duration_h=24, closed_links=["PUMP_1"]), crit)
    assert any("Removed" in n for n in closed.summary["notes"])
    assert closed.summary["pumps"][0]["hours_running"] == 0


def test_tools_return_actions(ltown):
    s = Session(ltown)
    res, acts = call_tool(s, "filter_pipes", {"conditions": [{"field": "diameter_mm", "op": "<", "value": 100}]})
    assert res["number_of_matching_pipes"] == 5 and acts[0]["type"] == "highlight"
    res, acts = call_tool(s, "criteria_violations", {"check": "low_pressure"})
    assert res["success"] is False  # no criteria / simulation yet
    res, _ = call_tool(s, "element_info", {"kind": "node", "id": "P235"})  # wrong kind and case
    assert res["success"] and res["id"] == "p235"


def test_isolated_nodes_blanked_and_compare(ltown):
    from wdn_engine.hydraulics import compare
    base = run_simulation(ltown, SimSettings(duration_h=24))
    cut = run_simulation(ltown, SimSettings(duration_h=24, closed_links=["p235"]))
    assert cut.summary["isolated_junctions_count"] >= 1
    assert cut.summary["pressure_m"]["min"]["value"] > -10  # no -80,000 m artefacts
    d = compare(base, cut, "pressure", "min")
    assert d["changed_count"] > 0 and d["lost_supply_count"] >= 1 and d["max_decrease"]["value"] < 0


def test_steady_state_flagged(net3):
    sim = run_simulation(net3, SimSettings(duration_h=0))
    assert sim.summary["single_step"] and any("Steady state" in n for n in sim.summary["notes"])


def test_topology_zero_message(ltown):
    from wdn_engine.formatting import format_result
    res, _ = call_tool(Session(ltown), "topology_characterization", {"focus": "unsupplied"})
    assert "0 junctions without a source" in format_result("topology_characterization", res)


def test_simulation_questions_route_and_answer(ltown):
    s = Session(ltown)
    assert route(s, "Which nodes have the lowest pressure?")[0][0] == "rank_elements"
    assert route(s, "where is the oldest water")[0][1]["variable"] == "age"
    assert route(s, "tank levels")[0][0] == "tank_events"
    res, acts = call_tool(s, "rank_elements", {"variable": "pressure", "order": "lowest", "n": 3})
    assert res["success"] and len(res["elements"]) == 3 and res["notes"]  # auto-ran a simulation
    assert res["elements"][0]["value"] <= res["elements"][1]["value"]
    res, _ = call_tool(s, "rank_elements", {"variable": "velocity", "order": "highest"})
    assert res["unit"] == "m/s"


def test_query_table_combined(ltown):
    from wdn_engine.tables import query
    s = Session(ltown)
    s.simulate(SimSettings(duration_h=24))
    r = query(s, "pipes", [{"column": "diameter_mm", "op": "<", "value": 101},
                           {"column": "max_velocity_ms", "op": ">", "value": 0.3}], sort_by="max_velocity_ms")
    assert r["total_matches"] > 0 and all(x["diameter_mm"] < 101 and x["max_velocity_ms"] > 0.3 for x in r["rows"])
    agg = query(s, "links", [{"column": "flow_reverses", "op": "is_true"}], aggregate={"func": "sum", "column": "length_m"})
    assert agg["aggregate"]["value"] > 0
    bad = query(s, "pipes", [{"column": "colour", "op": "=", "value": 1}])
    assert not bad["success"] and "Available" in bad["error"]


def test_time_and_space(ltown):
    from wdn_engine import spatial, temporal
    s = Session(ltown)
    s.simulate(SimSettings(duration_h=48))
    v = temporal.value_at(s, "n22", "day 2 08:00", "pressure")
    assert v["clock"] == "Day 2, 08:00" and v["values"]["pressure"] > 0
    w = temporal.when_extreme(s, "demand", "max")
    assert w["scope"] == "network" and w["series_range"][0] < w["value"]
    near = spatial.nodes_within(s, "R2", 200)
    assert near["count"] > 0 and all(r["distance_m"] <= 200 for r in near["nodes"])
    down = spatial.trace_flow(s, "p235", "downstream")
    assert down["junctions"] > 0 and "p235" in down["link_ids"]


def test_snapshot_reproduces_simulation():
    import numpy as np
    from wdn_engine import resilience as R
    for name in ("Net1", "Net3"):  # Net3 has a pattern called "1" (EPANET's default-pattern trap)
        m = NetworkModel.from_inp(str(LIB / f"{name}.inp"))
        s = Session(m)
        sim = R.baseline_for(s)
        t = R.peak_index(m, sim)
        j = [sim.node_index[x] for x in m.wn.junction_name_list]
        with R.SnapshotSolver(m, sim, demand_model="DDA") as sv:
            assert sv.base["delivered"].sum() == pytest.approx(float(np.nansum(sim.node_data["demand"][t, j])), rel=1e-4)
            assert np.nanmax(np.abs(sv.base["pressure"] - sim.node_data["pressure"][t, j])) < 0.01


def test_criticality_matches_full_pdd_run(ltown):
    import numpy as np
    from wdn_engine import resilience as R
    s = Session(ltown)
    c = R.pipe_criticality(s)
    top = c["top"][0]
    assert top["id"] in ("p239", "p240") and top["demand_shortfall_lps"] > 5
    base = run_simulation(ltown, SimSettings(duration_h=168, demand_model="PDD"))
    cut = run_simulation(ltown, SimSettings(duration_h=168, demand_model="PDD", closed_links=[top["id"]]))
    j = [base.node_index[x] for x in ltown.wn.junction_name_list]
    t = int(np.argmin(np.abs(base.times_h - c["snapshot_time_h"])))
    full = float(np.nansum(base.node_data["demand"][t, j]) - np.nansum(cut.node_data["demand"][t, j]))
    assert full == pytest.approx(top["demand_shortfall_lps"], rel=0.02)


def test_engineering_analyses(ltown):
    from wdn_engine import resilience as R
    s = Session(ltown)
    seg = R.valve_segments(s, "strategic", 2)
    assert seg["segments"] > 50 and seg["top"][0]["junctions_without_water"] > 0
    ff = R.fire_flow(s, "n22", 14.0, required_flow_lps=15)
    assert ff["available_lps"] > 15 and ff["check"]["meets_requirement"]
    g = R.demand_growth(s, 25.0, verify_eps=False)
    assert g["capacity_factor"] > 1 and g["first_failing_nodes"]
    leak = run_simulation(ltown, SimSettings.from_dict({"duration_h": 24, "leaks": [{"element": "n100", "diameter_mm": 10}]}))
    assert leak.summary["leakage"]["mean_lps"] == pytest.approx(1.83, rel=0.05)
    tr = run_simulation(ltown, SimSettings.from_dict({"duration_h": 72, "quality": "trace", "trace_node": "R1"}))
    assert 0 < tr.summary["trace"]["junctions_mostly_supplied_pct50"] < len(ltown.wn.junction_name_list)


def test_what_if_and_diagnosis(ltown):
    from wdn_engine import insight as I
    s = Session(ltown)
    w = I.what_if(s, closed_links=["PUMP_1"], duration_h=48)
    assert w["severity"] == "major" and w["tanks"][0]["empties_in_scenario"]
    d = I.diagnose_pressure(Session(ltown), "n22")
    c = d["components"]
    # head balance: source head - elevation - friction - valves + pumps = pressure (within rounding)
    assert d["source_head_m"] - d["elevation_m"] - c["pipe_friction_m"] - c["valve_loss_m"] + c["pump_gain_m"] == pytest.approx(d["pressure_m"], abs=0.05)
    assert any("Elevation" in x or "High ground" in x for x in d["likely_causes"])


def test_router_reasoning_questions(ltown):
    s = Session(ltown)
    assert route(s, "what is PDD?")[0] == ("explain_term", {"term": "pdd"})
    assert route(s, "what if demand grows by 30%")[0][1]["demand_multiplier"] == 1.3
    assert route(s, "what happens if pump 1 fails")[0][1]["closed_links"] == ["PUMP_1"]
    assert route(s, "why is pressure low at n22")[0][0] == "diagnose_pressure"
    assert route(s, "is this network well designed?")[0][0] == "assess_network"
    q = route(s, "how many nodes have pressure between 25 and 30 m")[0]
    assert q[0] == "query_table" and len(q[1]["filters"]) == 2


def test_number_verifier():
    from wdn_engine.verify import check, evidence_numbers
    ev = evidence_numbers({"shortfall": 7.3854, "ratio": 0.11, "clock": "Day 4, 10:40", "n": 92})
    r = check("Closing it cuts 7.4 L/s (11%) at Day 4, 10:40; 92 junctions; about 15 m lower.", ev)
    assert r["unverified"] == ["15"]


def test_report_document_consistent(ltown):
    from wdn_engine import report as RP
    from wdn_engine.tools import call_tool
    s = Session(ltown)
    call_tool(s, "demand_growth", {"min_pressure_m": 25, "verify_eps": False})
    doc = RP.build_document(s, "technical", include={"maps": False})
    text = " ".join(str(b) for b in doc.blocks)
    f = s.results["demand_growth"]["capacity_factor"]
    assert f"{f:g}×" in text and text.count("Demand can grow") >= 1
    # the assessment must reuse the same growth number (no contradictory values in one report)
    assert all(f"{f:g}×" in x for x in [t for t in text.split("Capacity:")[1:]][:1]) if "Capacity:" in text else True
