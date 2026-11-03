"""API smoke tests, including the LLM tool-calling loop against a fake provider."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from wdn_engine import llm

client = TestClient(app)


@pytest.fixture(scope="module")
def sid():
    r = client.post("/api/networks/sample/Net3")
    assert r.status_code == 200
    return r.json()["session_id"]


def test_samples_and_geometry(sid):
    assert any(s["name"] == "Net3" for s in client.get("/api/samples").json())
    g = client.get(f"/api/s/{sid}/geometry").json()
    assert len(g["nodes"]) == 97 and len(g["links"]) == 119


def test_upload_rejects_non_inp():
    r = client.post("/api/networks", files={"file": ("x.txt", b"hello")})
    assert r.status_code == 400


def test_simulation_flow(sid):
    r = client.post(f"/api/s/{sid}/simulate", json={"duration_h": 24, "water_age": True})
    assert r.status_code == 200, r.text
    sim = r.json()
    f = client.get(f"/api/s/{sid}/sim/{sim['sim_id']}/frame", params={"var": "pressure", "stat": "min"}).json()
    assert len(f["values"]) == 97
    s = client.get(f"/api/s/{sid}/sim/{sim['sim_id']}/series/node/10").json()
    assert "age" in s["series"]
    r = client.put(f"/api/s/{sid}/criteria", json={"min_pressure_m": 20, "label": "test"})
    assert r.json()["simulation"]["criteria"]["low_pressure"]["limit_m"] == 20


def test_tool_endpoint(sid):
    r = client.post(f"/api/s/{sid}/tool/node_metric", json={"metric": "betweenness_centrality"}).json()
    assert r["result"]["success"] and r["actions"][0]["type"] == "color"


def test_chat_rules_fallback(sid):
    r = client.post(f"/api/s/{sid}/chat", json={"message": "pipes longer than 1000 m", "provider": "rules"}).json()
    assert r["mode"] == "rules" and "pipe(s)" in r["text"]


def test_chat_llm_tool_loop(sid, monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        if len(calls) == 1:
            body = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "filter_pipes", "arguments":
                 '{"conditions":[{"field":"diameter_mm","op":">=","value":300}]}'}}]}}]}
        else:
            tool_msg = [m for m in json["messages"] if m["role"] == "tool"][0]
            n = __import__("json").loads(tool_msg["content"])["number_of_matching_pipes"]
            body = {"choices": [{"message": {"role": "assistant", "content": f"There are {n} large pipes."}}]}
        return httpx.Response(200, json=body)

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    r = client.post(f"/api/s/{sid}/chat", json={"message": "how many big pipes?", "provider": "groq", "api_key": "x"}).json()
    assert r["mode"] == "llm" and r["trace"][0]["tool"] == "filter_pipes"
    assert r["actions"][0]["type"] == "highlight"
    assert "large pipes" in r["text"]
    assert calls[0]["tools"] and calls[0]["messages"][0]["role"] == "system"


def test_chat_llm_bad_key_falls_back(sid, monkeypatch):
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: httpx.Response(401, json={"error": "bad"}))
    r = client.post(f"/api/s/{sid}/chat", json={"message": "network summary", "provider": "groq", "api_key": "bad"}).json()
    assert r["mode"] == "rules" and "rejected" in r["notice"] and "API key" not in r["text"]


def test_custom_url_blocked_by_default(sid, monkeypatch):
    seen = []
    monkeypatch.setattr(llm.httpx, "post", lambda url, **k: seen.append(url) or httpx.Response(401))
    client.post(f"/api/s/{sid}/chat", json={"message": "summary", "provider": "custom", "api_key": "k",
                                            "base_url": "http://169.254.169.254"})
    assert not any("169.254" in u for u in seen)


def test_gemini_style_400_key_error(sid, monkeypatch):
    body = [{"error": {"code": 400, "message": "Please pass a valid API key", "status": "INVALID_ARGUMENT"}}]
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: httpx.Response(400, json=body))
    r = client.post(f"/api/s/{sid}/chat", json={"message": "summary", "provider": "gemini", "api_key": "x"}).json()
    assert "rejected the key you entered" in r["notice"]
    t = client.post("/api/llm/test", json={"provider": "gemini", "api_key": "x"}).json()
    assert t["ok"] is False


def test_excel_exports(sid):
    sim = client.post(f"/api/s/{sid}/simulate", json={"duration_h": 12}).json()
    x = client.get(f"/api/s/{sid}/sim/{sim['sim_id']}/export.xlsx")
    assert x.status_code == 200 and x.content[:2] == b"PK"
    n = client.get(f"/api/s/{sid}/export/network.xlsx")
    assert n.status_code == 200 and n.content[:2] == b"PK"


def _final(text):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})


def test_fallback_after_rate_limit_and_cache(sid, monkeypatch):
    seen = []

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.append(url)
        if "groq" in url:
            return httpx.Response(429, json={"error": "rate"})
        return _final("answer from fallback")

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    body = {"message": "describe the network please", "provider": "groq", "api_key": "g",
            "fallbacks": [{"provider": "openrouter", "api_key": "o"}]}
    r = client.post(f"/api/s/{sid}/chat", json=body).json()
    assert r["text"] == "answer from fallback" and r["provider"] == "openrouter"
    assert "rate limit" in r["notice"]
    calls = len(seen)
    r2 = client.post(f"/api/s/{sid}/chat", json=body).json()
    assert r2.get("cached") and len(seen) == calls  # served from cache, no new request


def test_context_reaches_prompt_and_tool_subset(sid, monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json)
        return _final("ok")

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    ctx = {"selected": {"kind": "node", "id": "10"}, "color": {"attr": "pressure", "stat": "min"}}
    client.post(f"/api/s/{sid}/chat", json={"message": "when is pressure lowest here?", "provider": "groq",
                                            "api_key": "k", "context": ctx})
    system = captured["messages"][0]["content"]
    assert "selected node 10" in system and "coloured by pressure" in system
    names = {t["function"]["name"] for t in captured["tools"]}
    assert "when_extreme" in names and "node_metric" not in names


def test_rules_use_context_and_new_tools(sid):
    ctx = {"selected": {"kind": "node", "id": "10"}}
    r = client.post(f"/api/s/{sid}/chat", json={"message": "what feeds this node", "provider": "rules", "context": ctx}).json()
    assert r["trace"][0]["tool"] == "trace_flow" and r["trace"][0]["args"]["element"] == "10"
    r = client.post(f"/api/s/{sid}/chat", json={"message": "when is demand highest?", "provider": "rules"}).json()
    assert r["trace"][0]["tool"] == "when_extreme" and "Day" in r["text"]


def test_llm_answer_numbers_are_checked(sid, monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        tool_msgs = [m for m in json["messages"] if m["role"] == "tool"]
        if not tool_msgs:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "network_summary", "arguments": "{}"}}]}}]})
        import json as _j
        n = _j.loads(tool_msgs[0]["content"])["junctions"]
        return _final(f"The network has {n} junctions and roughly 4321 customers.")

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    r = client.post(f"/api/s/{sid}/chat", json={"message": "how big is it?", "provider": "groq", "api_key": "k"}).json()
    v = r["verification"]
    assert v["unverified"] == ["4321"] and v["numbers_checked"] == 2


def test_chat_async_reports_steps(sid):
    import time as _t
    job = client.post(f"/api/s/{sid}/chat_async", json={"message": "what if pipe 10 fails", "provider": "rules"}).json()
    for _ in range(200):
        job = client.get(f"/api/s/{sid}/jobs/{job['job_id']}").json()
        if job["status"] in ("done", "error"):
            break
        _t.sleep(0.1)
    assert job["status"] == "done", job.get("error")
    assert any("Step 2/4" in s for s in job["steps"]) and "What if" in job["text"]


def test_glossary_and_explain(sid):
    g = client.get("/api/glossary").json()
    assert "pdd" in g and "betweenness" in g
    r = client.post(f"/api/s/{sid}/chat", json={"message": "what is PDD?", "provider": "rules"}).json()
    assert "Pressure-driven" in r["text"]


def test_edit_undo_and_export(sid):
    f = client.get(f"/api/s/{sid}/editable/link/10").json()
    assert f["type"] in ("Pump", "Pipe")
    r = client.patch(f"/api/s/{sid}/element/link/20", json={"changes": {"diameter_mm": 500}})
    assert r.status_code == 200 and r.json()["edits"][0]["new"] == 500
    g = client.get(f"/api/s/{sid}/geometry").json()
    assert next(l for l in g["links"] if l["id"] == "20")["diameter_mm"] == 500
    bad = client.patch(f"/api/s/{sid}/element/link/20", json={"changes": {"diameter_mm": -1}})
    assert bad.status_code == 400
    inp = client.get(f"/api/s/{sid}/export").text
    assert "20 " in inp
    u = client.post(f"/api/s/{sid}/undo").json()
    assert u["undone"]["field"] == "diameter_mm"


def test_project_roundtrip(sid):
    client.patch(f"/api/s/{sid}/element/node/10", json={"changes": {"elevation_m": 222}})
    client.put(f"/api/s/{sid}/criteria", json={"min_pressure_m": 21, "label": "proj"})
    client.post(f"/api/s/{sid}/simulate", json={"duration_h": 6, "demand_multiplier": 1.2})
    proj = client.post(f"/api/s/{sid}/project", json={"chat": [{"role": "user", "content": "hello"}], "view": {"k": 2}})
    assert proj.status_code == 200
    r = client.post("/api/projects", files={"file": ("p.wdnproj", proj.content)}).json()
    assert r["criteria"]["min_pressure_m"] == 21 and r["project"]["chat"][0]["content"] == "hello"
    assert any(run["settings"]["demand_multiplier"] == 1.2 for run in r["runs"])
    node = client.get(f"/api/s/{r['session_id']}/element/node/10").json()
    assert node["elevation_m"] == pytest.approx(222, abs=0.01)


def test_charts_and_report(sid):
    import time as _t
    a = client.post(f"/api/s/{sid}/simulate", json={"duration_h": 12}).json()
    c = client.get(f"/api/s/{sid}/sim/{a['sim_id']}/charts").json()
    assert sum(c["pressure_hist"]["counts"][0]) == 92 and c["tanks"]
    for fmt, magic in (("docx", b"PK"), ("pdf", b"%PDF")):
        job = client.post(f"/api/s/{sid}/report", json={"format": fmt, "style": "thesis", "maps": False, "assessment": False}).json()
        for _ in range(300):
            job = client.get(f"/api/s/{sid}/jobs/{job['job_id']}").json()
            if job["status"] in ("done", "error"):
                break
            _t.sleep(0.1)
        assert job["status"] == "done", job.get("error")
        f = client.get(f"/api/s/{sid}/report/{job['job_id']}")
        assert f.status_code == 200 and f.content[:len(magic)] == magic


def test_rate_limit_wait_then_success(sid, monkeypatch):
    calls, slept = [], []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"x-ratelimit-reset-tokens": "4.2s"}, json={"error": "rate"})
        return _final("fine after waiting")

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    r = client.post(f"/api/s/{sid}/chat", json={"message": "describe the network please again", "provider": "groq", "api_key": "g"}).json()
    assert r["text"] == "fine after waiting" and slept and 4 <= slept[0] <= 5


def test_rate_limit_too_long_reports_reset(sid, monkeypatch):
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: httpx.Response(429, headers={"retry-after": "3600"}, json={}))
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    r = client.post(f"/api/s/{sid}/chat", json={"message": "network summary please", "provider": "groq", "api_key": "g"}).json()
    assert r["mode"] == "rules" and "resets in about" in r["notice"]
