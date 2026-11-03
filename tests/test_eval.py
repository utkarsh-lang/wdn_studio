"""Tests for the evaluation harness (benchmark, scoring, runner with a fake LLM)."""

import json
from pathlib import Path

import pytest
import wntr

from eval import run_eval
from eval.benchmark import build
from eval.scoring import score
from wdn_engine import llm
from wdn_engine.model import NetworkModel

NET1 = Path(wntr.__file__).parent / "library" / "networks" / "Net1.inp"


@pytest.fixture(scope="module")
def net1():
    return NetworkModel.from_inp(str(NET1))


def _item(**kw):
    base = {"id": "X-1", "network": "x", "category": "lookup", "question": "q", "numbers": [], "ids": [],
            "keywords": [], "tools": [], "no_numbers": False}
    base.update(kw)
    return base


def test_scoring_numbers_ids_and_rounding():
    it = _item(numbers=[{"value": 12.345, "rel": 0.01, "abs": 0, "alts": []}, {"value": 7, "rel": 0, "abs": 0, "alts": []}],
               ids=["P10"], tools=["query_table"])
    s = score(it, "Pipe P10: 12.3 m, count 7", [{"tool": "query_table"}], [])
    assert s["correct"] and s["tool_ok"]
    assert not score(it, "Pipe P10: 12.3 m, count 8", [], [])["correct"]       # counts are exact
    assert not score(it, "Pipe P101: 12.3 m, count 7", [], [])["correct"]      # ID must match whole


def test_scoring_unanswerable_rejects_invented_numbers():
    it = _item(no_numbers=True, keywords=[["not in the model", "not available", "does not contain"]])
    assert score(it, "Pipe material is not in the model.", [], [])["correct"]
    s = score(it, "Not in the model, but probably 1985.", [], [])
    assert s["invented_numbers"] and not s["correct"]


def test_benchmark_builds_dev_and_heldout(net1):
    items = build(net1, "N1")
    assert items and len({i.id for i in items}) == len(items)
    splits = {i.split for i in items}
    assert splits == {"dev", "heldout"}
    for i in items:
        assert i.numbers or i.ids or i.keywords, i.id
        if i.split == "heldout":
            assert i.id.endswith("H")


def test_runner_with_fake_llm_resumes(net1, tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "load_models", lambda sel, extra: {"N1": net1})

    def fake_chat(session, message, history, provider, model, key, *a, **k):
        r = llm.fallback_answer(session, message)   # answer with the rules, but mark as LLM mode
        r["mode"] = "llm"
        return r

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    run_eval.main(["--systems", "rules,llm:groq", "--categories", "lookup", "--out", str(tmp_path), "--rerun"])
    rows = [json.loads(x) for x in (tmp_path / "results.jsonl").read_text().splitlines()]
    systems = {r["system"] for r in rows}
    assert "rules" in systems and any(s.startswith("llm:groq") for s in systems)
    assert all(r["error"] is None for r in rows)
    for f in ["summary.csv", "results.md", "headline_metrics.png", "accuracy_by_category.png"]:
        assert (tmp_path / f).exists()
    n = len(rows)
    run_eval.main(["--systems", "rules", "--categories", "lookup", "--out", str(tmp_path)])   # resume: nothing new
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == n
