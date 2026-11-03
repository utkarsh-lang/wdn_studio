"""User-defined service/design criteria.

Standards differ by country, utility and network, so nothing is assumed: a check
is only applied when its limit is set.  Presets are *starting points* to edit.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any


@dataclass
class DesignCriteria:
    min_pressure_m: float | None = None
    max_pressure_m: float | None = None
    min_velocity_ms: float | None = None
    max_velocity_ms: float | None = None
    max_headloss_m_per_km: float | None = None
    label: str = "Custom"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DesignCriteria":
        data = data or {}
        kwargs = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            if f.name == "label":
                kwargs[f.name] = str(value or "Custom")[:60]
            elif value in (None, ""):
                kwargs[f.name] = None
            else:
                kwargs[f.name] = float(value)
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_set(self) -> bool:
        return any(v is not None for k, v in asdict(self).items() if k != "label")


PRESETS: dict[str, dict[str, Any]] = {
    "none": {"label": "Not set"},
    "illustrative": {
        "label": "Illustrative (edit me)",
        "min_pressure_m": 10.0, "max_pressure_m": 60.0,
        "min_velocity_ms": 0.1, "max_velocity_ms": 2.0, "max_headloss_m_per_km": 10.0,
    },
    "india_cpheeo_3storey": {
        "label": "CPHEEO-style residual pressure, 3-storey (17 m) — verify",
        "min_pressure_m": 17.0,
    },
}
