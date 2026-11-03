"""Per-user working state: a loaded network, criteria and simulation runs."""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict

from .criteria import DesignCriteria
from .hydraulics import SimSettings, SimulationResult, run_simulation
from .model import NetworkModel

MAX_SIMS_PER_SESSION = 6


class Session:
    def __init__(self, model: NetworkModel):
        self.id = uuid.uuid4().hex
        self.model = model
        self.criteria = DesignCriteria()
        self.sims: "OrderedDict[str, SimulationResult]" = OrderedDict()
        self.active_sim_id: str | None = None
        self.layers: dict[str, dict] = {}       # analysis results that can colour the map
        self.segment_rows: dict[int, dict] = {}
        self.results: dict[str, dict] = {}     # latest result of each analysis (for reports)
        self.touched = time.time()

    @property
    def sim(self) -> SimulationResult | None:
        return self.sims.get(self.active_sim_id) if self.active_sim_id else None

    def simulate(self, settings: SimSettings) -> SimulationResult:
        result = run_simulation(self.model, settings, self.criteria)
        self.sims[result.id] = result
        while len(self.sims) > MAX_SIMS_PER_SESSION:
            self.sims.popitem(last=False)
        self.active_sim_id = result.id
        return result

    def set_criteria(self, criteria: DesignCriteria) -> None:
        self.criteria = criteria
        for sim in self.sims.values():  # keep KPIs consistent with the new limits
            if criteria.is_set():
                sim.summary["criteria"] = sim.evaluate_criteria(self.model, criteria)
            else:
                sim.summary.pop("criteria", None)


class SessionStore:
    def __init__(self, ttl_s: int = 3 * 3600, max_sessions: int = 60):
        self.ttl_s, self.max_sessions = ttl_s, max_sessions
        self._items: "OrderedDict[str, Session]" = OrderedDict()

    def add(self, session: Session) -> Session:
        self.cleanup()
        self._items[session.id] = session
        while len(self._items) > self.max_sessions:
            self._items.popitem(last=False)
        return session

    def get(self, sid: str) -> Session | None:
        s = self._items.get(sid)
        if s:
            s.touched = time.time()
            self._items.move_to_end(sid)
        return s

    def cleanup(self) -> None:
        now = time.time()
        for sid in [k for k, s in self._items.items() if now - s.touched > self.ttl_s]:
            self._items.pop(sid, None)
