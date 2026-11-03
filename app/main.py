"""WDN Studio web API + static frontend.

Run locally:  uvicorn app.main:app --reload --port 7860
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import wntr
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from wdn_engine import export as X
from wdn_engine import llm
from wdn_engine import topology as T
from wdn_engine.criteria import PRESETS, DesignCriteria
from wdn_engine.formatting import format_result
from wdn_engine.hydraulics import LINK_VARS, NODE_VARS, SimSettings
from wdn_engine.model import NetworkModel
from wdn_engine.session import Session, SessionStore
from wdn_engine.tools import call_tool
from wdn_engine.validation import validate

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
SAMPLE_DIRS = [Path(wntr.__file__).parent / "library" / "networks", ROOT.parent / "samples"]
SAMPLE_INFO = {
    "Net1": "EPANET example: 11 nodes, pump + tank",
    "Net2": "EPANET example: 36 nodes, tank, fluoride tracer",
    "Net3": "EPANET example: 97 nodes, 2 pumps, 3 tanks",
    "ky4": "Kentucky KY4: ~960 nodes, rural",
    "ky10": "Kentucky KY10: ~930 nodes",
    "Net6": "Large utility model: ~3,300 nodes",
}

app = FastAPI(title="WDN Studio", version="0.1.0")
store = SessionStore()
ALLOW_CUSTOM_URL = os.getenv("ALLOW_CUSTOM_LLM_URL", "0") == "1"  # off on public deployments (SSRF guard)


def _session(sid: str) -> Session:
    s = store.get(sid)
    if s is None:
        raise HTTPException(404, "Session expired or not found. Load the network again.")
    return s


def _bad(error: Exception) -> HTTPException:
    return HTTPException(400, str(error))


def _load_payload(session: Session) -> dict[str, Any]:
    return {"session_id": session.id, "summary": session.model.summary(), "validation": validate(session.model),
            "criteria": session.criteria.to_dict()}


# ------------------------------------------------------------------ loading
@app.get("/api/samples")
def samples() -> list[dict[str, str]]:
    out = []
    for d in SAMPLE_DIRS:
        if d.is_dir():
            for p in sorted(d.glob("*.inp")):
                out.append({"name": p.stem, "description": SAMPLE_INFO.get(p.stem, "Sample network")})
    return out


@app.post("/api/networks/sample/{name}")
def load_sample(name: str) -> dict[str, Any]:
    for d in SAMPLE_DIRS:
        p = d / f"{name}.inp"
        if p.is_file():
            return _load_payload(store.add(Session(NetworkModel.from_inp(str(p), name))))
    raise HTTPException(404, f"Sample '{name}' not found.")


@app.post("/api/networks")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    if not (file.filename or "").lower().endswith(".inp"):
        raise HTTPException(400, "Please upload an EPANET .inp file.")
    data = await file.read()
    if len(data) > NetworkModel.MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(413, f"File larger than {NetworkModel.MAX_FILE_MB} MB.")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "network.inp"
        path.write_bytes(data)
        try:
            model = NetworkModel.from_inp(str(path), Path(file.filename).stem)
            model.layout  # resolve coordinates now
        except Exception as error:
            raise _bad(error)
    return _load_payload(store.add(Session(model)))


@app.get("/api/s/{sid}/geometry")
def geometry(sid: str) -> dict[str, Any]:
    m = _session(sid).model
    return {"nodes": m.nodes(), "links": m.links(), "layout_generated": m.layout_generated}


@app.get("/api/s/{sid}/export")
def export_inp(sid: str, background: BackgroundTasks):
    m = _session(sid).model
    tmp = tempfile.NamedTemporaryFile(suffix=".inp", delete=False)
    tmp.close()
    wntr.network.write_inpfile(m.wn, tmp.name)
    background.add_task(os.remove, tmp.name)
    return FileResponse(tmp.name, filename=f"{m.name}.inp", media_type="text/plain")


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx(data: bytes, name: str) -> Response:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return Response(data, media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="{safe}"'})


@app.get("/api/s/{sid}/export/network.xlsx")
def export_network_xlsx(sid: str) -> Response:
    m = _session(sid).model
    return _xlsx(X.network_workbook(m), f"{m.name}_network.xlsx")


@app.get("/api/s/{sid}/sim/{sim_id}/export.xlsx")
def export_sim_xlsx(sid: str, sim_id: str) -> Response:
    s = _session(sid)
    sim = s.sims.get(sim_id)
    if sim is None:
        raise HTTPException(404, "Simulation not found.")
    return _xlsx(X.simulation_workbook(s.model, sim, s.criteria), f"{s.model.name}_simulation_{sim_id}.xlsx")


@app.get("/api/s/{sid}/compare")
def compare_runs(sid: str, base: str, scenario: str, var: str = "pressure", stat: str | None = "min", t: int = 0) -> dict[str, Any]:
    from wdn_engine.hydraulics import compare
    s = _session(sid)
    a, b = s.sims.get(base), s.sims.get(scenario)
    if a is None or b is None:
        raise HTTPException(404, "Simulation not found.")
    if var not in NODE_VARS and var not in LINK_VARS:
        raise HTTPException(400, f"Unknown variable '{var}'.")
    try:
        res = compare(a, b, var, None if stat in (None, "", "none") else stat, t)
    except KeyError as error:
        raise HTTPException(400, str(error))
    return {**res, "markdown": format_result("compare_runs", res)}


# ----------------------------------------------------------------- analysis
@app.get("/api/s/{sid}/element/{kind}/{eid}")
def element(sid: str, kind: str, eid: str) -> dict[str, Any]:
    res, _ = call_tool(_session(sid), "element_info", {"kind": kind, "id": eid})
    if not res.get("success"):
        raise HTTPException(404, res.get("error"))
    return res


@app.get("/api/s/{sid}/characterization")
def characterization(sid: str) -> dict[str, Any]:
    res = T.characterization(_session(sid).model)
    return {**res, "markdown": format_result("topology_characterization", res)}


@app.get("/api/s/{sid}/pipe-statistics")
def pipe_stats(sid: str) -> dict[str, Any]:
    res = T.pipe_statistics(_session(sid).model)
    return {**res, "markdown": format_result("pipe_statistics", res)}


@app.get("/api/s/{sid}/metric/{target}/{name}")
def metric(sid: str, target: str, name: str, weighted: bool = False) -> dict[str, Any]:
    m = _session(sid).model
    res = T.node_metric(m, name, weighted) if target == "node" else T.link_metric(m, name, weighted)
    if not res.get("success"):
        raise HTTPException(400, res.get("error"))
    return {**res, "markdown": format_result("node_metric" if target == "node" else "link_metric", res)}


@app.get("/api/s/{sid}/distances")
def distances(sid: str, weighted: bool = False) -> dict[str, Any]:
    res = T.distance_statistics(_session(sid).model, weighted)
    return {**res, "markdown": format_result("distance_statistics", res)}


@app.get("/api/s/{sid}/path")
def path(sid: str, source: str, target: str, weighted: bool = True) -> dict[str, Any]:
    res = T.shortest_path(_session(sid).model, source, target, weighted)
    if not res.get("success"):
        raise HTTPException(400, res.get("error"))
    return {**res, "markdown": format_result("shortest_path", res)}


@app.post("/api/s/{sid}/tool/{name}")
def run_tool(sid: str, name: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run any registered analysis tool directly (the UI uses the same tools as the assistant)."""
    s = _session(sid)
    result, actions = call_tool(s, name, body or {})
    return {"result": result, "markdown": format_result(name, result), "actions": actions}


# ---------------------------------------------------- long analyses (background jobs)
ANALYSES = {"pipe_criticality", "valve_segments", "fire_flow", "demand_growth", "leak_scenario", "water_quality"}


@app.post("/api/s/{sid}/analysis/{name}")
def start_analysis(sid: str, name: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Start a long analysis in the background; poll /jobs/{job_id} for progress and the result."""
    from wdn_engine import jobs, resilience as R
    from wdn_engine.tools import REGISTRY
    s = _session(sid)
    if name not in ANALYSES:
        raise HTTPException(404, f"Unknown analysis '{name}'.")
    args = dict(body or {})
    direct = {"pipe_criticality": lambda p: R.pipe_criticality(s, args.get("required_pressure_m"), args.get("include", "pipes"),
                                                               args.get("time_h"), progress=p),
              "valve_segments": lambda p: R.valve_segments(s, args.get("placement", "strategic"), int(args.get("n", 2)),
                                                           hydraulic=bool(args.get("hydraulic", True)), progress=p),
              "fire_flow": lambda p: R.fire_flow(s, args.get("node"), float(args.get("residual_pressure_m", 14)),
                                                 args.get("required_flow_lps"), all_nodes=bool(args.get("all_nodes")),
                                                 system_min_pressure=args.get("system_min_pressure_m"), progress=p),
              "demand_growth": lambda p: R.demand_growth(s, args.get("min_pressure_m"), progress=p)}

    def run(progress):
        if name in direct:
            # full result (incl. 'all' rows for tables/CSV) + the same actions the chat tool would produce
            result = direct[name](progress)
            if result.get("success"):
                s.results[name] = {k: v for k, v in result.items() if k not in ("values", "all")}
            if not result.get("success"):
                return {"result": result, "markdown": format_result(name, result), "actions": []}
            slim = {k: v for k, v in result.items() if k != "all"}
            _, actions = _actions_for(s, name, slim)
            return {"result": result, "markdown": format_result(name, slim), "actions": actions}
        progress(0.1, "Running simulations…")
        result, actions = call_tool(s, name, args)
        return {"result": result, "markdown": format_result(name, result), "actions": actions}

    job = jobs.submit(s, name, run)
    return job.public()


def _actions_for(s: Session, name: str, res: dict[str, Any]):
    from wdn_engine import tools as TL
    if name == "pipe_criticality":
        top = [r["id"] for r in res["top"][:10] if r.get("demand_shortfall_lps", 0) > 0 or r.get("max_pressure_drop_m", 0) > 0.5]
        return res, TL._layer_actions(s, "criticality", top, "link", f"Top {len(top)} critical links")
    if name == "valve_segments":
        worst = s.segment_rows.get(res["top"][0]["segment"]) if res["top"] else None
        acts = [{"type": "layer", "name": "segments"}]
        if worst:
            acts.append(TL._highlight(nodes=worst["_nodes"], links=worst["_links"], label=f"Most critical segment #{worst['segment']}"))
        return res, acts
    if name == "fire_flow":
        if "weakest" in res:
            return res, TL._layer_actions(s, "fire_flow", [r["id"] for r in res["weakest"][:10]], "node", "Weakest fire-flow nodes")
        return res, [{"type": "select", "kind": "node", "id": res["id"]}]
    if name == "demand_growth":
        nodes = res.get("first_failing_nodes") or []
        acts = [TL._highlight(nodes=nodes, label="First nodes to fail with growth")] if nodes else []
        if res.get("eps_check"):
            acts.append({"type": "simulation", "sim_id": res["eps_check"]["sim_id"]})
        return res, acts
    return res, []


@app.get("/api/s/{sid}/jobs/{job_id}")
def job_status(sid: str, job_id: str) -> dict[str, Any]:
    s = _session(sid)
    job = getattr(s, "jobs", {}).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    out = job.public()
    if job.status == "done":
        out.update(job.result)
    return out


@app.get("/api/s/{sid}/layers")
def list_layers(sid: str) -> list[dict[str, Any]]:
    return [{"name": k, "title": v["title"], "target": v["target"], "unit": v["unit"]} for k, v in _session(sid).layers.items()]


@app.get("/api/s/{sid}/layers/{name}")
def get_layer(sid: str, name: str) -> dict[str, Any]:
    layer = _session(sid).layers.get(name)
    if layer is None:
        raise HTTPException(404, "Layer not found (run the analysis first).")
    return {"name": name, **layer}


# ------------------------------------------------------------- editing & projects
@app.get("/api/s/{sid}/editable/{kind}/{eid}")
def editable(sid: str, kind: str, eid: str) -> dict[str, Any]:
    from wdn_engine.edits import editable_fields
    r = editable_fields(_session(sid), kind, eid)
    if not r["success"]:
        raise HTTPException(404, r["error"])
    return r


@app.patch("/api/s/{sid}/element/{kind}/{eid}")
def edit_element(sid: str, kind: str, eid: str, body: dict[str, Any]) -> dict[str, Any]:
    from wdn_engine.edits import apply_edit
    r = apply_edit(_session(sid), kind, eid, body.get("changes") or {})
    if not r["success"]:
        raise HTTPException(400, r["error"])
    return r


@app.post("/api/s/{sid}/undo")
def undo(sid: str) -> dict[str, Any]:
    from wdn_engine.edits import undo_edit
    r = undo_edit(_session(sid))
    if not r["success"]:
        raise HTTPException(400, r["error"])
    return r


@app.get("/api/s/{sid}/edits")
def list_edits(sid: str) -> list[dict[str, Any]]:
    return _session(sid).__dict__.get("edits", [])


@app.post("/api/s/{sid}/project")
def save_project(sid: str, body: dict[str, Any] | None = None) -> Response:
    import json as _json
    from wdn_engine.edits import project_json
    s = _session(sid)
    doc = project_json(s, (body or {}).get("chat"), (body or {}).get("view"))
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in s.model.name)
    return Response(_json.dumps(doc, indent=1, default=str), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{safe}.wdnproj"'})


@app.post("/api/projects")
async def open_project(file: UploadFile = File(...)) -> dict[str, Any]:
    from wdn_engine.edits import load_project
    data = await file.read()
    if len(data) > 60 * 1024 * 1024:
        raise HTTPException(413, "Project file too large.")
    try:
        session, extra = load_project(data)
    except Exception as error:
        raise _bad(error)
    store.add(session)
    payload = _load_payload(session)
    payload["project"] = extra
    payload["runs"] = [_sim_payload(sim) for sim in session.sims.values()]
    payload["active_sim_id"] = session.active_sim_id
    return payload


@app.post("/api/s/{sid}/report")
def start_report(sid: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a Word/PDF report in the background; download from /report/{job_id} when done."""
    from wdn_engine import jobs
    from wdn_engine.report import build_report
    s = _session(sid)
    b = dict(body or {})
    fmt = "pdf" if b.get("format") == "pdf" else "docx"
    llm_cfg = b.get("llm") if b.get("discussion") else None
    if llm_cfg and llm_cfg.get("provider") == "custom" and not ALLOW_CUSTOM_URL:
        llm_cfg = None

    def run(progress):
        data, mime = build_report(s, fmt, b.get("style", "technical"), str(b.get("author", ""))[:80], llm_cfg,
                                  {"maps": bool(b.get("maps", True)), "resilience": True, "assessment": bool(b.get("assessment", True))}, progress)
        s.__dict__.setdefault("reports", {})[job_id_box[0]] = (data, mime, fmt)
        return {"ready": True, "format": fmt, "size_kb": round(len(data) / 1024)}

    job_id_box: list[str] = [""]
    job = jobs.submit(s, "report", run)
    job_id_box[0] = job.id
    return job.public()


@app.get("/api/s/{sid}/report/{job_id}")
def download_report(sid: str, job_id: str) -> Response:
    s = _session(sid)
    item = s.__dict__.get("reports", {}).get(job_id)
    if item is None:
        raise HTTPException(404, "Report not ready (or expired).")
    data, mime, fmt = item
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in s.model.name)
    return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{safe}_report.{fmt}"'})


# ----------------------------------------------------------------- criteria
@app.get("/api/criteria/presets")
def criteria_presets() -> dict[str, Any]:
    return PRESETS


@app.put("/api/s/{sid}/criteria")
def set_criteria(sid: str, body: dict[str, Any]) -> dict[str, Any]:
    s = _session(sid)
    try:
        s.set_criteria(DesignCriteria.from_dict(body))
    except (TypeError, ValueError) as error:
        raise _bad(error)
    md = format_result("run_simulation", {"success": True, **s.sim.summary}) if s.sim else None
    return {"criteria": s.criteria.to_dict(), "simulation": s.sim.summary if s.sim else None, "markdown": md}


@app.get("/api/s/{sid}/criteria")
def get_criteria(sid: str) -> dict[str, Any]:
    return _session(sid).criteria.to_dict()


# --------------------------------------------------------------- simulation
@app.post("/api/s/{sid}/simulate")
def simulate(sid: str, body: dict[str, Any]) -> dict[str, Any]:
    s = _session(sid)
    try:
        settings = SimSettings.from_dict(body)
        sim = s.simulate(settings)
    except (ValueError, RuntimeError) as error:
        raise _bad(error)
    return _sim_payload(sim)


def _sim_payload(sim) -> dict[str, Any]:
    return {**sim.summary, "times_h": [round(float(t), 4) for t in sim.times_h],
            "markdown": format_result("run_simulation", {"success": True, **sim.summary}),
            "node_vars": list(sim.node_data), "link_vars": list(sim.link_data)}


@app.get("/api/s/{sid}/sim/{sim_id}")
def sim_summary(sid: str, sim_id: str) -> dict[str, Any]:
    s = _session(sid)
    sim = s.sims.get(sim_id)
    if sim is None:
        raise HTTPException(404, "Simulation not found.")
    s.active_sim_id = sim_id
    return _sim_payload(sim)


@app.get("/api/s/{sid}/sim/{sim_id}/charts")
def sim_charts(sid: str, sim_id: str, other: str | None = None) -> dict[str, Any]:
    from wdn_engine.charts import chart_data
    s = _session(sid)
    sim = s.sims.get(sim_id)
    if sim is None:
        raise HTTPException(404, "Simulation not found.")
    o = s.sims.get(other) if other else None
    return chart_data(s.model, sim, o)


@app.get("/api/s/{sid}/sim/{sim_id}/frame")
def sim_frame(sid: str, sim_id: str, var: str, t: int = 0, stat: str | None = None) -> dict[str, Any]:
    s = _session(sid)
    sim = s.sims.get(sim_id)
    if sim is None:
        raise HTTPException(404, "Simulation not found (it may have been replaced by newer runs).")
    if var not in NODE_VARS and var not in LINK_VARS:
        raise HTTPException(400, f"Unknown variable '{var}'.")
    if stat not in (None, "", "none", "min", "max", "mean", "absmax"):
        raise HTTPException(400, "stat must be min, max, mean or absmax.")
    try:
        return sim.frame(var, t, None if stat in (None, "", "none") else stat)
    except KeyError as error:
        raise HTTPException(400, str(error))


@app.get("/api/s/{sid}/sim/{sim_id}/series/{kind}/{eid}")
def sim_series(sid: str, sim_id: str, kind: str, eid: str) -> dict[str, Any]:
    sim = _session(sid).sims.get(sim_id)
    if sim is None:
        raise HTTPException(404, "Simulation not found.")
    try:
        return sim.series("link" if kind == "link" else "node", eid)
    except KeyError as error:
        raise HTTPException(404, str(error))


# --------------------------------------------------------------------- chat
class ChatRequest(BaseModel):
    message: str = Field(..., max_length=4000)
    history: list[dict[str, Any]] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    context: dict[str, Any] | None = None
    fallbacks: list[dict[str, Any]] = Field(default_factory=list)


@app.get("/api/llm/providers")
def providers() -> dict[str, Any]:
    return {"providers": llm.provider_info(), "default": llm.default_provider(), "custom_url_allowed": ALLOW_CUSTOM_URL}


@app.post("/api/llm/models")
def models(body: dict[str, Any]) -> dict[str, Any]:
    base_url = body.get("base_url") if ALLOW_CUSTOM_URL else None
    try:
        return {"models": llm.list_models(body.get("provider", "groq"), body.get("api_key"), base_url)}
    except Exception as error:
        raise HTTPException(400, f"Could not list models: {error}")


@app.post("/api/llm/test")
def llm_test(body: dict[str, Any]) -> dict[str, Any]:
    base_url = body.get("base_url") if (ALLOW_CUSTOM_URL and body.get("provider") == "custom") else None
    return llm.test_connection(body.get("provider", ""), body.get("model"), body.get("api_key"), base_url)


@app.post("/api/s/{sid}/chat_async")
def chat_async(sid: str, req: ChatRequest) -> dict[str, Any]:
    """Same as /chat but runs as a job, so the UI can show each step live."""
    from wdn_engine import jobs
    from wdn_engine.tools import PROGRESS
    s = _session(sid)
    base_url = req.base_url if (ALLOW_CUSTOM_URL and req.provider == "custom") else None

    def run(progress):
        PROGRESS.set(progress)
        return llm.chat(s, req.message, req.history, req.provider, req.model, req.api_key, base_url, req.context, req.fallbacks[:3])

    job = jobs.submit(s, "chat", run, pool="chat")
    return job.public()


@app.get("/api/glossary")
def glossary() -> dict[str, Any]:
    from wdn_engine.glossary import GLOSSARY
    return GLOSSARY


@app.post("/api/s/{sid}/chat")
def chat(sid: str, req: ChatRequest) -> dict[str, Any]:
    s = _session(sid)
    base_url = req.base_url if (ALLOW_CUSTOM_URL and req.provider == "custom") else None
    return llm.chat(s, req.message, req.history, req.provider, req.model, req.api_key, base_url,
                    req.context, req.fallbacks[:3])


# ------------------------------------------------------------------- static
@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "wntr": wntr.__version__}


@app.exception_handler(Exception)
async def unhandled(_, exc: Exception):  # keep errors readable in the UI
    return JSONResponse(status_code=500, content={"detail": f"Internal error: {exc}"})


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
