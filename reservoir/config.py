from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
OUTPUTS = ROOT / "outputs"


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@dataclass
class Region:
    id: str
    name: str
    state: str
    utility: str
    towns: list[str]
    all_utility_towns: bool
    sources: dict            # state sources merged over national
    national: dict
    weights: dict
    raw: dict = field(default_factory=dict)

    @property
    def crs(self) -> int:
        return int(self.sources["crs"])

    @property
    def out_dir(self) -> Path:
        d = OUTPUTS / self.id
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def parcel_fields(self) -> dict[str, str | None]:
        """Standard parcel field name -> this state's source column (None if the source doesn't have it)."""
        p = self.sources["parcels"]
        of = p.get("owner_fields") or {}
        return {
            "parcel_id": p.get("id_field"), "site_address": p.get("site_address_field"),
            "use_code": p.get("use_code_field"), "use_desc": p.get("use_desc_field"), "map_par": p.get("map_par_field"),
            **{f"owner_{k}": of.get(k) for k in ("name", "care_of", "address", "city", "state", "zip")},
        }

    def hosting(self) -> dict | None:
        hc = self.sources.get("hosting_capacity", {})
        for name, cfg in hc.items():
            if name.lower() in self.utility.lower() or self.utility.lower() in name.lower():
                return cfg
        return None


def list_regions() -> list[str]:
    return sorted(p.stem for p in (CONFIG / "regions").glob("*.yaml"))


def load_region(region_id: str, weights: str | None = None) -> Region:
    raw = _load(CONFIG / "regions" / f"{region_id}.yaml")
    state = raw["state"].lower()
    w = _load(CONFIG / "weights" / f"{weights or raw.get('weights', 'default')}.yaml")
    return Region(
        id=raw["id"],
        name=raw["name"],
        state=raw["state"],
        utility=raw.get("utility", ""),
        towns=[t.upper() for t in raw.get("towns", [])],
        all_utility_towns=bool(raw.get("all_utility_towns")),
        sources=_load(CONFIG / "sources" / f"{state}.yaml"),
        national=_load(CONFIG / "sources" / "national.yaml"),
        weights=w,
        raw=raw,
    )
