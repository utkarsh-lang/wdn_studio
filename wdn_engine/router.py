"""Offline, rule-based question router (used when no LLM is configured or it fails).

Notes: diameters default to mm (not metres), inches
and feet are understood, comparison words such as "smaller/longer/between" work,
and element look-ups only fire when the token is a real node or link ID.
"""

from __future__ import annotations

import re
from typing import Any

from .session import Session

UNIT_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "inch": 25.4, "inches": 25.4, '"': 25.4, "ft": 304.8}
UNIT_TO_M = {"mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0, "ft": 0.3048, "in": 0.0254, "inch": 0.0254, "inches": 0.0254}

OP_WORDS = [
    ("less than or equal to", "<="), ("greater than or equal to", ">="), ("at most", "<="), ("at least", ">="),
    ("no more than", "<="), ("no less than", ">="), ("up to", "<="),
    ("less than", "<"), ("smaller than", "<"), ("narrower than", "<"), ("shorter than", "<"), ("below", "<"), ("under", "<"),
    ("greater than", ">"), ("more than", ">"), ("larger than", ">"), ("bigger than", ">"), ("wider than", ">"),
    ("longer than", ">"), ("above", ">"), ("over", ">"), ("exceeding", ">"),
    ("equal to", "="), ("equals", "="), ("exactly", "="), ("of", "="),
    ("<=", "<="), (">=", ">="), ("≤", "<="), ("≥", ">="), ("<", "<"), (">", ">"), ("=", "="),
]
IMPLIED_FIELD = {"shorter than": "length", "longer than": "length", "narrower than": "diameter", "wider than": "diameter"}
FIELD_WORDS = {"diameter": "diameter", "diameters": "diameter", "dia": "diameter", "dn": "diameter",
               "length": "length", "lengths": "length", "long": "length",
               "roughness": "roughness", "c-factor": "roughness", "c factor": "roughness"}
NUM = r"(\d+(?:\.\d+)?)"
UNIT = r"\s*(mm|cm|km|m|inches|inch|in|ft|\")?(?![a-z])"


def _op_regex() -> str:
    return "|".join(re.escape(w) for w, _ in sorted(OP_WORDS, key=lambda x: -len(x[0])))


def _convert(field: str, value: float, unit: str | None) -> tuple[str, float]:
    if field == "diameter":
        return "diameter_mm", value * UNIT_TO_MM.get(unit or "mm", 1.0)
    if field == "length":
        return "length_m", value * UNIT_TO_M.get(unit or "m", 1.0)
    return "roughness", value


def parse_pipe_conditions(text: str) -> list[dict[str, Any]]:
    q = text.lower().replace("≤", "<=").replace("≥", ">=")
    ops = _op_regex()
    conds: list[dict[str, Any]] = []
    fields = "|".join(sorted(map(re.escape, FIELD_WORDS), key=len, reverse=True))

    # "diameter between 100 and 200 mm"
    for m in re.finditer(rf"\b({fields})\b[^0-9]{{0,25}}?between\s+{NUM}{UNIT}\s+and\s+{NUM}{UNIT}", q):
        field = FIELD_WORDS[m.group(1)]
        unit = m.group(5) or m.group(3)
        f, lo = _convert(field, float(m.group(2)), unit)
        _, hi = _convert(field, float(m.group(4)), unit)
        conds += [{"field": f, "op": ">=", "value": lo}, {"field": f, "op": "<=", "value": hi}]
    if conds:
        return conds

    # "<field> [is|of pipes] <op> <num><unit>"
    for m in re.finditer(rf"\b({fields})\b\s*(?:of\s+)?(?:the\s+)?(?:pipes?\s+)?(?:is\s+|are\s+)?({ops})\s*{NUM}{UNIT}", q):
        op = dict(OP_WORDS)[m.group(2)]
        field, value = _convert(FIELD_WORDS[m.group(1)], float(m.group(3)), m.group(4))
        conds.append({"field": field, "op": op, "value": value})
    # "<op> <num><unit> <field>" or implied field ("longer than 500 m", "smaller than 4 inch")
    for m in re.finditer(rf"({ops})\s*{NUM}{UNIT}(?:\s+(?:in\s+)?({fields}))?", q):
        word = m.group(1)
        op = dict(OP_WORDS)[word]
        if m.group(4):
            field = FIELD_WORDS[m.group(4)]
        elif word in IMPLIED_FIELD:
            field = IMPLIED_FIELD[word]
        elif m.group(3) in {"mm", "cm", "in", "inch", "inches", '"'} and word not in {"of"}:
            field = "diameter"
        elif m.group(3) in {"km"}:
            field = "length"
        else:
            continue
        f, v = _convert(field, float(m.group(2)), m.group(3))
        if not any(c["field"] == f and c["op"] == op and abs(c["value"] - v) < 1e-9 for c in conds):
            conds.append({"field": f, "op": op, "value": v})
    return conds


def _find_ids(session: Session, text: str) -> tuple[list[str], list[str]]:
    nodes, links = [], []
    for token in re.findall(r"[A-Za-z0-9_\-\.]+", text):
        token = token.strip(".")
        n, l = session.model.resolve_node(token), session.model.resolve_link(token)
        if n and n not in nodes:
            nodes.append(n)
        if l and l not in links:
            links.append(l)
    return nodes, links


GEN_FIELDS = {
    # word: (column if "less than", column if "greater than", table, unit kind)
    "pressure": ("min_pressure_m", "max_pressure_m", "nodes", "m"),
    "elevation": ("elevation_m", "elevation_m", "nodes", "m"),
    "base demand": ("base_demand_lps", "base_demand_lps", "nodes", "lps"),
    "demand": ("base_demand_lps", "base_demand_lps", "nodes", "lps"),
    "water age": ("max_age_h", "max_age_h", "nodes", "h"),
    "age": ("max_age_h", "max_age_h", "nodes", "h"),
    "velocity": ("max_velocity_ms", "max_velocity_ms", "links", "ms"),
    "speed": ("max_velocity_ms", "max_velocity_ms", "links", "ms"),
    "flow": ("max_abs_flow_lps", "max_abs_flow_lps", "links", "lps"),
    "headloss": ("max_headloss_m_per_km", "max_headloss_m_per_km", "links", "mkm"),
    "head loss": ("max_headloss_m_per_km", "max_headloss_m_per_km", "links", "mkm"),
    "diameter": ("diameter_mm", "diameter_mm", "links", "mm"),
    "length": ("length_m", "length_m", "links", "m"),
    "roughness": ("roughness", "roughness", "links", ""),
}
SIM_COLUMNS = {"min_pressure_m", "max_pressure_m", "max_age_h", "max_velocity_ms", "max_abs_flow_lps", "max_headloss_m_per_km"}
TIME_RE = re.compile(r"(?:\bday\s*\d+[, ]*)?(?:\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b|\bt\s*=\s*\d+(?:\.\d+)?\s*h?\b|"
                     r"\bafter\s+\d+(?:\.\d+)?\s*(?:h|hours?)\b|\bat\s+\d+(?:\.\d+)?\s*(?:h|hours?)\b)")
PRONOUN_RE = re.compile(r"\b(?:this|that|the selected|selected)\s+(?:node|junction|pipe|link|element|one|tank|pump|valve)\b|"
                        r"\bthe selection\b|\bhere\b|\bit\b")


def parse_general_conditions(text: str) -> list[dict[str, Any]]:
    """Conditions on any column, e.g. 'pipes under 100 mm with velocity above 0.5 m/s',
    'junctions above 80 m elevation with pressure below 30 m'.

    Each comparison (operator + number + unit) is paired with the field word right after it
    ('above 80 m elevation') or, failing that, the nearest field word before it ('pressure below 30').
    A bare 'under 100 mm' / '4 inch' implies diameter.
    """
    q = text.lower().replace("≤", "<=").replace("≥", ">=").replace("m/s", " ms").replace("l/s", " lps")
    words = "|".join(sorted(map(re.escape, GEN_FIELDS), key=len, reverse=True))
    out_between: list[dict[str, Any]] = []
    for m in re.finditer(rf"\b({words})\b[^0-9]{{0,25}}?between\s+(-?\d+(?:\.\d+)?)\s*(mm|m|ms|lps|h)?\s+and\s+(-?\d+(?:\.\d+)?)\s*(mm|m|ms|lps|h)?", q):
        field = m.group(1)
        col_lt, col_gt, table, kind = GEN_FIELDS[field]
        lo, hi = float(m.group(2)), float(m.group(4))
        before = q[max(0, m.start() - 9):m.start()]
        if field == "pressure" and ("minimum" in before or "min " in before or "lowest" in before):
            out_between.append({"column": "min_pressure_m", "op": "between", "value": [lo, hi], "_table": table})
        elif field == "pressure" and ("maximum" in before or "max " in before or "highest" in before):
            out_between.append({"column": "max_pressure_m", "op": "between", "value": [lo, hi], "_table": table})
        elif field == "pressure":   # "pressure between a and b" = stays within the band over the run
            out_between += [{"column": "min_pressure_m", "op": ">=", "value": lo, "_table": table},
                            {"column": "max_pressure_m", "op": "<=", "value": hi, "_table": table}]
        else:
            out_between.append({"column": col_gt, "op": "between", "value": [lo, hi], "_table": table})
        q = q[:m.start()] + " " * (m.end() - m.start()) + q[m.end():]
    fields = [(m.start(), m.end(), m.group(1)) for m in re.finditer(rf"\b({words})\b", q)]
    op_words = [w for w, _ in OP_WORDS if w != "of"]
    ops = "|".join(re.escape(w) for w in sorted(op_words, key=len, reverse=True))
    out: list[dict[str, Any]] = []
    for m in re.finditer(rf"({ops})\s*(-?\d+(?:\.\d+)?)\s*(mm|cm|km|m|inches|inch|in|ft|ms|lps|h|hours?|\")?(?![a-z0-9])", q):
        word, value, unit = m.group(1), float(m.group(2)), m.group(3)
        op = dict(OP_WORDS)[word]
        after = [f for f in fields if 0 <= f[0] - m.end() <= 4]                       # "above 80 m elevation"
        before = [f for f in fields if f[1] <= m.start() and m.start() - f[1] <= 40]  # "pressure below 30"
        if after:
            field = after[0][2]
        elif before:
            field = before[-1][2]
        elif word in IMPLIED_FIELD:
            field = IMPLIED_FIELD[word]
        elif unit in ("mm", "cm", "in", "inch", "inches", '"'):
            field = "diameter"
        elif unit == "ms":
            field = "velocity"
        elif unit == "lps":
            field = "flow"
        else:
            continue
        col_lt, col_gt, table, kind = GEN_FIELDS[field]
        col = col_lt if op in ("<", "<=") else col_gt
        if kind == "mm":
            value *= UNIT_TO_MM.get(unit or "mm", 1.0)
        elif field == "length":
            value *= UNIT_TO_M.get(unit or "m", 1.0)
        out.append({"column": col, "op": op, "value": round(value, 6), "_table": table})
    return out_between + out


def _resolve_pronouns(text: str, context: dict[str, Any] | None) -> str:
    sel = (context or {}).get("selected") or {}
    if sel.get("id") and PRONOUN_RE.search(text):
        return PRONOUN_RE.sub(str(sel["id"]), text)
    return text


def route(session: Session, text: str, context: dict[str, Any] | None = None) -> list[tuple[str, dict[str, Any]]] | str:
    """Return tool calls to execute, or a help string. ``context`` = what is on screen (selection, time)."""
    q = _resolve_pronouns((text or "").strip(), context)
    low = q.lower()
    if not q:
        return HELP

    def has(*words: str) -> bool:
        return any(w in low for w in words)

    def ids_in(t: str) -> tuple[list[str], list[str]]:
        return _find_ids(session, TIME_RE.sub(" ", t))

    if has("colour by", "color by", "colour the map", "color the map", "show pressure", "show velocity", "show flow"):
        for attr in ("pressure", "velocity", "flow", "headloss", "diameter", "elevation", "demand", "age", "length", "head", "roughness"):
            if attr in low:
                attr = "base_demand" if attr == "demand" and not session.sim else attr
                stat = "min" if "min" in low else "max" if "max" in low else "mean" if "mean" in low or "average" in low else "none"
                return [("set_map_colour", {"attribute": attr, "stat": stat})]

    # ---- edits ("set the diameter of p100 to 150 mm", "close pipe p12", "undo")
    if has("undo") and has("edit", "change", "last"):
        return [("undo_edit", {})]
    m = re.search(r"\b(?:set|change|make|increase|decrease|resize|upsize|update)\b.*?\b(diameter|length|roughness|elevation|demand|base demand|head|level|setting|speed)\b"
                  r".*?\bof\s+([^\s,]+).*?\bto\s+(-?\d+(?:\.\d+)?)\s*(mm|m|l/s|lps|inch|in)?", low) or \
        re.search(r"\b(?:set|change|make|resize|upsize)\s+([^\s,]+)(?:'s)?\s+(diameter|length|roughness|elevation|demand|base demand|head|level|setting|speed)\s+to\s+(-?\d+(?:\.\d+)?)\s*(mm|m|l/s|lps|inch|in)?", low)
    if m:
        g = m.groups()
        field_word, target, value, unit = (g[0], g[1], g[2], g[3]) if g[0] in ("diameter", "length", "roughness", "elevation", "demand", "base demand", "head", "level", "setting", "speed") else (g[1], g[0], g[2], g[3])
        nid, lid = session.model.resolve_node(target), session.model.resolve_link(target)
        v = float(value)
        fmap = {"diameter": "diameter_mm", "length": "length_m", "roughness": "roughness", "elevation": "elevation_m",
                "demand": "base_demand_lps", "base demand": "base_demand_lps", "head": "base_head_m", "level": "init_level_m",
                "setting": "setting", "speed": "speed"}
        field = fmap[field_word]
        if field == "diameter_mm" and unit in ("inch", "in"):
            v *= 25.4
        if lid or nid:
            return [("edit_element", {"kind": "link" if lid else "node", "id": lid or nid, "changes": {field: v}})]
    m = re.search(r"\b(open|close|reopen)\s+(?:the\s+)?(?:pipe|valve|pump|link)?\s*([^\s,?.]+)\s*(?:permanently|in the model)", low)
    if m and session.model.resolve_link(m.group(2)):
        return [("edit_element", {"kind": "link", "id": session.model.resolve_link(m.group(2)),
                                  "changes": {"status": "Closed" if m.group(1) == "close" else "Open"}})]

    # ---- counts, totals, extremes and questions the model cannot answer
    if has("material", "how old", "installed", "installation", "year of", "which year", "customers", "population",
           "households", "people", "cost", "price", "budget", "manufacturer", "brand", "owner", "address", "street"):
        return MODEL_CONTENTS
    if has("no path to", "connected to a source", "connected to a reservoir", "not connected to", "unsupplied",
           "without a source", "cannot be supplied", "every junction connected"):
        return [("topology_characterization", {"focus": "unsupplied"})]
    if has("revers") and has("flow", "direction"):
        agg = {"func": "sum", "column": "length_m"} if has("length", "how long", "km") else {"func": "count"}
        return [("query_table", {"table": "pipes", "filters": [{"column": "flow_reverses", "op": "is_true"}], "aggregate": agg,
                                 "sort_by": "length_m", "limit": 10})]
    m = re.search(r"(?:which|what)\s+(?:reservoirs?|tanks?|sources?)(?:\s+or\s+(?:reservoirs?|tanks?))?\s+(?:supply|supplies|feed|feeds|serve|serves)\s+"
                  r"(?:junction|node)?\s*([^\s,?]+)", q, re.I)
    if m:
        return [("trace_flow", {"element": m.group(1), "direction": "upstream"})]
    size_words = {"longest": ("length_m", True), "shortest": ("length_m", False), "largest": ("diameter_mm", True),
                  "biggest": ("diameter_mm", True), "widest": ("diameter_mm", True), "smallest": ("diameter_mm", False),
                  "narrowest": ("diameter_mm", False)}
    for word, (col, desc) in size_words.items():
        if word in low and has("pipe", "diameter") and not re.search(r"\d", low):
            agg = {"func": "max" if desc else "min", "column": col}
            return [("query_table", {"table": "pipes", "sort_by": col, "descending": desc, "limit": 5, "aggregate": agg})]
    if has("total base demand", "total demand of the network", "total network demand in l/s", "sum of demands", "total consumption"):
        if not has("when", "time", "peak"):
            return [("network_summary", {})]
    if low.startswith("how many") and not re.search(r"\d", low) and has("tank", "reservoir", "junction", "pump", "valve", "nodes", "links", "pipes") \
            and not has("dead", "bridge", "articulation", "loop", "segment", "pressure", "velocity", "flow", "cut off", "lose"):
        return [("network_summary", {})]

    # ---- definitions, assessment, diagnosis, what-if, run comparison
    from .glossary import ALIASES, GLOSSARY
    m = re.match(r"\s*(?:what\s+(?:is|are|does)|what's|explain|define|meaning of|what do you mean by|tell me what)\s+"
                 r"(?:an?\s+|the\s+)?(.+?)(?:\s+mean)?\s*\??\s*$", low)
    if m:
        term = re.sub(r"\s+(measure|measures|mean|means|do|does|represent|represents|indicate|indicates|tell us|show|shows)$", "",
                      m.group(1).strip().rstrip("?").strip())
        key = ALIASES.get(term) or (term.replace(" ", "_") if term.replace(" ", "_") in GLOSSARY else None) or next(
            (k for k, v in GLOSSARY.items() if v["title"].lower().split(" (")[0] == term), None) or next(
            (k for k, v in GLOSSARY.items() if len(v["title"].split(" (")[0].split()) >= 2 and v["title"].lower().split(" (")[0] in term), None) or next(
            (k for a2, k in ALIASES.items() if len(a2.split()) >= 2 and a2 in term), None)
        if key:
            return [("explain_term", {"term": key})]
    if has("well designed", "well-designed", "assess", "evaluate the network", "main problem", "weakness", "strengths",
           "how good is", "overall assessment", "review the network", "review this network", "design review", "what should i improve"):
        return [("assess_network", {})]
    if has("why") and has("pressure", "low", "high", "poor", "weak"):
        nodes, _ = ids_in(q)
        juncs = [n for n in nodes if session.model.wn.get_node(n).node_type == "Junction"]
        if juncs:
            args = {"node": juncs[0]}
            tm2 = TIME_RE.search(low)
            if tm2:
                args["time"] = tm2.group(0)
            return [("diagnose_pressure", args)]
    whatif_words = ("what if", "what-if", "what happens", "what would happen", "suppose", "if we close", "if we shut",
                    "impact of closing", "effect of closing", "consequence")
    failure_words = ("fail", "failure", "burst", "break", "outage", "out of service", "shut", "close", "closed", "trip", "offline")
    if has(*whatif_words) or (has(*failure_words) and has("impact", "effect", "happen", "consequen")):
        nodes, links = ids_in(q)
        wn = session.model.wn
        if has("pump") and not any(l in wn.pump_name_list for l in links):
            num = re.search(r"pump\s*[#_ -]?\s*(\w+)", low)
            pumps = list(wn.pump_name_list)
            cand = [p for p in pumps if num and re.search(rf"(?:^|[^0-9a-z]){re.escape(num.group(1))}$", p.lower())] or (pumps if len(pumps) == 1 else [])
            links = cand[:1] + links
        args: dict[str, Any] = {}
        if links:
            args["closed_links"] = links[:10]
        mm = re.search(r"(?:demand\D{0,20}?(?:x|×|\*|by)\s*|(?:x|×)\s*)(\d+(?:\.\d+)?)", low)
        pct = re.search(r"(\d+(?:\.\d+)?)\s*%", low)
        if pct and has("demand", "growth", "grow", "consumption", "increase", "more", "decrease", "less", "reduc"):
            args["demand_multiplier"] = round(1 + float(pct.group(1)) / 100 * (-1 if has("decrease", "less", "reduc", "drop") else 1), 4)
        elif mm and float(mm.group(1)) <= 10:
            args["demand_multiplier"] = float(mm.group(1))
        elif has("double", "twice"):
            args["demand_multiplier"] = 2.0
        elif has("triple"):
            args["demand_multiplier"] = 3.0
        elif has("halve", "half"):
            args["demand_multiplier"] = 0.5
        if has("demand-driven", "demand driven", " dd "):
            args["demand_model"] = "DD"
        d = re.search(rf"{NUM}\s*(?:h|hr|hrs|hours?)\b", low)
        if d:
            args["duration_h"] = float(d.group(1))
        if args.get("closed_links") or args.get("demand_multiplier"):
            return [("what_if", args)]
        return "Tell me what changes, e.g. `what if p239 fails`, `what if PUMP_1 fails`, `what if demand grows by 30%`."
    if has("compare", "difference between run", "what changed", "versus", " vs "):
        runs = {int(r["no"]): r["sim_id"] for r in ((context or {}).get("runs") or []) if r.get("sim_id") in session.sims}
        nums = [int(x) for x in re.findall(r"(?:run\s*#?|#)(\d+)", low)]
        args = {}
        if len(nums) >= 2 and nums[0] in runs and nums[1] in runs:
            args = {"base_sim_id": runs[nums[0]], "scenario_sim_id": runs[nums[1]]}
        elif len(nums) == 1 and nums[0] in runs:
            args = {"base_sim_id": runs[nums[0]]}
        var = next((v for w, v in (("velocit", "velocity"), ("flow", "flow"), ("demand", "demand"), ("headloss", "headloss"),
                                   ("head", "head")) if w in low), "pressure")
        args["variable"] = var
        args["stat"] = "mean" if has("mean", "average") else "max" if (has("max") and var != "pressure") else "min"
        if len(session.sims) >= 2:
            return [("compare_runs", args)]

    # ---- engineering analyses (before the generic simulate branch)
    def num_before(units: str) -> float | None:
        m = re.search(rf"(\d+(?:\.\d+)?)\s*(?:{units})(?![a-z])", low)
        return float(m.group(1)) if m else None

    if has("leak", "burst hole", "orifice"):
        nodes, links = ids_in(q)
        elements = nodes + [l for l in links if l not in nodes]
        if elements:
            args = {"elements": elements[:5], "diameter_mm": num_before("mm") or 10.0}
            d = num_before("h|hours?")
            if d:
                args["duration_h"] = d
            return [("leak_scenario", args)]
        return "Tell me where the leak is, e.g. `simulate a 10 mm leak at n100`."
    if has("critical pipe", "criticality", "n-1", "n - 1", "most critical", "critical link", "critical main",
           "which pipe failure", "pipe failure", "failure of each", "if each pipe", "most important pipe"):
        args = {}
        p = num_before("m(?!m)")
        if p and has("pressure", "below", "require"):
            args["required_pressure_m"] = p
        if has("pump", "valve", "all links"):
            args["include"] = "all"
        return [("pipe_criticality", args)]
    if has("segment") or (has("valve") and has("isolat", "shut off", "shutdown", "shut down")):
        m = re.search(r"(\d+)\s*valves?\s*(?:per|at each|on each)", low)
        args = {"placement": "random" if has("random") else "strategic"}
        if m:
            args["n"] = int(m.group(1))
        return [("valve_segments", args)]
    if has("fire flow", "fire-flow", "hydrant", "firefight", "fire fight", "fire demand"):
        nodes, _ = ids_in(q)
        args: dict[str, Any] = {}
        r = re.search(r"(\d+(?:\.\d+)?)\s*m\b(?![a-z/])", low)
        if r and has("residual", "pressure", "keep", "maintain"):
            args["residual_pressure_m"] = float(r.group(1))
        f = num_before("l/s|lps|litres per second|liters per second")
        if f:
            args["required_flow_lps"] = f
        if has("all", "every", "each", "map", "whole", "weakest") or not nodes:
            args["all_nodes"] = True
        else:
            args["node"] = nodes[0]
        return [("fire_flow", args)]
    if has("demand growth", "grow", "growth", "how much more demand", "capacity", "future demand", "headroom", "population increase"):
        args = {}
        p = num_before("m(?!m)")
        if p:
            args["min_pressure_m"] = p
        return [("demand_growth", args)]
    if has("chlorine", "disinfect", "residual cl"):
        args = {"mode": "chlorine"}
        c = num_before("mg/l|mg")
        if c:
            args["chlorine_mgl"] = c
        return [("water_quality", args)]
    if has("where does the water come from", "which source", "source share", "share of water", "supplied by which",
           "water come from", "source tracing", "trace the source", "mix of sources", "mixing"):
        nodes, _ = ids_in(q)
        srcs = [n for n in nodes if n in session.model.sources]
        if srcs and has("from " + srcs[0].lower(), "trace"):
            return [("water_quality", {"mode": "trace", "source": srcs[0]})]
        return [("water_quality", {"mode": "sources"})]

    if has("simulat", "run hydraulic", "run the model", "run epanet", "scenario"):
        args: dict[str, Any] = {}
        m = re.search(rf"{NUM}\s*(?:h|hr|hrs|hours?)\b", low)
        if m:
            args["duration_h"] = float(m.group(1))
        elif re.search(r"\b(\d+)\s*days?\b", low):
            args["duration_h"] = 24.0 * float(re.search(r"\b(\d+)\s*days?\b", low).group(1))
        elif has("steady", "single period", "snapshot"):
            args["duration_h"] = 0.0
        if has("pdd", "pressure-driven", "pressure driven"):
            args["demand_model"] = "PDD"
        m = re.search(r"(?:demand\D{0,20}?(?:x|×|\*|by)\s*|(?:x|×)\s*)(\d+(?:\.\d+)?)", low) or re.search(r"(\d+(?:\.\d+)?)\s*(?:x|×)\s*demand", low)
        if m:
            args["demand_multiplier"] = float(m.group(1))
        m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:more|increase|higher)", low)
        if m:
            args["demand_multiplier"] = 1 + float(m.group(1)) / 100
        if has("close", "closed", "shut", "break", "burst", "fail"):
            _, links = _find_ids(session, q)
            if links:
                args["closed_links"] = links
        if has("age", "water quality"):
            args["water_age"] = True
        return [("run_simulation", args)]

    # ---- spatial
    m = re.search(r"within\s+(\d+(?:\.\d+)?)\s*(km|m|metres|meters)?\s+(?:of|from|around)\s+([^\s,?]+)", q, re.I)
    if m:
        d = float(m.group(1)) * (1000 if (m.group(2) or "").lower() == "km" else 1)
        method = "straight" if has("straight", "crow", "radius", "as the bird") else "network"
        return [("nodes_within", {"center": m.group(3), "distance_m": d, "method": method})]
    m = re.search(r"(?:downstream of|fed by|supplied by|served by|depends? on|dependent on)\s+([^\s,?]+)", q, re.I)
    if m:
        return [("trace_flow", {"element": m.group(1), "direction": "downstream"})]
    m = (re.search(r"upstream of\s+([^\s,?]+)", q, re.I) or
         re.search(r"(?:what|which\s+\w+)\s+(?:feeds?|supplies|supply)\s+([^\s,?]+)", q, re.I) or
         re.search(r"where does\s+([^\s,?]+)\s+get", q, re.I))
    if m:
        return [("trace_flow", {"element": m.group(1), "direction": "upstream"})]

    # ---- tanks
    nodes_q, _ = ids_in(q)
    tanks = [n for n in nodes_q if session.model.wn.get_node(n).node_type == "Tank"]
    if (has("tank") or tanks) and has("empty", "empties", "fill", "full", "level", "overflow", "drain", "when", "behav"):
        return [("tank_events", {"tank_id": tanks[0]} if tanks else {})]

    # ---- time
    vword = None
    for word, v in (("water age", "age"), ("age", "age"), ("pressure", "pressure"), ("velocit", "velocity"),
                    ("headloss", "headloss"), ("flow", "flow"), ("demand", "demand"), ("consumption", "demand"),
                    ("usage", "demand"), ("head", "head")):
        if re.search(rf"\b{re.escape(word)}" + (r"\b" if word in ("age", "head", "flow") else ""), low):
            vword = v
            break
    tm = TIME_RE.search(low)
    if tm or has(" now", "current time", "this time"):
        nodes, links = ids_in(q)
        target = (nodes or links or [None])[0]
        if target:
            when = tm.group(0) if tm else (context or {}).get("time_h")
            if when is not None:
                args = {"element_id": target, "time": str(when)}
                if vword:
                    args["variable"] = vword
                return [("value_at_time", args)]
    if low.startswith("when") or has("what time", "which time", "at what time", "peak hour", "peak time", "time of day",
                                      "time of peak", "time of the peak"):
        nodes, links = ids_in(q)
        extreme = "min" if has("lowest", "minimum", "least", "smallest", "drop", "worst", "low ", "empty", "dip") else "max"
        if vword == "pressure" and has("worst", "critical"):
            extreme = "min"
        args = {"variable": vword or "demand", "extreme": extreme}
        if nodes or links:
            args["element_id"] = (nodes or links)[0]
        return [("when_extreme", args)]

    # ---- combined / simulation-based filters -> query_table
    gconds = parse_general_conditions(q)
    if gconds and (any(c["column"] in SIM_COLUMNS or c["column"] in ("elevation_m", "base_demand_lps") for c in gconds)
                   or len({c["column"] for c in gconds}) > 1):
        tables = [c["_table"] for c in gconds]
        table = ("links" if has("pipe", "link") else "nodes" if has("node", "junction") else max(set(tables), key=tables.count))
        filters = [{k: v for k, v in c.items() if k != "_table"} for c in gconds if c["_table"] == table]
        if filters:
            want_pipes = table == "links" and not has("pump", "valve", "link")
            want_junctions = table == "nodes" and not has("tank", "reservoir", "node")
            args = {"table": "pipes" if want_pipes else "junctions" if want_junctions else table, "filters": filters, "limit": 20}
            sort_col = next((f["column"] for f in filters if f["column"] in SIM_COLUMNS), filters[-1]["column"])
            args["sort_by"] = sort_col
            args["descending"] = filters[-1]["op"] in (">", ">=")
            return [("query_table", args)]

    if has("violat", "criteria", "limit") or (session.criteria.is_set() and session.sim and has(
            "low pressure", "below minimum pressure", "insufficient pressure", "high pressure", "high velocity", "stagnat",
            "low velocity", "high headloss")):
        check = ("high_pressure" if has("high pressure") else "high_velocity" if has("high velocity") else
                 "low_velocity" if has("low velocity", "stagnat") else "high_headloss" if has("headloss") else "low_pressure")
        return [("criteria_violations", {"check": check})]
    var = None
    for word, v in (("water age", "age"), ("age", "age"), ("oldest", "age"), ("stale", "age"), ("water quality", "age"), ("pressure", "pressure"), ("velocit", "velocity"), ("speed", "velocity"),
                    ("headloss", "headloss"), ("head loss", "headloss"), ("flow", "flow"), ("demand", "demand"), ("head", "head")):
        if re.search(rf"\b{re.escape(word)}" + (r"\b" if word in ("age", "head", "flow") else ""), low):
            var = v
            break
    low_words = ("lowest", "minimum", " min ", "least", "smallest", "worst", "low ", "weakest", "slowest", "stagnant")
    high_words = ("highest", "maximum", " max ", "most", "largest", "biggest", "fastest", "high ", "greatest", "top", "oldest")
    if var and (var == "age" or has(*low_words) or has(*high_words) or f" {low.split()[0]} ".strip() in ("which", "where")):
        order = "lowest" if has(*low_words) and not has("oldest") else "highest"
        m = re.search(r"\b(?:top|first)\s+(\d+)\b|\b(\d+)\s+(?:nodes|junctions|pipes|links|elements)\b", low)
        n = int(m.group(1) or m.group(2)) if m else 10
        if not (var == "demand" and has("base demand")):
            return [("rank_elements", {"variable": var, "order": order, "n": n})]
    if var and has("average", "mean", "overall", "summary"):
        return [("simulation_results", {})]
    if has("simulation result", "kpi", "results of the simulation", "last simulation", "tank", "pump", "energy",
           "resilience", "todini", "simulation summary", "hydraulic result", "how did the simulation"):
        return [("simulation_results", {})]

    conds = parse_pipe_conditions(q)
    if conds and has("pipe", "diameter", "length", "roughness", "longer", "shorter", "smaller", "larger", "mm", "inch"):
        return [("filter_pipes", {"conditions": conds})]

    if has("valid", "error", "issue", "problem", "health", "check the model", "sanity"):
        return [("validation_report", {})]

    m = re.search(r"(?:from|between)\s+([^\s,]+)\s+(?:to|and)\s+([^\s,?.]+)", q, re.I)
    if m and has("path", "route", "distance"):
        return [("shortest_path", {"source": m.group(1), "target": m.group(2), "weighted": "hop" not in low})]

    weighted = has("weighted", "by length", "length-weighted", "metres", "meters")
    for phrase, metric in (("betweenness", "betweenness_centrality"), ("closeness", "closeness_centrality"),
                           ("eigenvector", "eigenvector_centrality"), ("clustering", "clustering"),
                           ("degree centrality", "degree"), ("hub", "degree"), ("node degree", "degree"),
                           ("highest degree", "degree"), ("most connected", "degree")):
        if phrase in low:
            if phrase == "betweenness" and has("edge", "link", "pipe"):
                return [("link_metric", {"metric": "edge_betweenness", "weighted": weighted})]
            return [("node_metric", {"metric": metric, "weighted": weighted})]
    if has("average shortest path", "network diameter", "graph diameter", "topological diameter"):
        return [("distance_statistics", {"weighted": weighted})]
    if has("dead end", "dead-end"):
        return [("topology_characterization", {"focus": "dead_ends"})]
    if has("bridge", "critical link", "critical pipe", "single point of failure"):
        return [("topology_characterization", {"focus": "bridges"})]
    if has("articulation", "critical node", "cut vertex"):
        return [("topology_characterization", {"focus": "articulation_points"})]
    if has("unsupplied", "no source", "not connected to", "disconnected"):
        return [("topology_characterization", {"focus": "unsupplied"})]
    if has("characteri", "topology", "redundan", "loops", "meshed", "connectivity", "graph"):
        return [("topology_characterization", {"focus": "overall"})]
    if has("pipe stat", "statistics", "stats", "average diameter", "mean diameter", "total length", "diameter distribution",
           "average pipe diameter", "pipe summary", "longest pipe", "material"):
        return [("pipe_statistics", {})]
    if has("summary", "overview", "how many", "describe the network", "tell me about the network", "what is in"):
        return [("network_summary", {})]

    nodes, links = _find_ids(session, q)
    wants_link = has("pipe", "link", "pump", "valve")
    if links and (wants_link or not nodes):
        return [("element_info", {"kind": "link", "id": links[0]})]
    if nodes:
        return [("element_info", {"kind": "node", "id": nodes[0]})]
    return HELP


MODEL_CONTENTS = (
    "That information is **not in the model**: an EPANET .inp file only stores the hydraulic description of the network — "
    "junction elevations and demands, pipe lengths, diameters, roughness and status, reservoirs, tanks, pumps, valves, "
    "patterns, curves and controls (plus coordinates). Pipe material, installation year, customers, costs or addresses "
    "are not available. Roughness can hint at material/age, and demand can be compared with typical per-capita use, "
    "but those would be assumptions, not data from the model."
)

HELP = (
    "I answer with verified calculations. Things you can ask:\n\n"
    "- **Network:** `network summary` · `check the model for issues` · `dead ends` · `bridges` · `betweenness centrality`\n"
    "- **Pipes:** `pipes with diameter below 100 mm` · `pipes longer than 500 m` · `average pipe diameter`\n"
    "- **Combined:** `pipes under 100 mm with velocity above 0.5 m/s` · `junctions above 80 m elevation with pressure below 30 m`\n"
    "- **Simulation:** `run a 24 h simulation` · `which nodes have the lowest pressure?` · `pipes with the highest velocity` · `oldest water`\n"
    "- **Time:** `when is demand highest?` · `pressure at n22 at 8 am` · `when is pressure lowest at n22` · `tank levels`\n"
    "- **Where:** `nodes within 300 m of R1` · `what is downstream of p235` · `what feeds n22` · `shortest path from n1 to R1`\n"
    "- **Scenarios:** `simulate with demand x1.5 and close p100` · `show low pressure violations`\n"
    "- **Resilience:** `most critical pipes` · `valve segments` · `fire flow at n22` · `fire flow map` · `how much can demand grow?`\n"
    "- **What-if & why:** `what if p239 fails` · `what if PUMP_1 fails` · `what if demand grows by 30%` · `why is pressure low at n22`\n"
    "- **Big picture:** `is this network well designed?` · `compare run 1 and run 2` · `what is PDD?` · `explain betweenness`\n"
    "- **Leaks & quality:** `simulate a 10 mm leak at n100` · `chlorine residual` · `where does the water come from?`\n"
    "- **On screen:** click an element, then ask `what feeds this node` or `pressure here now`\n\n"
    "Connect a free LLM (⚙️ in the chat) to ask in free-form language."
)
