"""Scoring of one answer against a benchmark item."""

from __future__ import annotations

import re
from typing import Any

from wdn_engine.verify import NUM_RE


def answer_numbers(text: str) -> list[tuple[float, int]]:
    out = []
    for m in NUM_RE.finditer(text or ""):
        raw = m.group(1).replace(",", "")
        try:
            v = float(raw)
        except ValueError:
            continue
        out.append((v, len(raw.split(".")[1]) if "." in raw else 0))
    return out


def number_found(expected: dict[str, Any], nums: list[tuple[float, int]]) -> bool:
    targets = [expected["value"]] + list(expected.get("alts") or [])
    for t in targets:
        for v, dec in nums:
            tol = max(expected.get("abs", 0.0), expected.get("rel", 0.0) * abs(t), 0.5 * 10 ** (-dec) if expected.get("rel", 0) > 0 else 0.0)
            if abs(v - t) <= tol + 1e-9:
                return True
    return False


def id_found(eid: str, text: str) -> bool:
    return re.search(rf"(?<![\w.]){re.escape(str(eid))}(?![\w])", text or "", re.I) is not None


def score(item: dict[str, Any], answer: str, trace: list[dict[str, Any]] | None, unverified: list[str] | None) -> dict[str, Any]:
    text = answer or ""
    low = text.lower()
    nums = answer_numbers(text)
    num_ok = [number_found(n, nums) for n in item["numbers"]]
    ids_ok = [id_found(i, text) for i in item["ids"]]
    kw_ok = [any(w in low for w in group) for group in item["keywords"]]
    invented = False
    if item.get("no_numbers"):
        # numbers that are not IDs mentioned in the question count as invented facts
        q_nums = {v for v, _ in answer_numbers(item["question"])}
        invented = any(v not in q_nums for v, _ in nums if not (v.is_integer() and v <= 3))
    correct = all(num_ok) and all(ids_ok) and all(kw_ok) and not invented
    tools_used = [t.get("tool") for t in (trace or [])]
    tool_ok = (None if not item["tools"] else any(t in item["tools"] for t in tools_used))
    return {"correct": bool(correct), "numbers_ok": f"{sum(num_ok)}/{len(num_ok)}", "ids_ok": f"{sum(ids_ok)}/{len(ids_ok)}",
            "keywords_ok": f"{sum(kw_ok)}/{len(kw_ok)}", "invented_numbers": invented, "tool_ok": tool_ok,
            "tools_used": ",".join(t for t in tools_used if t), "unverified_count": len(unverified or [])}
