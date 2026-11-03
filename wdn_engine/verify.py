"""Check that every number in an LLM answer is backed by a tool result (or the user's own words).

A number counts as verified if some number from the evidence matches it at the precision it is
written with (e.g. 7.4 matches 7.3854), within 0.5 %, or as a ratio/percentage (0.11 ↔ 11 %).
Anything else is reported so the UI can mark it "⚠ unverified".
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

NUM_RE = re.compile(r"(?<![\w.\-/])(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)(?![\w])")
LIST_MARK_RE = re.compile(r"^\s*\d+[.)]\s", re.M)


def _numbers(text: str) -> list[tuple[str, float, int]]:
    out = []
    for m in NUM_RE.finditer(text):
        raw = m.group(1)
        clean = raw.replace(",", "")
        try:
            val = float(clean)
        except ValueError:
            continue
        decimals = len(clean.split(".")[1]) if "." in clean else 0
        out.append((raw, val, decimals))
    return out


def evidence_numbers(*sources: Any) -> list[float]:
    vals: list[float] = []
    for src in sources:
        text = src if isinstance(src, str) else json.dumps(src, default=str)
        vals += [v for _, v, _ in _numbers(text)]
    return vals


def _matches(value: float, decimals: int, evidence: Iterable[float]) -> bool:
    for x in evidence:
        for cand in (x, x * 100, x / 100, -x):
            if round(cand, decimals) == round(value, decimals):
                return True
            if cand and abs(cand - value) <= 0.005 * abs(cand):
                return True
    return False


def check(answer: str, evidence: list[float], ignore_small: int = 3) -> dict[str, Any]:
    body = LIST_MARK_RE.sub(" ", answer)
    body = re.sub(r"`[^`]*`", " ", body)          # IDs and code are not claims
    found = _numbers(body)
    unverified, checked = [], 0
    ev = sorted(set(evidence))
    for raw, val, dec in found:
        if dec == 0 and abs(val) <= ignore_small:
            continue                               # list counters, "2 sources", "1 pipe" …
        checked += 1
        if not _matches(val, dec, ev):
            if raw not in unverified:
                unverified.append(raw)
    return {"numbers_checked": checked, "unverified": unverified}


def caveats(results: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    blob = json.dumps(results, default=str).lower()
    if '"approximate": true' in blob or "sampled" in blob:
        out.append("Some graph metrics were sampled (approximate) for speed.")
    if "peak-hour snapshot" in blob:
        out.append("Uses a peak-hour snapshot (tank levels frozen after failures).")
    if "steady state (one time step)" in blob:
        out.append("Based on a steady-state (single time step) run.")
    if "assumed" in blob and "valve" in blob:
        out.append("Valve layout was assumed (not in the model file).")
    if "negative pressures under demand-driven" in blob:
        out.append("Demand-driven results contain negative pressures; pressure-driven (PDD) is more realistic.")
    return out
