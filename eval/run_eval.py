"""Run the WDN Studio benchmark against one or more "systems" and summarise the results.

Systems
  rules                      built-in rule router (no LLM, no key)
  llm:<provider>[:<model>]   tool-calling assistant (groq | openrouter | gemini | ollama | custom)
  plain:<provider>[:<model>] the same LLM WITHOUT tools: it only sees the network summary (hallucination baseline)

Keys are read from GROQ_API_KEY / OPENROUTER_API_KEY / GEMINI_API_KEY (or LLM_API_KEY + LLM_BASE_URL for custom).

Examples
  python -m eval.run_eval --systems rules
  python -m eval.run_eval --systems rules,llm:groq,plain:groq --sleep 4
  python -m eval.run_eval --systems llm:groq:openai/gpt-oss-20b --networks LT --categories simulation,time

Results are appended to <out>/results.jsonl, so an interrupted run (e.g. a daily free-tier limit) resumes where it
stopped when started again with the same --out.  Summary tables and charts are rebuilt at the end of every run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import warnings
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LLM_AUTO_FALLBACK", "0")   # never silently answer with another provider during evaluation
os.environ.setdefault("LLM_MAX_WAIT_S", "75")     # wait for per-minute limits to reset instead of failing

import wntr  # noqa: E402

from eval.benchmark import build_all  # noqa: E402
from eval.scoring import score  # noqa: E402
from wdn_engine import llm  # noqa: E402
from wdn_engine.model import NetworkModel  # noqa: E402
from wdn_engine.session import Session  # noqa: E402
from wdn_engine.verify import check, evidence_numbers  # noqa: E402

LIB = Path(wntr.__file__).parent / "library" / "networks"
DEFAULT_NETWORKS = {"N3": LIB / "Net3.inp", "LT": ROOT / "samples" / "L-TOWN.inp"}


def load_models(selected: list[str], extra: list[str]) -> dict[str, NetworkModel]:
    paths = dict(DEFAULT_NETWORKS)
    if os.getenv("LTOWN_INP"):
        paths["LT"] = Path(os.environ["LTOWN_INP"])
    for e in extra:   # --network NAME=path/to/file.inp
        name, _, p = e.partition("=")
        paths[name] = Path(p)
    out = {}
    for k, p in paths.items():
        if selected and k not in selected:
            continue
        if not p.is_file():
            print(f"  (skipping {k}: {p} not found — put L-TOWN.inp in samples/ or set LTOWN_INP)")
            continue
        out[k] = NetworkModel.from_inp(str(p))
    return out


def parse_system(spec: str) -> dict[str, Any]:
    parts = spec.split(":", 2)
    kind = parts[0]
    if kind == "rules":
        return {"name": "rules", "kind": "rules"}
    provider = parts[1] if len(parts) > 1 else "groq"
    model = parts[2] if len(parts) > 2 else llm.PROVIDERS.get(provider, {}).get("model")
    return {"name": f"{kind}:{provider}:{model}", "kind": kind, "provider": provider, "model": model}


def plain_answer(model: NetworkModel, question: str, provider: str, mdl: str) -> dict[str, Any]:
    p = llm.PROVIDERS[provider]
    key = os.getenv(p["env"]) if p["env"] else None
    prompt = ("You are an expert in water distribution networks. Answer the question about this EPANET network. "
              "You only know this summary of the model:\n" + json.dumps(model.summary()) + f"\n\nQuestion: {question}")
    data = llm._post(p["base_url"], key, {"model": mdl, "temperature": 0.2, "messages": [{"role": "user", "content": prompt}]})
    u = data.get("usage") or {}
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return {"text": text, "trace": [], "mode": "llm", "usage": {"prompt_tokens": u.get("prompt_tokens", 0),
                                                              "completion_tokens": u.get("completion_tokens", 0), "llm_calls": 1}}


def run_one(system: dict[str, Any], model: NetworkModel, item: dict[str, Any]) -> dict[str, Any]:
    warnings.simplefilter("ignore")
    session = Session(model)
    t0 = time.perf_counter()
    error = None
    try:
        if system["kind"] == "rules":
            r = llm.fallback_answer(session, item["question"])
        elif system["kind"] == "llm":
            r = llm.chat(session, item["question"], [], system["provider"], system["model"],
                         os.getenv(llm.PROVIDERS[system["provider"]]["env"] or "") or None)
            if r.get("mode") != "llm":
                error = "LLM not used: " + (r.get("notice") or "fallback to rules")
        else:
            r = plain_answer(model, item["question"], system["provider"], system["model"])
    except Exception as exc:  # recorded, the run continues
        r, error = {"text": "", "trace": []}, f"{type(exc).__name__}: {exc}"
    latency = time.perf_counter() - t0
    text = r.get("text", "")
    if system["kind"] == "plain":   # check numbers against the true values instead of tool results
        ev = evidence_numbers([n["value"] for n in item["numbers"]], model.summary(), item["question"])
        unver = check(text, ev)["unverified"]
    else:
        unver = (r.get("verification") or {}).get("unverified", [])
    sc = score(item, text, r.get("trace"), unver)
    if error:   # a failed or fallback answer never counts as correct for this system
        sc["correct"] = False
    usage = r.get("usage") or {}
    return {"system": system["name"], "id": item["id"], "network": item["network"], "category": item["category"],
            "split": item.get("split", "dev"),
            "question": item["question"], **sc, "latency_s": round(latency, 2), "error": error,
            "prompt_tokens": usage.get("prompt_tokens", 0), "completion_tokens": usage.get("completion_tokens", 0),
            "llm_calls": usage.get("llm_calls", 0), "answer": text[:1500]}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--systems", default="rules")
    ap.add_argument("--networks", default="", help="comma list of network keys (default: all available)")
    ap.add_argument("--network", action="append", default=[], help="extra network NAME=path.inp (repeatable)")
    ap.add_argument("--categories", default="")
    ap.add_argument("--split", default="", help="dev | heldout (default: both)")
    ap.add_argument("--limit", type=int, default=0, help="max questions per system (0 = all)")
    ap.add_argument("--sleep", type=float, default=0.0, help="seconds to pause between LLM questions (free tiers)")
    ap.add_argument("--out", default=str(ROOT / "eval" / "results"))
    ap.add_argument("--rerun", action="store_true", help="ignore previous results in --out")
    a = ap.parse_args(argv)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    models = load_models([x for x in a.networks.split(",") if x], a.network)
    if not models:
        sys.exit("No networks available.")
    print(f"Building benchmark for {', '.join(models)}…")
    items = [i.to_dict() for i in build_all(models)]
    if a.categories:
        cats = set(a.categories.split(","))
        items = [i for i in items if i["category"] in cats]
    if a.split:
        items = [i for i in items if i["split"] == a.split]
    (out / "benchmark.json").write_text(json.dumps(items, indent=1), encoding="utf-8")
    print(f"  {len(items)} questions")

    res_path = out / "results.jsonl"
    done = set()
    if res_path.exists() and not a.rerun:
        for line in res_path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if not r.get("error"):
                done.add((r["system"], r["id"]))
    elif a.rerun and res_path.exists():
        res_path.unlink()

    for spec in [s for s in a.systems.split(",") if s]:
        system = parse_system(spec)
        if system["kind"] != "rules":
            env = (llm.PROVIDERS.get(system["provider"]) or {}).get("env")
            if system["provider"] not in llm.PROVIDERS:
                print(f"\n== {spec}: unknown provider (use groq, openrouter, gemini, ollama or custom) — skipped")
                continue
            if env and not os.getenv(env):
                print(f"\n== {system['name']}: no API key — skipped. In this same window run:  set {env}=your_key   (no spaces, no quotes)")
                continue
        todo = [i for i in items if (system["name"], i["id"]) not in done]
        if a.limit:
            todo = todo[:a.limit]
        print(f"\n== {system['name']}: {len(todo)} question(s) to run")
        for n, item in enumerate(todo, 1):
            row = run_one(system, models[item["id"].split("-")[0]], item)
            with res_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            mark = "✓" if row["correct"] else ("!" if row["error"] else "✗")
            print(f"  {mark} {item['id']:7s} {item['category']:12s} {row['latency_s']:6.1f}s  {item['question'][:70]}"
                  + (f"   [{row['error'][:80]}]" if row["error"] else ""))
            if row["error"] and "rate limit" in row["error"].lower() and "h (daily" in row["error"]:
                print("  Daily quota reached — stopping this system. Re-run later with the same --out to resume.")
                break
            if system["kind"] != "rules" and a.sleep:
                time.sleep(a.sleep)

    from eval.summarize import summarize
    summarize(out)


if __name__ == "__main__":
    main()
