"""Editing the loaded network (with an undo log) and saving/opening project files."""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
from typing import Any

import wntr

from .criteria import DesignCriteria
from .hydraulics import SimSettings
from .model import NetworkModel
from .session import Session

EDITABLE = {
    "Junction": {"elevation_m": "m", "base_demand_lps": "L/s"},
    "Reservoir": {"base_head_m": "m"},
    "Tank": {"init_level_m": "m", "min_level_m": "m", "max_level_m": "m", "elevation_m": "m", "diameter_m": "m"},
    "Pipe": {"diameter_mm": "mm", "length_m": "m", "roughness": "", "minor_loss": "", "status": "Open/Closed"},
    "Valve": {"setting": "valve units (m for PRV/PSV)", "status": "Open/Closed/Active", "diameter_mm": "mm"},
    "Pump": {"status": "Open/Closed", "speed": "relative speed"},
}
PROJECT_FORMAT = "wdn-studio-project"


def _get(session: Session, kind: str, eid: str):
    m = session.model
    if kind == "node":
        nid = m.resolve_node(eid)
        return (nid, m.wn.get_node(nid)) if nid else (None, None)
    lid = m.resolve_link(eid)
    return (lid, m.wn.get_link(lid)) if lid else (None, None)


def _read(obj, field: str, session: Session, eid: str):
    if field == "base_demand_lps":
        return round(session.model.total_base_demand(eid) * 1000, 6)
    if field == "diameter_mm":
        return round(obj.diameter * 1000, 4)
    if field == "status":
        st = getattr(obj, "initial_status", None)
        return getattr(st, "name", str(st)).capitalize()
    if field == "setting":
        return getattr(obj, "initial_setting", None)
    if field == "speed":
        return getattr(obj, "base_speed", None)
    attr = {"elevation_m": "elevation", "base_head_m": "base_head", "init_level_m": "init_level", "min_level_m": "min_level",
            "max_level_m": "max_level", "diameter_m": "diameter", "length_m": "length"}.get(field, field)
    return getattr(obj, attr, None)


def _write(obj, field: str, value: Any, session: Session, eid: str) -> None:
    if field == "status":
        v = str(value).strip().capitalize()
        if v not in ("Open", "Closed", "Active"):
            raise ValueError("status must be Open, Closed or Active.")
        if v == "Active" and obj.link_type != "Valve":
            raise ValueError("Only valves can be Active.")
        obj.initial_status = getattr(wntr.network.LinkStatus, v)
        return
    x = float(value)
    if field == "base_demand_lps":
        if x < 0:
            raise ValueError("Base demand must be ≥ 0 (use a reservoir for inflows).")
        series = obj.demand_timeseries_list
        total = sum(d.base_value for d in series)
        target = x / 1000.0
        if not series:
            obj.add_demand(target, None)
        elif total > 0:
            for d in series:          # keep the mix of demand categories
                d.base_value = d.base_value * target / total
        else:
            series[0].base_value = target
        return
    if field in ("diameter_mm", "length_m", "diameter_m") and x <= 0:
        raise ValueError(f"{field} must be positive.")
    if field == "roughness" and x <= 0:
        raise ValueError("Roughness must be positive.")
    if field == "diameter_mm":
        obj.diameter = x / 1000.0
    elif field == "setting":
        obj.initial_setting = x
    elif field == "speed":
        obj.base_speed = x
    else:
        attr = {"elevation_m": "elevation", "base_head_m": "base_head", "init_level_m": "init_level", "min_level_m": "min_level",
                "max_level_m": "max_level", "diameter_m": "diameter", "length_m": "length"}.get(field, field)
        setattr(obj, attr, x)
    if obj.__class__.__name__ == "Tank" and not (obj.min_level <= obj.init_level <= obj.max_level):
        raise ValueError("Tank levels must satisfy min ≤ initial ≤ max.")


def _invalidate(session: Session) -> None:
    session.model._cache.clear()
    session.edit_version = getattr(session, "edit_version", 0) + 1
    session.layers.clear()
    if hasattr(session, "answer_cache"):
        session.answer_cache.clear()


def editable_fields(session: Session, kind: str, eid: str) -> dict[str, Any]:
    kind = "link" if kind in ("link", "pipe", "pump", "valve") else "node"
    rid, obj = _get(session, kind, eid)
    if obj is None:
        return {"success": False, "error": f"Unknown {kind} '{eid}'."}
    typ = obj.node_type if kind == "node" else obj.link_type
    fields = [{"field": f, "unit": u, "value": _read(obj, f, session, rid)} for f, u in EDITABLE.get(typ, {}).items()]
    return {"success": True, "kind": kind, "id": rid, "type": typ, "fields": fields}


def apply_edit(session: Session, kind: str, eid: str, changes: dict[str, Any]) -> dict[str, Any]:
    kind = "link" if kind in ("link", "pipe", "pump", "valve") else "node"
    rid, obj = _get(session, kind, eid)
    if obj is None:
        return {"success": False, "error": f"Unknown {kind} '{eid}'."}
    typ = obj.node_type if kind == "node" else obj.link_type
    allowed = EDITABLE.get(typ, {})
    bad = [f for f in changes if f not in allowed]
    if bad:
        return {"success": False, "error": f"Cannot edit {', '.join(bad)} on a {typ}. Editable: {', '.join(allowed)}."}
    log = session.__dict__.setdefault("edits", [])
    done = []
    try:
        for field, value in changes.items():
            old = _read(obj, field, session, rid)
            _write(obj, field, value, session, rid)
            new = _read(obj, field, session, rid)
            entry = {"kind": kind, "id": rid, "type": typ, "field": field, "old": old, "new": new, "at": time.strftime("%H:%M:%S"), "at_epoch": time.time()}
            log.append(entry)
            done.append(entry)
    except (ValueError, TypeError) as error:
        for e in reversed(done):     # all-or-nothing
            _write(obj, e["field"], e["old"], session, rid)
            log.pop()
        return {"success": False, "error": str(error)}
    _invalidate(session)
    return {"success": True, "edits": done, "total_edits": len(log),
            "note": "Existing runs were made before this edit; run a new simulation to see its effect."}


def undo_edit(session: Session) -> dict[str, Any]:
    log = session.__dict__.setdefault("edits", [])
    if not log:
        return {"success": False, "error": "Nothing to undo."}
    e = log.pop()
    _, obj = _get(session, e["kind"], e["id"])
    _write(obj, e["field"], e["old"], session, e["id"])
    _invalidate(session)
    return {"success": True, "undone": e, "total_edits": len(log)}


def inp_text(model: NetworkModel) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "net.inp")
        wntr.network.write_inpfile(model.wn, path)
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()


def project_json(session: Session, chat: list[dict[str, Any]] | None = None, view: dict[str, Any] | None = None) -> dict[str, Any]:
    from dataclasses import asdict
    runs = []
    for sim in session.sims.values():
        st = asdict(sim.settings)
        runs.append({"sim_id": sim.id, "settings": st, "active": sim.id == session.active_sim_id})
    return {
        "format": PROJECT_FORMAT, "version": 1, "name": session.model.name, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "inp": inp_text(session.model), "criteria": session.criteria.to_dict(),
        "edits": session.__dict__.get("edits", []), "runs": runs[-8:], "chat": (chat or [])[-200:], "view": view or {},
    }


def load_project(data: bytes | str, rerun: bool = True) -> tuple[Session, dict[str, Any]]:
    doc = json.loads(data)
    if doc.get("format") != PROJECT_FORMAT:
        raise ValueError("Not a WDN Studio project file.")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "project.inp")
        with open(path, "w", encoding="utf-8") as f:
            f.write(doc["inp"])
        model = NetworkModel.from_inp(path, doc.get("name") or "project")
        model.layout  # noqa: B018 — resolve coordinates while the file exists
    session = Session(model)
    session.criteria = DesignCriteria.from_dict(doc.get("criteria"))
    session.edits = list(doc.get("edits") or [])
    id_map, failed = {}, []
    active = None
    if rerun:
        for r in doc.get("runs", [])[-6:]:
            try:
                sim = session.simulate(SimSettings.from_dict(r["settings"]))
                id_map[r["sim_id"]] = sim.id
                if r.get("active"):
                    active = sim.id
            except Exception as error:  # keep loading even if one scenario fails
                failed.append(f"{r.get('settings', {}).get('label') or r['sim_id']}: {error}")
        if id_map:
            session.active_sim_id = active or list(id_map.values())[-1]
    return session, {"chat": doc.get("chat", []), "view": doc.get("view", {}), "run_ids": id_map, "failed_runs": failed,
                     "saved_at": doc.get("saved_at"), "edits": session.edits}
