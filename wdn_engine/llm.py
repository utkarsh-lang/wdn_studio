"""Provider-agnostic LLM assistant using OpenAI-compatible tool calling.

The model never computes numbers: it chooses tools from ``tools.REGISTRY`` and
explains their verified outputs.  Works with any OpenAI-compatible endpoint,
including free tiers (Groq, OpenRouter, Google Gemini) and local Ollama.
If no key is configured or the provider fails, the rule-based router answers.
"""

from __future__ import annotations

import json
import os
import re
from collections import OrderedDict
import time
from typing import Any

import httpx

from .formatting import format_result
from .router import route
from .session import Session
from .tools import PROGRESS, REGISTRY, call_tool, compact
from .verify import caveats as _caveats
from .verify import check as _check_numbers
from .verify import evidence_numbers

# Defaults are editable in the UI; model IDs change often, so the UI can also
# list the models a provider currently offers.
PROVIDERS: dict[str, dict[str, Any]] = {
    "groq": {"label": "Groq (free tier)", "base_url": "https://api.groq.com/openai/v1",
             "model": "openai/gpt-oss-120b", "env": "GROQ_API_KEY", "key_url": "https://console.groq.com/keys"},
    "openrouter": {"label": "OpenRouter (free models)", "base_url": "https://openrouter.ai/api/v1",
                   "model": "openai/gpt-oss-120b:free", "env": "OPENROUTER_API_KEY", "key_url": "https://openrouter.ai/keys"},
    "gemini": {"label": "Google Gemini (free tier)", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "model": "gemini-3.6-flash", "env": "GEMINI_API_KEY", "key_url": "https://aistudio.google.com/apikey"},
    "ollama": {"label": "Ollama (local)", "base_url": "http://localhost:11434/v1", "model": "qwen3:8b", "env": None,
               "key_url": "https://ollama.com"},
    "custom": {"label": "Custom OpenAI-compatible", "base_url": os.getenv("LLM_BASE_URL", ""),
               "model": os.getenv("LLM_MODEL", ""), "env": "LLM_API_KEY", "key_url": ""},
}

MAX_ROUNDS = 6
TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def provider_info() -> list[dict[str, Any]]:
    out = []
    for key, p in PROVIDERS.items():
        out.append({"id": key, "label": p["label"], "default_model": p["model"], "base_url": p["base_url"],
                    "server_key": bool(p["env"] and os.getenv(p["env"])), "env_var": p["env"], "key_url": p["key_url"]})
    return out


def default_provider() -> str | None:
    forced = os.getenv("LLM_PROVIDER")
    if forced in PROVIDERS:
        return forced
    for key, p in PROVIDERS.items():
        if p["env"] and os.getenv(p["env"]):
            return key
    return None


def _context_text(session: Session, context: dict[str, Any] | None) -> str:
    if not context:
        return "nothing selected"
    parts = []
    sel = context.get("selected") or {}
    if sel.get("id"):
        parts.append(f"selected {sel.get('kind', 'element')} {sel['id']} (\"this\", \"it\", \"here\" refer to it)")
    col = context.get("color") or {}
    if col.get("attr"):
        parts.append(f"map coloured by {col['attr']}" + (f" ({col.get('stat')})" if col.get("stat") not in (None, "", "none") else ""))
    if context.get("time_h") is not None and session.sim and len(session.sim.times_h) > 1:
        from .temporal import clock
        parts.append(f"time slider at {float(context['time_h']):.2f} h ({clock(session, float(context['time_h']))}, time_index {context.get('time_index')}) — \"now\" means this time")
    if context.get("highlight"):
        parts.append(f"highlighted: {context['highlight']}")
    runs = [r for r in (context.get("runs") or []) if r.get("sim_id") in session.sims]
    if runs:
        parts.append("RUNS: " + ", ".join(f"#{r['no']} = {r['sim_id']} ({r.get('label', '')})" for r in runs[:8]))
    return "; ".join(parts) or "nothing selected"


def _system_prompt(session: Session, context: dict[str, Any] | None = None) -> str:
    s = session.model.summary()
    sim = session.sim
    crit = session.criteria.to_dict() if session.criteria.is_set() else "not set (ask the user before judging compliance)"
    return f"""You are the analysis assistant of WDN Studio, a web app for water distribution network (EPANET) analysis.

RULES
- Every number you state must come from a tool result in this conversation. Never estimate, recompute or invent values.
- Call tools to answer; you may call several. Prefer one precise tool over many.
- Units: diameters mm, lengths m, flows/demands L/s, pressure m (head), velocity m/s, headloss m/km.
  Convert user units before calling tools (1 inch = 25.4 mm, 1 ft = 0.3048 m).
- Tools also update the map (highlight/colour/select). Mention when something is highlighted.
- Separate facts (tool outputs) from engineering interpretation. Judge pressures/velocities against the
  user's design criteria only; if none are set, say so instead of assuming a standard.
- If a question cannot be answered with the tools, say what data/analysis would be needed.
- Be concise: short paragraphs or bullets, markdown allowed, no tables longer than 10 rows.

LOADED NETWORK: {s['name']} — {s['nodes']} nodes ({s['junctions']} junctions, {s['reservoirs']} reservoirs, {s['tanks']} tanks),
{s['links']} links ({s['pipes']} pipes, {s['pumps']} pumps, {s['valves']} valves); headloss {s['headloss_formula']}; file duration {s['duration_h']} h.
DESIGN CRITERIA: {crit}
LATEST SIMULATION: {sim.id + ' (' + sim.settings.demand_model + ', ' + str(round(float(sim.times_h[-1]), 1)) + ' h)' if sim else 'none yet (tools that need one run it automatically)'}
ON SCREEN: {_context_text(session, context)}
TOOL TIPS: "what happens if…" -> what_if (one call does baseline+scenario+comparison); "why…" at a junction -> diagnose_pressure;
"is the network good / main problems" -> assess_network; definitions -> explain_term (never define terms from memory);
combined or unusual filters -> query_table; "when" questions -> when_extreme; values at a clock time -> value_at_time;
upstream/downstream/fed by -> trace_flow; within X m -> nodes_within; tanks -> tank_events; lowest/highest -> rank_elements.
Reliability: pipe_criticality (N-1 ranking), valve_segments (isolation impact; valves are assumed), fire_flow, demand_growth,
leak_scenario (leak + pressure signature), water_quality (chlorine / age / source shares). These use a peak-hour snapshot
or extra runs; mention their stated assumptions when you report results.
"""


CORE_TOOLS = ["network_summary", "element_info", "query_table", "rank_elements", "simulation_results"]
TOOL_GROUPS: list[tuple[tuple[str, ...], list[str]]] = [
    (("topolog", "dead", "bridge", "articulation", "loop", "mesh", "connect", "characteri", "redundan", "graph",
      "isolat", "source", "diameter of the network"), ["topology_characterization", "distance_statistics"]),
    (("centrality", "betweenness", "closeness", "eigenvector", "degree", "clustering", "important", "critical", "hub"),
     ["node_metric", "link_metric", "topology_characterization"]),
    (("path", "route", "between", " from "), ["shortest_path"]),
    (("within", "near", "around", "radius", "downstream", "upstream", "fed", "feed", "supplied", "supplies", "serve",
      "depend", "area", "zone"), ["nodes_within", "trace_flow"]),
    (("when", "time", "hour", " am", " pm", ":", "day ", "peak", "now", "at t", "morning", "night", "evening"),
     ["value_at_time", "when_extreme", "tank_events"]),
    (("tank", "pump", "energy", "fill", "empt", "storage", "level"), ["tank_events"]),
    (("simulat", "run ", "scenario", "what if", "what-if", "close", "fail", "burst", "break", "shut", "demand x",
      "increase", "growth", "pdd", "pressure-driven", "age", "quality"), ["run_simulation", "compare_runs"]),
    (("compare", "change", "differ", "before", "after", " vs", "versus", "impact", "effect"), ["compare_runs"]),
    (("criteria", "limit", "violat", "standard", "minimum pressure", "max velocity", "compl", "acceptable", "requirement"),
     ["criteria_violations", "set_design_criteria"]),
    (("valid", "issue", "error", "problem", "health", "check", "sanity", "wrong"), ["validation_report"]),
    (("pipe", "diameter", "length", "roughness", "material", "mm", "inch"), ["pipe_statistics", "filter_pipes"]),
    (("colour", "color", "show", "map", "display", "visual"), ["set_map_colour"]),
    (("critical", "criticality", "n-1", "failure", "fail", "important pipe", "vulnerab", "reliab", "resilien", "weak"),
     ["pipe_criticality", "valve_segments"]),
    (("segment", "valve", "isolat", "shut off", "shutdown", "repair", "maintenance"), ["valve_segments"]),
    (("fire", "hydrant"), ["fire_flow"]),
    (("grow", "growth", "capacity", "future", "headroom", "population", "expansion"), ["demand_growth"]),
    (("leak", "loss", "burst", "nrw", "non-revenue", "sensor"), ["leak_scenario"]),
    (("chlorine", "quality", "disinfect", "trace", "source", "come from", "mix", "age"), ["water_quality"]),
    (("what if", "what-if", "what happens", "fail", "failure", "outage", "out of service", "shut", "burst", "break",
      "scenario", "impact", "consequence", "suppose", "if we", "if the"), ["what_if"]),
    (("why", "cause", "reason", "explain the", "diagnos", "low pressure", "high pressure"), ["diagnose_pressure"]),
    (("assess", "evaluat", "well designed", "good design", "problem", "weakness", "strength", "overall", "review",
      "how good", "health", "recommend", "improve"), ["assess_network", "validation_report"]),
    (("what is", "what are", "what does", "meaning", "define", "definition", "explain", "mean by", "difference between"),
     ["explain_term"]),
    (("compare", "run #", "run 1", "run 2", "run 3", "previous run", "last run", "baseline"), ["compare_runs"]),
]


def select_tools(message: str, history: list[dict[str, str]] | None = None,
                 context: dict[str, Any] | None = None) -> list[str]:
    """Only send the tools a question plausibly needs (keeps free-tier token limits in reach)."""
    texts = [message] + [h.get("content", "") for h in (history or [])[-2:] if h.get("role") == "user"]
    low = " " + " ".join(texts).lower() + " "
    names = list(CORE_TOOLS)
    for words, tools in TOOL_GROUPS:
        if any(w in low for w in words):
            names += tools
    if (context or {}).get("selected"):
        names += ["value_at_time", "trace_flow", "when_extreme"]
    if len(set(names)) <= len(CORE_TOOLS):  # vague question: give a broader but still small set
        names += ["topology_characterization", "run_simulation", "validation_report", "pipe_statistics"]
    return [n for i, n in enumerate(names) if n in REGISTRY and n not in names[:i]]


class LLMError(RuntimeError):
    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code if message else "ERROR"


MAX_RATE_WAIT_S = float(os.getenv("LLM_MAX_WAIT_S", "25"))   # wait this long for a rate-limit reset before giving up


def _parse_duration(text: str | None) -> float | None:
    """'7.66s', '2m59.5s', '1h2m3s', '450ms' or plain seconds -> seconds."""
    if not text:
        return None
    t = str(text).strip().lower()
    try:
        return float(t)
    except ValueError:
        pass
    total, found = 0.0, False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(ms|h|m|s)", t):
        found = True
        total += float(num) * {"ms": 0.001, "h": 3600, "m": 60, "s": 1}[unit]
    return total if found else None


def _rate_limit_wait(r: httpx.Response) -> float | None:
    h = r.headers
    for key in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        v = _parse_duration(h.get(key))
        if v is not None:
            return v
    m = re.search(r"try again in\s+([0-9.hms]+)", r.text, re.I)
    return _parse_duration(m.group(1)) if m else None


def _post(base_url: str, api_key: str | None, payload: dict[str, Any], max_wait: float | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if "openrouter.ai" in base_url:
        headers["HTTP-Referer"] = os.getenv("PUBLIC_URL", "https://huggingface.co/spaces")
        headers["X-Title"] = "WDN Studio"
    max_wait = MAX_RATE_WAIT_S if max_wait is None else max_wait
    last, waited, attempts = None, 0.0, 0
    while attempts < 4:
        attempts += 1
        try:
            r = httpx.post(f"{base_url.rstrip('/')}/chat/completions", headers=headers, json=payload, timeout=TIMEOUT)
        except httpx.HTTPError as error:
            last = f"network error: {error}"
            time.sleep(2)
            continue
        text = r.text[:400]
        if r.status_code in (401, 403) or (r.status_code == 400 and any(
                w in text.lower() for w in ("api key", "api_key", "apikey", "invalid authentication", "unauthorized"))):
            raise LLMError("API_KEY", "The provider rejected the API key.")
        if r.status_code == 404:
            raise LLMError("MODEL", "Model or endpoint not found (HTTP 404) — pick another model in ⚙️ (use **List**).")
        if r.status_code == 429:
            wait = _rate_limit_wait(r)
            wait = 3.0 if wait is None else wait + 0.5
            if waited + wait > max_wait:
                when = f" It resets in about {wait:.0f} s." if wait < 3600 else f" It resets in about {wait / 3600:.1f} h (daily quota)."
                raise LLMError("RATE_LIMIT", "Rate limit reached for this provider (free-tier quota)." + when)
            PROGRESS.get()(0.2, f"Free-tier rate limit reached — waiting {wait:.0f} s, then continuing…")
            time.sleep(wait)
            waited += wait
            attempts -= 1  # waiting for a reset is not a failed attempt
            continue
        if r.status_code in (500, 502, 503, 504):
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            time.sleep(2)
            continue
        if r.status_code >= 400:
            raise LLMError("HTTP", f"HTTP {r.status_code}: {text[:250]}")
        return r.json()
    raise LLMError("UNAVAILABLE", f"Provider busy or unreachable after retries ({last}).")


def test_connection(provider: str, model: str | None, api_key: str | None, base_url: str | None) -> dict[str, Any]:
    p = PROVIDERS.get(provider)
    if p is None:
        return {"ok": False, "message": "Unknown provider."}
    key = api_key or (os.getenv(p["env"]) if p["env"] else None)
    source = "entered key" if api_key else (f"server variable {p['env']}" if key else "no key")
    if provider != "ollama" and not key:
        return {"ok": False, "message": f"No API key: paste one from {p['key_url']}"}
    try:
        _post(base_url or p["base_url"], key, {"model": model or p["model"], "max_tokens": 5,
                                               "messages": [{"role": "user", "content": "Reply with OK."}]})
        return {"ok": True, "message": f"✅ Connected to {p['label']} with model {model or p['model']} ({source})."}
    except LLMError as error:
        msg = f"rejected the {source}" if error.code == "API_KEY" else str(error)
        return {"ok": False, "message": f"❌ {p['label']} {msg}"}


def list_models(provider: str, api_key: str | None = None, base_url: str | None = None) -> list[str]:
    p = PROVIDERS.get(provider, PROVIDERS["custom"])
    url = (base_url or p["base_url"]).rstrip("/")
    key = api_key or (os.getenv(p["env"]) if p["env"] else None)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    r = httpx.get(f"{url}/models", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json().get("data", [])
    return sorted(str(m.get("id", "")).removeprefix("models/") for m in data if m.get("id"))


def fallback_answer(session: Session, message: str, note: str | None = None,
                    context: dict[str, Any] | None = None) -> dict[str, Any]:
    plan = route(session, message, context)
    if isinstance(plan, str):
        return {"text": plan, "actions": [], "trace": [], "mode": "rules", "notice": note}
    parts, actions, trace = [], [], []
    results = []
    for name, args in plan:
        PROGRESS.get()(0.3, f"Running {name.replace('_', ' ')}…")
        result, acts = call_tool(session, name, args)
        results.append(result)
        parts.append(format_result(name, result))
        actions += acts
        trace.append({"tool": name, "args": args, "ok": bool(result.get("success", True))})
    return {"text": "\n\n".join(parts), "actions": actions, "trace": trace, "mode": "rules", "notice": note,
            "verification": {"numbers_checked": 0, "unverified": [], "caveats": _caveats(results), "tool_calls": len(trace)}}


class _ToolsRan(Exception):
    """Provider failed after tools already changed state: do not retry elsewhere."""

    def __init__(self, error: LLMError, partial: dict[str, Any]):
        super().__init__(str(error))
        self.error, self.partial = error, partial


def _run_llm(session: Session, message: str, history, provider: str, model: str, key: str | None, url: str,
             context: dict[str, Any] | None) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": _system_prompt(session, context)}]
    budget = 3500  # characters of history kept (free tiers limit tokens per minute)
    kept: list[dict[str, Any]] = []
    for h in reversed((history or [])[-10:]):
        if h.get("role") in {"user", "assistant"} and isinstance(h.get("content"), str):
            content = h["content"][:900]
            budget -= len(content)
            if budget < 0:
                break
            kept.insert(0, {"role": h["role"], "content": content})
    messages += kept
    messages.append({"role": "user", "content": message[:4000]})
    names = select_tools(message, history, context)
    tools = [REGISTRY[n].schema() for n in names]
    actions: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    verified: list[str] = []
    results: list[dict[str, Any]] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0}
    progress = PROGRESS.get()
    for rnd in range(MAX_ROUNDS):
        progress(0.1 + 0.1 * rnd, f"Asking {PROVIDERS.get(provider, {}).get('label', provider)} what to calculate…" if rnd == 0
                 else "Asking the model to continue…")
        try:
            data = _post(url, key, {"model": model, "messages": messages, "tools": tools,
                                    "tool_choice": "auto", "temperature": 0.2})
        except LLMError as error:
            if trace:
                raise _ToolsRan(error, {"text": "\n\n".join(verified), "actions": actions, "trace": trace,
                                        "mode": "rules"}) from error
            raise
        u = data.get("usage") or {}
        usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        usage["completion_tokens"] += int(u.get("completion_tokens") or 0)
        usage["llm_calls"] += 1
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = msg.get("tool_calls") or []
        if not calls:
            text = (msg.get("content") or "").strip() or "\n\n".join(verified) or "The model returned no answer."
            evidence = evidence_numbers(results, message, [h.get("content", "") for h in (history or []) if h.get("role") == "user"],
                                        session.model.summary(), session.criteria.to_dict(), context or {})
            check = _check_numbers(text, evidence) if results or any(ch.isdigit() for ch in text) else {"numbers_checked": 0, "unverified": []}
            return {"text": text, "actions": actions, "trace": trace, "mode": "llm", "provider": provider,
                    "model": model, "verified": verified, "tools_offered": names,
                    "verification": {**check, "caveats": _caveats(results), "tool_calls": len(trace)}, "usage": usage}
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if name not in names and name in REGISTRY:
                names.append(name)  # model asked for a tool it was not offered: allow it next round
                tools.append(REGISTRY[name].schema())
            progress(0.3 + 0.1 * len(trace), f"Running {name.replace('_', ' ')}…")
            result, acts = call_tool(session, name, args if isinstance(args, dict) else {})
            actions += acts
            trace.append({"tool": name, "args": args, "ok": bool(result.get("success", True))})
            verified.append(format_result(name, result))
            results.append(compact(result, max_list=200))
            messages.append({"role": "tool", "tool_call_id": call.get("id", name), "name": name,
                             "content": json.dumps(compact(result, max_list=12), default=str, separators=(",", ":"))[:5000]})
    return {"text": "\n\n".join(verified) + "\n\n_(Stopped after the maximum number of tool rounds.)_",
            "actions": actions, "trace": trace, "mode": "llm", "provider": provider, "model": model,
            "verified": verified, "tools_offered": names}


STATEFUL_TOOLS = {"run_simulation", "set_design_criteria", "compare_runs"}


def _cache_key(session: Session, message: str, provider: str, context: dict[str, Any] | None) -> str:
    sel = ((context or {}).get("selected") or {}).get("id")
    crit = tuple(sorted(session.criteria.to_dict().items()))
    t = (context or {}).get("time_index") if any(w in message.lower() for w in ("now", "this time", "current")) else None
    return json.dumps([" ".join(message.lower().split()), provider, session.active_sim_id, crit, sel, t], default=str)


def _candidates(provider: str | None, model: str | None, api_key: str | None, base_url: str | None,
                fallbacks: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Primary provider first, then user-configured fallbacks, then any provider with a server key."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(pid, mdl=None, key=None, url=None, origin="settings"):
        p = PROVIDERS.get(pid or "")
        if p is None or pid in seen:
            return
        k = key or (os.getenv(p["env"]) if p["env"] else None)
        if pid != "ollama" and not k:
            return
        u = url or p["base_url"]
        if not u:
            return
        seen.add(pid)
        out.append({"provider": pid, "model": mdl or p["model"], "key": k, "url": u, "label": p["label"],
                    "key_source": "the key you entered in ⚙️" if key else f"the server environment variable {p['env']}",
                    "user_key": bool(key), "origin": origin})

    if provider and provider != "rules":
        add(provider, model, api_key, base_url)
    for f in fallbacks or []:
        add(f.get("provider"), f.get("model"), f.get("api_key"), None, "fallback")
    if os.getenv("LLM_AUTO_FALLBACK", "1") == "1":
        for pid, p in PROVIDERS.items():
            if p["env"] and os.getenv(p["env"]):   # only providers with a server key (never probe a local Ollama unasked)
                add(pid, None, None, None, "server")
    return out


def chat(session: Session, message: str, history: list[dict[str, str]] | None = None,
         provider: str | None = None, model: str | None = None, api_key: str | None = None,
         base_url: str | None = None, context: dict[str, Any] | None = None,
         fallbacks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    provider = provider or default_provider()
    if provider in (None, "", "rules"):
        return fallback_answer(session, message, context=context)
    if provider not in PROVIDERS:
        return fallback_answer(session, message, f"Unknown provider '{provider}'; used the built-in rules.", context)

    cache = getattr(session, "answer_cache", None)
    if cache is None:
        cache = session.answer_cache = OrderedDict()
    key = _cache_key(session, message, provider, context)
    if key in cache:
        hit = dict(cache[key])
        hit["cached"] = True
        cache.move_to_end(key)
        return hit

    candidates = _candidates(provider, model, api_key, base_url, fallbacks)
    if not candidates:
        p = PROVIDERS[provider]
        return fallback_answer(session, message, f"No API key for {p['label']}; answered with the built-in rules.", context)
    failures: list[str] = []
    for c in candidates:
        try:
            out = _run_llm(session, message, history, c["provider"], c["model"], c["key"], c["url"], context)
        except _ToolsRan as stop:
            out = stop.partial
            out["notice"] = f"{c['label']} stopped mid-answer ({stop.error}); showing the verified results computed so far."
            return out
        except LLMError as error:
            if error.code == "API_KEY":
                why = f"{c['label']} rejected {c['key_source']}" + (
                    "" if c["user_key"] else f" (remove/replace {PROVIDERS[c['provider']]['env']} and restart the app)")
            elif error.code == "RATE_LIMIT":
                why = f"{c['label']}: {str(error).replace('Rate limit reached for this provider (free-tier quota).', 'free-tier rate limit reached.').strip()}"
            else:
                why = f"{c['label']}: {error}"
            failures.append(why)
            continue
        if failures:
            out["notice"] = "; ".join(failures) + f" — answered by {c['label']} instead."
        if not any(t["tool"] in STATEFUL_TOOLS for t in out["trace"]) and not any(
                a.get("type") == "simulation" for a in out["actions"]):
            cache[key] = {k: v for k, v in out.items() if k != "notice"}
            while len(cache) > 100:
                cache.popitem(last=False)
        return out
    return fallback_answer(session, message, "LLM unavailable — " + "; ".join(failures) + ". Answered with the built-in rules.",
                           context)
