"""Network model wrapper: loading, unit-consistent element tables and graphs.

WNTR stores everything in SI (m, m³/s).  This layer exposes the units engineers
usually read: lengths in m, diameters in mm, flows/demands in L/s, pressure in m.
"""

from __future__ import annotations

import math
import os
from functools import cached_property
from typing import Any

import networkx as nx
import wntr

LPS = 1000.0  # m³/s -> L/s


def fnum(value: Any, digits: int | None = None) -> float | None:
    """Finite float or None (JSON safe)."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return round(x, digits) if digits is not None else x


def _status(link) -> str:
    status = getattr(link, "initial_status", None)
    name = getattr(status, "name", None) or str(status or "")
    return name.capitalize() if name else "Open"


class NetworkModel:
    """A loaded EPANET network plus cached, unit-consistent views of it."""

    MAX_FILE_MB = 25

    def __init__(self, wn: wntr.network.WaterNetworkModel, name: str = "network"):
        self.wn = wn
        self.name = name
        self._cache: dict[Any, Any] = {}
        self._node_lookup: dict[str, str] = {}
        self.case_collisions: list[tuple[str, str]] = []
        for nid in wn.node_name_list:
            key = str(nid).casefold()
            if key in self._node_lookup:
                self.case_collisions.append((self._node_lookup[key], nid))
            else:
                self._node_lookup[key] = nid
        self._link_lookup: dict[str, str] = {}
        for lid in wn.link_name_list:
            key = str(lid).casefold()
            if key in self._link_lookup:
                self.case_collisions.append((self._link_lookup[key], lid))
            else:
                self._link_lookup[key] = lid

    # ------------------------------------------------------------------ loading
    @classmethod
    def from_inp(cls, path: str, name: str | None = None) -> "NetworkModel":
        if not path or not os.path.isfile(path):
            raise FileNotFoundError("Select a valid EPANET .inp file.")
        if os.path.getsize(path) > cls.MAX_FILE_MB * 1024 * 1024:
            raise ValueError(f"File is larger than {cls.MAX_FILE_MB} MB.")
        try:
            wn = wntr.network.WaterNetworkModel(path)
        except Exception as error:  # WNTR raises many types for malformed files
            raise ValueError(f"Could not parse the .inp file: {error}") from error
        return cls(wn, name or os.path.splitext(os.path.basename(path))[0])

    # ---------------------------------------------------------------- lookups
    def resolve_node(self, requested: Any) -> str | None:
        return self._node_lookup.get(str(requested).strip().strip("`'\"").casefold())

    def resolve_link(self, requested: Any) -> str | None:
        return self._link_lookup.get(str(requested).strip().strip("`'\"").casefold())

    @cached_property
    def sources(self) -> list[str]:
        return list(self.wn.reservoir_name_list) + list(self.wn.tank_name_list)

    def total_base_demand(self, node_id: str) -> float:
        """Sum of all demand categories in m³/s.

        WNTR's ``junction.base_demand`` returns only the *first* category, which
        under-reports multi-category networks such as L-TOWN.
        """
        node = self.wn.get_node(node_id)
        series = getattr(node, "demand_timeseries_list", None)
        if not series:
            return 0.0
        return float(sum(d.base_value for d in series))

    # ----------------------------------------------------------------- layout
    @cached_property
    def layout(self) -> dict[str, tuple[float, float]]:
        """Node coordinates; generated when the file has none."""
        coords = {}
        for nid in self.wn.node_name_list:
            c = getattr(self.wn.get_node(nid), "coordinates", None)
            if c is not None and len(c) == 2 and all(fnum(v) is not None for v in c):
                coords[nid] = (float(c[0]), float(c[1]))
        distinct = {v for v in coords.values()}
        if len(coords) == len(self.wn.node_name_list) and len(distinct) > max(1, 0.5 * len(coords)):
            self.layout_generated = False
            return coords
        self.layout_generated = True
        graph = self.graphs()[1]
        pos = nx.kamada_kawai_layout(graph) if graph.number_of_nodes() <= 600 else nx.spring_layout(graph, seed=7, iterations=80)
        return {n: (float(p[0]) * 1000.0, float(p[1]) * 1000.0) for n, p in pos.items()}

    layout_generated = False

    # ----------------------------------------------------------------- tables
    def node_record(self, nid: str) -> dict[str, Any]:
        node = self.wn.get_node(nid)
        x, y = self.layout.get(nid, (0.0, 0.0))
        rec = {
            "id": nid,
            "type": node.node_type,
            "x": x,
            "y": y,
            "elevation_m": fnum(getattr(node, "elevation", None), 3),
        }
        if node.node_type == "Junction":
            rec["base_demand_lps"] = fnum(self.total_base_demand(nid) * LPS, 4)
            rec["demand_categories"] = len(getattr(node, "demand_timeseries_list", []) or [])
        if node.node_type == "Reservoir":
            rec["base_head_m"] = fnum(getattr(node, "base_head", None), 3)
        if node.node_type == "Tank":
            for attr in ("init_level", "min_level", "max_level", "diameter"):
                rec[f"{attr}_m"] = fnum(getattr(node, attr, None), 3)
        return rec

    def link_record(self, lid: str) -> dict[str, Any]:
        link = self.wn.get_link(lid)
        rec = {
            "id": lid,
            "type": link.link_type,
            "from": link.start_node_name,
            "to": link.end_node_name,
            "status": _status(link),
        }
        if link.link_type == "Pipe":
            rec.update({
                "length_m": fnum(link.length, 3),
                "diameter_mm": fnum(link.diameter * 1000.0, 2),
                "roughness": fnum(link.roughness, 4),
                "minor_loss": fnum(link.minor_loss, 4),
                "check_valve": bool(getattr(link, "check_valve", False)),
            })
        elif link.link_type == "Valve":
            rec.update({
                "valve_type": str(getattr(link, "valve_type", "")),
                "diameter_mm": fnum(link.diameter * 1000.0, 2),
                "setting": fnum(getattr(link, "initial_setting", None), 4),
            })
        elif link.link_type == "Pump":
            rec.update({
                "pump_type": str(getattr(link, "pump_type", "")),
                "speed": fnum(getattr(link, "base_speed", None), 4),
            })
        verts = getattr(link, "vertices", None) or []
        if verts and not self.layout_generated:
            rec["vertices"] = [[float(a), float(b)] for a, b in verts]
        return rec

    def nodes(self) -> list[dict[str, Any]]:
        if "nodes" not in self._cache:
            self._cache["nodes"] = [self.node_record(n) for n in self.wn.node_name_list]
        return self._cache["nodes"]

    def links(self) -> list[dict[str, Any]]:
        if "links" not in self._cache:
            self._cache["links"] = [self.link_record(l) for l in self.wn.link_name_list]
        return self._cache["links"]

    def pipes(self) -> list[dict[str, Any]]:
        return [l for l in self.links() if l["type"] == "Pipe"]

    # ----------------------------------------------------------------- graphs
    def graphs(self, include_closed: bool = True) -> tuple[nx.MultiGraph, nx.Graph]:
        """Undirected physical topology (MultiGraph) and a simple projection.

        ``include_closed=False`` drops links whose *initial* status is Closed,
        giving the operational topology.  Parallel links are recorded on the
        simple edge so a duplicated pipe is never reported as a bridge.
        Weights: ``length_m`` (distance) and ``strength`` (1/length) for metrics
        that interpret weights as connection strength.
        """
        key = ("graphs", include_closed)
        if key in self._cache:
            return self._cache[key]
        multi = nx.MultiGraph()
        for nid in self.wn.node_name_list:
            multi.add_node(nid, node_type=self.wn.get_node(nid).node_type)
        for lid in self.wn.link_name_list:
            link = self.wn.get_link(lid)
            if not include_closed and _status(link) == "Closed":
                continue
            length = fnum(getattr(link, "length", None)) if link.link_type == "Pipe" else None
            length = length if length and length > 0 else 1.0  # pumps/valves: nominal 1 m
            multi.add_edge(link.start_node_name, link.end_node_name, key=lid, link_id=lid,
                           link_type=link.link_type, length_m=length, strength=1.0 / length)
        simple = nx.Graph()
        simple.add_nodes_from(multi.nodes(data=True))
        for u, v, data in multi.edges(data=True):
            if u == v:
                continue  # self-loops carry no topological information
            if not simple.has_edge(u, v):
                simple.add_edge(u, v, **data, physical_link_count=1, parallel_link_ids=[data["link_id"]])
                continue
            edge = simple[u][v]
            edge["physical_link_count"] += 1
            edge["parallel_link_ids"].append(data["link_id"])
            edge["strength"] += data["strength"]
            if data["length_m"] < edge["length_m"]:
                edge.update(link_id=data["link_id"], link_type=data["link_type"], length_m=data["length_m"])
        self._cache[key] = (multi, simple)
        return multi, simple

    # ---------------------------------------------------------------- summary
    def summary(self) -> dict[str, Any]:
        wn = self.wn
        hyd = wn.options.hydraulic
        total_demand = sum(self.total_base_demand(j) for j in wn.junction_name_list)
        pipes = self.pipes()
        return {
            "name": self.name,
            "nodes": len(wn.node_name_list),
            "junctions": len(wn.junction_name_list),
            "reservoirs": len(wn.reservoir_name_list),
            "tanks": len(wn.tank_name_list),
            "links": len(wn.link_name_list),
            "pipes": len(wn.pipe_name_list),
            "pumps": len(wn.pump_name_list),
            "valves": len(wn.valve_name_list),
            "total_pipe_length_km": round(sum(p["length_m"] or 0 for p in pipes) / 1000.0, 3),
            "total_base_demand_lps": round(total_demand * LPS, 3),
            "inp_flow_units": str(hyd.inpfile_units),
            "headloss_formula": str(hyd.headloss),
            "roughness_meaning": "Hazen-Williams C (unitless)" if str(hyd.headloss) == "H-W" else (
                "Darcy-Weisbach ε (mm)" if str(hyd.headloss) == "D-W" else "Chezy-Manning n"),
            "duration_h": round(wn.options.time.duration / 3600.0, 3),
            "hydraulic_timestep_min": round(wn.options.time.hydraulic_timestep / 60.0, 3),
            "coordinates": "generated layout" if (self.layout and self.layout_generated) else "from file",
        }
