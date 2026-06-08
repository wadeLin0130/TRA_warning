"""
Geospatial logic for mapping GPS (lat, lon) to Taiwan Railway line + official mileage (km).
Uses station cumulative distances + track shapes (polylines) from TDX.
Pure python + haversine for distances. No shapely required (but can swap in).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from haversine import haversine, Unit
except ImportError:
    # fallback inline haversine
    def haversine(point1, point2, unit= "km"):
        lat1, lon1 = point1
        lat2, lon2 = point2
        R = 6371.0  # km
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        dist = R * c
        return dist if unit == "km" else dist * 1000

    class Unit:
        KILOMETERS = "km"
        METERS = "m"

@dataclass
class LineMileage:
    line_id: str
    line_name: str
    mileage: float  # km
    confidence: float  # 0-1 rough, based on dist to track
    nearest_station_id: Optional[str] = None
    nearest_station_name: Optional[str] = None
    nearest_station_dist_m: Optional[float] = None
    projection_lat: Optional[float] = None
    projection_lon: Optional[float] = None


def _haversine_m(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    """Return distance in meters."""
    d = haversine((p1[0], p1[1]), (p2[0], p2[1]), unit=Unit.KILOMETERS)
    return d * 1000.0


def _project_point_to_segment(
    pt: Tuple[float, float],
    a: Tuple[float, float],
    b: Tuple[float, float],
) -> Tuple[Tuple[float, float], float, float]:
    """
    Project pt onto segment [a,b] (approx using local cartesian for short segments).
    Returns (proj_point, frac_along_segment 0-1, dist_m to proj).
    """
    lat1, lon1 = a
    lat2, lon2 = b
    latp, lonp = pt

    # Very rough local meter scale at ~23.5N Taiwan
    m_per_deg_lat = 110540.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(23.5))

    x1 = (lon1 - lon1) * m_per_deg_lon
    y1 = (lat1 - lat1) * m_per_deg_lat
    x2 = (lon2 - lon1) * m_per_deg_lon
    y2 = (lat2 - lat1) * m_per_deg_lat
    xp = (lonp - lon1) * m_per_deg_lon
    yp = (latp - lat1) * m_per_deg_lat

    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        projx, projy = x1, y1
        frac = 0.0
    else:
        t = ((xp - x1) * dx + (yp - y1) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))
        projx = x1 + t * dx
        projy = y1 + t * dy
        frac = t

    proj_lon = lon1 + projx / m_per_deg_lon
    proj_lat = lat1 + projy / m_per_deg_lat

    # dist from original pt (in relative xp,yp) to the proj point (projx,projy)
    dist_m = math.hypot(xp - projx, yp - projy)
    return (proj_lat, proj_lon), frac, dist_m


class TRAGeoIndex:
    """
    Holds preloaded shapes + station-of-line cumulative distances for fast query.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.lines: Dict[str, Dict[str, Any]] = {}  # LineID -> { "LineName": ... }
        self.stations: Dict[str, Dict[str, Any]] = {}  # StationID -> { "StationName", "PositionLat", "PositionLon", ... }
        self.station_of_lines: Dict[str, List[Dict]] = {}  # LineID -> list of station dicts sorted by Sequence, with CumulativeDistance
        self.shapes: Dict[str, List[Tuple[float, float]]] = {}  # LineID -> [(lon, lat), ...] in travel order

        # Precomputed per line: list of (cum_geo_m, station_cum_km, station_id) anchors + full shape cum
        self._line_anchors: Dict[str, List[Tuple[float, float, str]]] = {}
        self._line_shape_cum: Dict[str, List[float]] = {}  # prefix cum dist in meters for each shape point

    def load(self):
        # lines
        lines_path = self.data_dir / "lines.json"
        if lines_path.exists():
            for ln in json.loads(lines_path.read_text(encoding="utf-8")):
                lid = ln.get("LineID") or ln.get("lineID")
                if lid:
                    self.lines[lid] = ln

        # stations
        st_path = self.data_dir / "stations.json"
        if st_path.exists():
            for st in json.loads(st_path.read_text(encoding="utf-8")):
                sid = st.get("StationID") or st.get("stationID")
                if sid:
                    pos = st.get("StationPosition") or {}
                    self.stations[sid] = {
                        "StationID": sid,
                        "StationName": (st.get("StationName") or {}).get("Zh_tw") if isinstance(st.get("StationName"), dict) else st.get("StationName"),
                        "PositionLat": pos.get("PositionLat") or st.get("PositionLat"),
                        "PositionLon": pos.get("PositionLon") or st.get("PositionLon"),
                    }

        # station of line (has CumulativeDistance)
        sol_path = self.data_dir / "station_of_lines.json"
        if sol_path.exists():
            for sol in json.loads(sol_path.read_text(encoding="utf-8")):
                lid = sol.get("LineID")
                stas = sol.get("Stations", [])
                # ensure sorted
                stas = sorted(stas, key=lambda s: s.get("Sequence", 0))
                self.station_of_lines[lid] = stas
                # enrich with pos if missing
                for s in stas:
                    sid = s.get("StationID")
                    if sid in self.stations and "StationPosition" not in s:
                        s["StationPosition"] = {
                            "PositionLat": self.stations[sid]["PositionLat"],
                            "PositionLon": self.stations[sid]["PositionLon"],
                        }

        # --- Correct TDX CumulativeDistance to match official TRA K-post mileages (user-reported) ---
        # TDX StationOfLine.CumulativeDistance uses an internal datum that can be offset from the
        # physical kilometer posts (K markers) on the ground. We apply additive offsets per line
        # so that reported mileages match what users see on track / official references.
        # Example: 南科站 TDX reports ~337.1 but official K341+850 → offset +4.75 for western trunk.
        # If you discover offsets for other lines, add them here.
        LINE_MILEAGE_OFFSETS = {
            # Western Trunk Line and its detailed shape segments (most common main line)
            "WL": 4.75,
            "TL": 4.75,
            "TL-N": 4.75,
            "TL-S": 4.75,
            "TL-M": 4.75,
            "TL-C": 4.75,
            "WL-C": 4.75,
            "WL-M": 4.75,
            # Add others as users report (e.g. "EL": x.x for eastern, etc.)
        }
        for lid, stas in self.station_of_lines.items():
            off = LINE_MILEAGE_OFFSETS.get(lid, 0.0)
            if off:
                for s in stas:
                    if s.get("CumulativeDistance") is not None:
                        s["CumulativeDistance"] = float(s["CumulativeDistance"]) + off

        # shapes
        sh_path = self.data_dir / "shapes.json"
        if sh_path.exists():
            for sh in json.loads(sh_path.read_text(encoding="utf-8")):
                lid = sh.get("LineID")
                geom = sh.get("Geometry")
                coords = []
                if isinstance(geom, str):
                    gup = geom.upper()
                    if gup.startswith("LINESTRING") or gup.startswith("MULTILINESTRING"):
                        # TDX v3 returns WKT, sometimes MULTILINESTRING for complex lines
                        try:
                            # Extract all coordinate groups
                            import re
                            # Find all ( ... ) groups
                            groups = re.findall(r"\(([^)]+)\)", geom)
                            best_coords = []
                            for g in groups:
                                pairs = []
                                for p in g.split(","):
                                    p = p.strip()
                                    if not p: continue
                                    parts = p.split()
                                    if len(parts) >= 2:
                                        pairs.append([float(parts[0].lstrip("(")), float(parts[1].rstrip(")"))])
                                if len(pairs) > len(best_coords):
                                    best_coords = pairs
                            coords = best_coords if best_coords else []
                        except Exception:
                            coords = []
                elif isinstance(geom, dict):
                    if geom.get("Type") == "LineString" or "Coordinates" in geom:
                        coords = geom.get("Coordinates", [])
                    elif "coordinates" in geom:  # geojson style
                        coords = geom["coordinates"]
                if lid and coords:
                    # store as (lon, lat) tuples to match existing logic
                    self.shapes[lid] = [(float(c[0]), float(c[1])) for c in coords]

        # build anchors and cum for each line that has both shape + stations
        self._build_indices()

        # Mapping from detailed shape LineIDs (e.g. TL-N) to StationOfLine LineIDs that provide CumulativeDistance anchors
        # This is needed because TDX Shape returns fine-grained segments for main trunks, while StationOfLine uses coarser groups.
        self.shape_to_sol: Dict[str, str] = {
            "TL": "WL",
            "TL-N": "WL",
            "TL-S": "WL",
            "TL-M": "WL",
            "TL-C": "WL-C",
            "YL": "EL",
            "NL": "EL",
            "TT": "EL",
            "PL": "SL",  # Pingtung line often grouped under south link
            # add more if new shapes appear
        }
        # Nicer display names for detailed lines
        self.line_display_names: Dict[str, str] = {
            "TL-N": "縱貫線北段",
            "TL-S": "縱貫線南段",
            "TL-M": "縱貫線山線",
            "TL-C": "縱貫線海線",
            "TL": "縱貫線",
            "WL": "西部幹線",
            "WL-C": "西部幹線海線",
            "WL-M": "西部幹線山線",
            "EL": "東部幹線",
            "YL": "宜蘭線",
            "NL": "北迴線",
            "TT": "台東線",
            "SL": "南迴線",
        }

    def _build_indices(self):
        self._line_anchors = {}
        self._line_shape_cum = {}

        for lid, coords in self.shapes.items():
            if len(coords) < 2:
                continue
            # compute prefix cum dist (meters) along shape
            cum = [0.0]
            for i in range(1, len(coords)):
                d = _haversine_m((coords[i-1][1], coords[i-1][0]), (coords[i][1], coords[i][0]))  # (lat,lon) for haversine
                cum.append(cum[-1] + d)
            self._line_shape_cum[lid] = cum

            # anchors: for each station on this line, find closest point on shape, record (shape_cum_m, official_cum_km, sid)
            stas = self.station_of_lines.get(lid, [])
            anchors: List[Tuple[float, float, str]] = []
            for s in stas:
                sid = s.get("StationID")
                cum_km = s.get("CumulativeDistance")
                if cum_km is None:
                    continue
                pos = s.get("StationPosition") or {}
                slat = pos.get("PositionLat")
                slon = pos.get("PositionLon")
                if slat is None or slon is None:
                    continue
                # find closest on shape
                best_cum = None
                best_d = float("inf")
                for i, c in enumerate(coords):
                    d = _haversine_m((slat, slon), (c[1], c[0]))
                    if d < best_d:
                        best_d = d
                        best_cum = cum[i]
                if best_cum is not None:
                    anchors.append((best_cum, float(cum_km), sid))
            if anchors:
                # sort by shape cum
                anchors.sort(key=lambda x: x[0])
                self._line_anchors[lid] = anchors

    def find_closest(self, lat: float, lon: float, max_dist_m: float = 5000.0) -> Optional[LineMileage]:
        """
        Find the closest point on any TRA track shape, return line + interpolated official mileage.
        """
        best: Optional[LineMileage] = None
        best_d = float("inf")

        for lid, coords in self.shapes.items():
            if len(coords) < 2:
                continue
            cum_list = self._line_shape_cum.get(lid, [])
            if not cum_list:
                # compute on fly (slow path)
                cum_list = [0.0]
                for i in range(1, len(coords)):
                    d = _haversine_m((coords[i-1][1], coords[i-1][0]), (coords[i][1], coords[i][0]))
                    cum_list.append(cum_list[-1] + d)

            # find closest segment
            min_d_seg = float("inf")
            best_proj = None
            best_frac = 0.0
            best_seg_idx = 0
            for i in range(len(coords) - 1):
                proj, frac, d = _project_point_to_segment((lat, lon), (coords[i][1], coords[i][0]), (coords[i+1][1], coords[i+1][0]))
                if d < min_d_seg:
                    min_d_seg = d
                    best_proj = proj
                    best_frac = frac
                    best_seg_idx = i

            if min_d_seg > max_dist_m:
                continue

            # cum along shape at proj
            c0 = cum_list[best_seg_idx]
            c1 = cum_list[best_seg_idx + 1]
            shape_cum_m = c0 + best_frac * (c1 - c0)

            # interpolate official km using anchors if available
            # Use mapped sol lid for detailed shapes (TL-N etc map to WL etc)
            effective_lid = self.shape_to_sol.get(lid, lid)
            anchors = self._line_anchors.get(effective_lid, []) or self._line_anchors.get(lid, [])
            mileage_km: Optional[float] = None
            nearest_sid = None
            nearest_sname = None

            if anchors:
                # find surrounding anchors
                left = None
                right = None
                for a in anchors:
                    if a[0] <= shape_cum_m:
                        left = a
                    if a[0] >= shape_cum_m and right is None:
                        right = a
                        break
                if left and right and left[0] != right[0]:
                    ratio = (shape_cum_m - left[0]) / (right[0] - left[0])
                    mileage_km = left[1] + ratio * (right[1] - left[1])
                    nearest_sid = left[2] if ratio < 0.5 else right[2]
                elif left:
                    mileage_km = left[1]
                    nearest_sid = left[2]
                elif right:
                    mileage_km = right[1]
                    nearest_sid = right[2]
                else:
                    # extrapolate from first/last
                    if shape_cum_m < anchors[0][0]:
                        mileage_km = anchors[0][1]
                    else:
                        mileage_km = anchors[-1][1]
                    nearest_sid = anchors[0][2] if shape_cum_m < anchors[0][0] else anchors[-1][2]
            else:
                # fallback: use total shape length proportion if we know line total km from last station
                stas = self.station_of_lines.get(effective_lid, []) or self.station_of_lines.get(lid, [])
                if stas:
                    total_km = max((s.get("CumulativeDistance") or 0) for s in stas)
                    if cum_list[-1] > 0:
                        mileage_km = (shape_cum_m / cum_list[-1]) * total_km
                    nearest_sid = stas[0].get("StationID") if stas else None

            if mileage_km is None:
                continue

            line_name = (self.lines.get(lid) or {}).get("LineName", {}).get("Zh_tw") if self.lines.get(lid) else lid
            if isinstance(line_name, dict):
                line_name = line_name.get("Zh_tw", lid)
            # prefer our display name for sub-lines
            line_name = self.line_display_names.get(lid, line_name)

            # --- Better: recompute mileage using station positions + CumulativeDistance on the effective line ---
            # This avoids cum-origin mismatches between sub-shapes (TL-N) and coarse WL shapes.
            eff_lid_for_mile = self.shape_to_sol.get(lid, lid)
            eff_stas = self.station_of_lines.get(eff_lid_for_mile, [])
            if eff_stas and best_proj:
                p_lat, p_lon = best_proj
                # find best consecutive station pair on this line (by geo dist of proj to segment)
                best_pair_d = float("inf")
                best_a = None
                best_b = None
                for j in range(len(eff_stas) - 1):
                    sa = eff_stas[j]
                    sb = eff_stas[j+1]
                    pa = sa.get("StationPosition") or {}
                    pb = sb.get("StationPosition") or {}
                    alat = pa.get("PositionLat")
                    alon = pa.get("PositionLon")
                    blat = pb.get("PositionLat")
                    blon = pb.get("PositionLon")
                    if alat is None or blat is None: continue
                    _, _, dd = _project_point_to_segment((p_lat, p_lon), (alat, alon), (blat, blon))
                    if dd < best_pair_d:
                        best_pair_d = dd
                        best_a = sa
                        best_b = sb
                if best_a and best_b:
                    ca = float(best_a.get("CumulativeDistance") or 0)
                    cb = float(best_b.get("CumulativeDistance") or 0)
                    # frac from the station pair projection (we can re-project for exact frac)
                    pa = best_a.get("StationPosition") or {}
                    pb = best_b.get("StationPosition") or {}
                    alat, alon = pa.get("PositionLat"), pa.get("PositionLon")
                    blat, blon = pb.get("PositionLat"), pb.get("PositionLon")
                    if alat and blat:
                        _, frac2, _ = _project_point_to_segment((p_lat, p_lon), (alat, alon), (blat, blon))
                        mileage_km = ca + frac2 * (cb - ca)

            # nearest station name
            if nearest_sid and nearest_sid in self.stations:
                nearest_sname = self.stations[nearest_sid].get("StationName")

            # nearest station overall (for display)
            nsid = None
            nsname = None
            nsdist = None
            for sid, s in self.stations.items():
                slat = s.get("PositionLat")
                slon = s.get("PositionLon")
                if slat and slon:
                    d = _haversine_m((lat, lon), (slat, slon))
                    if nsdist is None or d < nsdist:
                        nsdist = d
                        nsid = sid
                        nsname = s.get("StationName")

            cand = LineMileage(
                line_id=lid,
                line_name=line_name or lid,
                mileage=round(mileage_km, 3),
                confidence=max(0.0, min(1.0, 1.0 - (min_d_seg / 3000.0))),  # rough
                nearest_station_id=nsid,
                nearest_station_name=nsname,
                nearest_station_dist_m=round(nsdist, 1) if nsdist else None,
                projection_lat=round(best_proj[0], 6) if best_proj else None,
                projection_lon=round(best_proj[1], 6) if best_proj else None,
            )
            if min_d_seg < best_d:
                best_d = min_d_seg
                best = cand

        return best

    def find_nearby_lines(self, lat: float, lon: float, max_dist_m: float = 1000.0, max_results: int = 5) -> list[dict]:
        """Return other lines close to the point (within max_dist_m), with their mileage on that line.
        Used to let user choose which line to monitor when near junctions or parallel tracks.
        """
        candidates = []
        for lid, coords in self.shapes.items():
            if len(coords) < 2:
                continue
            cum_list = self._line_shape_cum.get(lid, [])
            if not cum_list:
                # compute on fly
                cum_list = [0.0]
                for i in range(1, len(coords)):
                    d = _haversine_m((coords[i-1][1], coords[i-1][0]), (coords[i][1], coords[i][0]))
                    cum_list.append(cum_list[-1] + d)
            # find closest segment
            min_d_seg = float("inf")
            best_frac = 0.0
            best_seg_idx = 0
            best_proj = None
            for i in range(len(coords) - 1):
                proj, frac, d = _project_point_to_segment((lat, lon), (coords[i][1], coords[i][0]), (coords[i+1][1], coords[i+1][0]))
                if d < min_d_seg:
                    min_d_seg = d
                    best_frac = frac
                    best_seg_idx = i
                    best_proj = proj
            if min_d_seg > max_dist_m:
                continue
            # cum along shape
            c0 = cum_list[best_seg_idx]
            c1 = cum_list[best_seg_idx + 1]
            shape_cum_m = c0 + best_frac * (c1 - c0)
            # interpolate official km using anchors for this lid
            anchors = self._line_anchors.get(lid, [])
            mileage_km = None
            if anchors:
                left = right = None
                for a in anchors:
                    if a[0] <= shape_cum_m:
                        left = a
                    if a[0] >= shape_cum_m and right is None:
                        right = a
                        break
                if left and right and left[0] != right[0]:
                    ratio = (shape_cum_m - left[0]) / (right[0] - left[0])
                    mileage_km = left[1] + ratio * (right[1] - left[1])
                elif left:
                    mileage_km = left[1]
                elif right:
                    mileage_km = right[1]
            if mileage_km is None:
                continue
            line_name = self.line_display_names.get(lid, lid)
            candidates.append({
                "line_id": lid,
                "line_name": line_name,
                "mileage_km": round(mileage_km, 3),
                "dist_m": round(min_d_seg, 1),
            })
        # dedup by effective (e.g. TL*/WL often same trunk line, JJ branch)
        eff_to_best = {}
        for c in candidates:
            eff = self.shape_to_sol.get(c["line_id"], c["line_id"])
            c["effective_line_id"] = eff
            if eff not in eff_to_best or c.get("dist_m", 9999) < eff_to_best[eff].get("dist_m", 9999):
                c["line_name"] = self.line_display_names.get(eff, c["line_name"])
                eff_to_best[eff] = c
        candidates = list(eff_to_best.values())
        candidates.sort(key=lambda x: x["dist_m"])
        return candidates[:max_results]

    def get_danger_zone_stations(
        self,
        line_id: str,
        user_mileage: float,
        num_before: int = 2,
        num_after: int = 2,
    ) -> tuple[list[str], list[str], list[dict]]:
        """
        Return station IDs, names, and details (with lat/lon) for the danger zone.
        Danger zone = 2 stations before + 2 stations after user position.
        If user is within 1 km of the immediately adjacent station on either side,
        that station is excluded (the worker stands too close to it to count it).
        """
        SKIP_THRESHOLD_KM = 1.0
        effective_lid = self.shape_to_sol.get(line_id, line_id)
        stas = self.station_of_lines.get(effective_lid, [])
        if not stas:
            return [], [], []

        # Sort by cumulative distance so index order matches mileage order
        sorted_stas = sorted(stas, key=lambda s: float(s.get("CumulativeDistance") or 0))

        # Find bracket: left_idx is the last station with mileage <= user, right_idx is the first > user
        left_idx, right_idx = -1, len(sorted_stas)
        for i, s in enumerate(sorted_stas):
            cm = float(s.get("CumulativeDistance") or 0)
            if cm <= user_mileage:
                left_idx = i
            else:
                right_idx = i
                break

        # Decide how many stations to take on each side, skipping the immediate neighbor if < 1 km
        def _cm(idx):
            if 0 <= idx < len(sorted_stas):
                return float(sorted_stas[idx].get("CumulativeDistance") or 0)
            return None

        left_skip = (left_idx >= 0 and abs((_cm(left_idx) or 0) - user_mileage) < SKIP_THRESHOLD_KM)
        right_skip = (right_idx < len(sorted_stas) and abs((_cm(right_idx) or 0) - user_mileage) < SKIP_THRESHOLD_KM)

        # Build index list: 2 stations to the left, 2 to the right (skipping immediate neighbor if too close)
        indices = set()
        left_start = left_idx - 1 if left_skip else left_idx
        for i in range(left_start, left_start - num_before, -1):
            if 0 <= i < len(sorted_stas):
                indices.add(i)

        right_start = right_idx + 1 if right_skip else right_idx
        for i in range(right_start, right_start + num_after):
            if 0 <= i < len(sorted_stas):
                indices.add(i)

        zone_stas = [sorted_stas[i] for i in sorted(indices)]

        ids = []
        names = []
        details = []
        for s in zone_stas:
            sid = s.get("StationID")
            if sid:
                ids.append(sid)
                name = s.get("StationName") or {}
                if isinstance(name, dict):
                    name = name.get("Zh_tw", sid)
                names.append(name)
                # position for map
                pos = s.get("StationPosition") or {}
                if (not pos or not pos.get("PositionLat")) and sid in self.stations:
                    pos = self.stations[sid] or {}
                lat = pos.get("PositionLat") if isinstance(pos, dict) else None
                lon = pos.get("PositionLon") if isinstance(pos, dict) else None
                details.append({
                    "id": sid,
                    "name": name,
                    "lat": lat,
                    "lon": lon,
                })

        return ids, names, details

    def get_line_direction_ends(self, line_id: str) -> Optional[dict]:
        """
        Return info for the two travel directions on this line, based on the canonical
        ordered StationOfLine list (by Sequence). This replaces global odd/even parity
        because parity convention differs by line (e.g. reversed on 花東線/EL, and
        N/S doesn't apply to 集集線/JJ, 南迴線/SL, branches etc).
        Use the line's own station order + each train's timetable StopTimes order
        to determine which way the train is heading.
        """
        effective = self.shape_to_sol.get(line_id, line_id)
        stas = self.station_of_lines.get(effective, [])
        if not stas:
            return None
        # ensure ordered by Sequence (load already does, but defensive)
        ordered = sorted(stas, key=lambda s: s.get("Sequence", 0))
        if not ordered:
            return None

        def get_zh(n):
            if isinstance(n, dict):
                return n.get("Zh_tw") or n.get("En") or ""
            return n or ""

        low = ordered[0]
        high = ordered[-1]
        low_name = get_zh(low.get("StationName"))
        high_name = get_zh(high.get("StationName"))

        seq_by_sid = {}
        index_by_sid = {}
        cum_by_sid = {}
        for i, s in enumerate(ordered):
            sid = s.get("StationID")
            if sid:
                seq_by_sid[sid] = s.get("Sequence", i)
                index_by_sid[sid] = i
                cd = s.get("CumulativeDistance")
                if cd is not None:
                    cum_by_sid[sid] = float(cd)

        return {
            "effective_line_id": effective,
            "low_end_id": low.get("StationID"),
            "low_end_name": low_name,
            "high_end_id": high.get("StationID"),
            "high_end_name": high_name,
            "ordered_stations": ordered,
            "seq_by_sid": seq_by_sid,
            "index_by_sid": index_by_sid,
            "cum_by_sid": cum_by_sid,
        }


# Global index
_geo_index: Optional[TRAGeoIndex] = None

def get_geo_index(data_dir: Optional[Path] = None) -> TRAGeoIndex:
    global _geo_index
    if _geo_index is None:
        d = data_dir or Path(__file__).parent.parent / "data"
        _geo_index = TRAGeoIndex(d)
        _geo_index.load()
    return _geo_index
