"""Built-in, reviewed definitions used by ⓘ help icons and the `explain_term` tool.

Keeping definitions in the app (not the LLM's memory) makes explanations consistent and checkable.
"""

from __future__ import annotations

from typing import Any

GLOSSARY: dict[str, dict[str, Any]] = {
    "pressure": {"title": "Pressure (m)", "short": "Water pressure at a node, as metres of water column (1 m ≈ 9.81 kPa ≈ 0.1 bar).",
                 "detail": "Pressure = hydraulic head − node elevation. It decides whether water reaches taps and upper floors. "
                           "Too low: poor service; too high: more leakage and bursts."},
    "head": {"title": "Hydraulic head (m)", "short": "Total energy level of the water: elevation + pressure, in metres.",
             "detail": "Water flows from high head to low head. Reservoirs and tanks fix the head; pipes lose head to friction; pumps add head."},
    "demand": {"title": "Demand (L/s)", "short": "Water taken out at a junction (consumption).",
               "detail": "Base demand × time pattern × global multiplier. A junction may have several demand categories "
                         "(e.g. residential, commercial), which WDN Studio sums."},
    "velocity": {"title": "Velocity (m/s)", "short": "Average speed of water in a pipe (flow ÷ cross-section area).",
                 "detail": "Very low velocities (< ~0.1 m/s) let water stagnate and age; high velocities (> ~1.5–2 m/s) cause large "
                           "headloss, noise and transient (water-hammer) risk."},
    "headloss": {"title": "Headloss (m/km)", "short": "Energy lost to friction per kilometre of pipe.",
                 "detail": "Grows roughly with velocity squared. High unit headloss usually means the pipe is too small for its flow."},
    "flow": {"title": "Flow (L/s)", "short": "Volume of water passing through a link per second; the sign gives direction.",
             "detail": "Positive = from the link's start node to its end node. Arrows on the map show the direction."},
    "roughness": {"title": "Roughness", "short": "Pipe-wall friction parameter.",
                  "detail": "Hazen-Williams C (unitless, ~140 new plastic, ~100 old cast iron; higher = smoother) or Darcy-Weisbach "
                            "ε (mm; higher = rougher) depending on the model's headloss formula."},
    "dd": {"title": "Demand-driven analysis (DD)", "short": "Every junction receives its full demand regardless of pressure.",
           "detail": "Standard EPANET mode. Fine for normal conditions, but in failures it can produce impossible negative pressures "
                     "because demand is forced through. Use PDD for failure scenarios."},
    "pdd": {"title": "Pressure-driven analysis (PDD)", "short": "Delivered demand depends on pressure.",
            "detail": "Full demand at or above the required pressure, none at or below the minimum pressure, and a smooth curve in "
                      "between. Gives realistic results when pressures are low (pipe bursts, pump failures, growth)."},
    "required_pressure": {"title": "Required pressure (PDD)", "short": "Pressure at which a node receives 100% of its demand in PDD.",
                          "detail": "Typical values 15–25 m depending on building heights and local standards."},
    "demand_multiplier": {"title": "Demand multiplier", "short": "Scales every demand in the network (1 = normal, 1.5 = +50%).",
                          "detail": "Used for peak-day studies and future growth."},
    "water_age": {"title": "Water age (h)", "short": "Time the water has spent in the network since leaving a reservoir or tank.",
                  "detail": "High age means lower disinfectant residual and quality risk. Needs long runs (72 h+) to settle; an age equal "
                            "to the run length means the water there was never refreshed."},
    "chlorine": {"title": "Chlorine residual (mg/L)", "short": "Disinfectant concentration left in the water.",
                 "detail": "Decays over time (bulk decay k, 1/day) and by reaction with pipe walls. Many guidelines aim for ≥ 0.2 mg/L at taps."},
    "trace": {"title": "Source tracing (%)", "short": "Share of the water at each node that came from a chosen source.",
              "detail": "Shows supply zones and mixing between sources."},
    "steady_state": {"title": "Steady state", "short": "A single snapshot (duration 0): one time step, no time variation.",
                     "detail": "Min, max and mean are identical; tanks do not fill or drain. Use an extended-period run (e.g. 24 h) for daily cycles."},
    "eps": {"title": "Extended-period simulation (EPS)", "short": "A series of hydraulic snapshots over time (e.g. 24 h at 5–60 min steps).",
            "detail": "Captures demand patterns, tank filling/draining, and pump and valve controls."},
    "min_pressure": {"title": "Minimum pressure criterion", "short": "Lowest acceptable service pressure at junctions.",
                     "detail": "Set it in the Criteria tab. Junctions below it get a red ring when colouring by pressure."},
    "max_pressure": {"title": "Maximum pressure criterion", "short": "Highest acceptable pressure.",
                     "detail": "High pressure increases leakage and burst frequency; often managed with PRVs."},
    "min_velocity": {"title": "Minimum peak velocity", "short": "Flags pipes whose fastest flow never reaches this value.",
                     "detail": "Such pipes are prone to stagnation and sediment build-up."},
    "max_velocity": {"title": "Maximum velocity", "short": "Fastest acceptable flow speed in a pipe.",
                     "detail": "Exceeding it means high headloss, noise and transient risk."},
    "max_headloss": {"title": "Maximum headloss (m/km)", "short": "Largest acceptable friction loss per km.",
                     "detail": "A design check for pipe sizing."},
    "betweenness": {"title": "Betweenness centrality", "short": "Share of shortest routes between all node pairs that pass through a node.",
                    "detail": "High values mark 'transfer' nodes whose loss would force long detours. It is purely topological — it ignores "
                              "flows and pressures."},
    "closeness": {"title": "Closeness centrality", "short": "Inverse of the average distance from a node to all others.",
                  "detail": "High values = centrally located nodes."},
    "eigenvector": {"title": "Eigenvector centrality", "short": "A node is important if its neighbours are important.",
                    "detail": "Weighted version uses 1/length, so short pipes count as strong connections."},
    "degree": {"title": "Degree", "short": "Number of distinct neighbouring nodes.", "detail": "1 = dead end; ≥ 3 = junction of several pipes."},
    "clustering": {"title": "Clustering coefficient", "short": "Fraction of a node's neighbour pairs that are directly connected.",
                   "detail": "Usually very low in water networks (few triangles)."},
    "bridge": {"title": "Bridge (single-link)", "short": "A link whose removal splits the network into two parts.",
               "detail": "No alternative route exists, so its failure isolates everything beyond it. Parallel pipes are not bridges."},
    "articulation": {"title": "Articulation point", "short": "A node whose removal splits the network.", "detail": "The node equivalent of a bridge."},
    "dead_end": {"title": "Dead end", "short": "A junction connected to only one neighbour.",
                 "detail": "No alternative supply and often low velocity — water-quality and reliability concern."},
    "meshedness": {"title": "Meshedness coefficient", "short": "Number of independent loops relative to the maximum possible for a planar network.",
                   "detail": "0 = tree (branched), 1 = maximally meshed. Real distribution networks are typically 0.05–0.3."},
    "todini": {"title": "Todini resilience index", "short": "Share of surplus power available at junctions above the required pressure.",
               "detail": "0–1; higher means more spare pressure to cope with failures. Needs a minimum pressure (P*)."},
    "criticality": {"title": "Pipe criticality (N-1)", "short": "How much harm the loss of a single pipe causes.",
                    "detail": "Each pipe is closed in turn (pressure-driven) and ranked by demand that cannot be delivered, junctions "
                              "pushed below the required pressure and pressure drop."},
    "snapshot": {"title": "Peak-hour snapshot", "short": "The network frozen at the hour of highest demand of a baseline run.",
                 "detail": "Demands, reservoir heads, tank levels and pump/valve statuses are fixed at that moment, allowing thousands of "
                           "fast solves. Tank levels cannot evolve after a failure, so longer-term effects are not captured."},
    "segment": {"title": "Valve isolation segment", "short": "The area shut off together when the surrounding valves are closed.",
                "detail": "Repairs isolate whole segments, not single pipes. Benchmark files rarely include valves, so a layout is assumed: "
                          "'N valves per junction' puts valves on N−1 of each junction's pipes (N-1 rule)."},
    "fire_flow": {"title": "Fire flow", "short": "Extra flow a hydrant can deliver while keeping a residual pressure.",
                  "detail": "Typical residual is 14 m (≈ 20 psi). Computed demand-driven at the peak-hour snapshot by bisection."},
    "demand_growth": {"title": "Demand growth capacity", "short": "How far all demands can be scaled up before minimum pressure is breached.",
                      "detail": "A capacity factor of 1.5 means 50% headroom."},
    "leak": {"title": "Leak (orifice/emitter)", "short": "Outflow through a hole: Q = Cd·A·√(2g·p).",
             "detail": "Leak flow grows with the square root of pressure, which is why pressure management reduces leakage."},
    "prv": {"title": "Pressure reducing valve (PRV)", "short": "Limits the pressure downstream to its setting.", "detail": "Used to create pressure zones."},
    "tank": {"title": "Tank", "short": "Storage whose level changes over time.", "detail": "Fills when supply exceeds demand, drains at peaks."},
    "reservoir": {"title": "Reservoir", "short": "An unlimited source/sink at a fixed (or patterned) head.", "detail": "E.g. a lake, river or treatment plant."},
    "shortest_path": {"title": "Shortest path", "short": "Route between two nodes with the least total pipe length (or fewest pipes, 'by hops').",
                      "detail": "Purely topological; real water may take other routes."},
    "query": {"title": "Query table", "short": "Filter, sort, count or aggregate any node/link attribute or simulation result.",
              "detail": "Simulation columns are statistics over the whole run (min/mean/max)."},
}

ALIASES = {
    "demand driven": "dd", "demand-driven": "dd", "pressure driven": "pdd", "pressure-driven": "pdd", "age": "water_age",
    "betweenness centrality": "betweenness", "closeness centrality": "closeness", "eigenvector centrality": "eigenvector",
    "bridges": "bridge", "articulation point": "articulation", "articulation points": "articulation", "dead ends": "dead_end",
    "dead-end": "dead_end", "todini index": "todini", "resilience index": "todini", "n-1": "criticality",
    "pipe criticality": "criticality", "valve segment": "segment", "segments": "segment", "isolation segment": "segment",
    "fire-flow": "fire_flow", "fire flow": "fire_flow", "hydrant": "fire_flow", "growth": "demand_growth", "capacity": "demand_growth",
    "emitter": "leak", "pressure reducing valve": "prv", "extended period": "eps", "extended-period": "eps", "steady-state": "steady_state",
    "steady state": "steady_state", "snapshot": "snapshot", "peak hour": "snapshot", "head loss": "headloss", "hazen-williams": "roughness",
    "c-factor": "roughness", "chlorine residual": "chlorine", "source tracing": "trace", "required pressure": "required_pressure",
    "multiplier": "demand_multiplier", "meshed": "meshedness",
}


def lookup(term: str) -> tuple[str, dict[str, Any]] | None:
    t = str(term or "").strip().lower().replace("_", " ")
    key = ALIASES.get(t) or (t.replace(" ", "_") if t.replace(" ", "_") in GLOSSARY else None)
    if key is None:
        for k, v in GLOSSARY.items():   # loose match on titles / aliases contained in the text
            if k.replace("_", " ") in t or v["title"].lower().split(" (")[0] in t:
                key = k
                break
        else:
            for a, k in sorted(ALIASES.items(), key=lambda kv: -len(kv[0])):
                if a in t:
                    key = k
                    break
    return (key, GLOSSARY[key]) if key else None
