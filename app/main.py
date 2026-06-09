"""
FastAPI backend + simple frontend for TRA position + upcoming trains.
- POST /api/position  -> line + mileage from lat/lon
- GET  /api/upcoming  -> trains that will pass the position soon (based on live + line/mile)
- GET /                 -> serves the UI
"""
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from dotenv import load_dotenv
from datetime import datetime
import asyncio
import math
import time

# Rate limiting to protect TDX quota when deployed publicly
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from .tdx_client import get_client
from .geo import get_geo_index, LineMileage

load_dotenv()

app = FastAPI(title="TRA Position & Train Spotter", version="0.1.0")

# Rate limiter: conservative limits for public safety tool (protects your TDX quota)
limiter = Limiter(key_func=get_remote_address, default_limits=["30/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

TEMPLATES_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

# In-memory cache for live board.
# TTL 20 s: auto-refresh interval is 10 s, so every user hits the cache on normal cadence.
# _live_board_lock prevents a "cache stampede" where multiple concurrent requests all see
# an expired cache and each fire a TDX call simultaneously (burst → 429).
_live_board_cache = {"data": None, "ts": 0, "ttl": 20}
_live_board_lock = asyncio.Lock()

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_geo = get_geo_index(DATA_DIR)
_tdx = get_client()


class PositionRequest(BaseModel):
    lat: float
    lon: float


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/health")
async def health():
    """Health check for cloud platforms / load balancers."""
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.get("/api/spots")
async def get_spots():
    """Return list of pre-defined monitored spots for RTDB mode."""
    import json
    spots_path = BASE_DIR / "spots.json"
    if spots_path.exists():
        with open(spots_path, encoding="utf-8") as f:
            return json.load(f)
    return []


@app.post("/api/position")
@limiter.limit("10/minute")
async def get_position(request: Request, req: PositionRequest):
    try:
        res: Optional[LineMileage] = _geo.find_closest(req.lat, req.lon)
        if not res:
            return JSONResponse({"error": "No nearby TRA track found (try closer to rail)"}, status_code=404)
        km = res.mileage
        k_int = int(km)
        k_frac = int(round((km - k_int) * 1000))
        # Find other lines close by (for user to choose when near junctions/parallel tracks)
        nearby = _geo.find_nearby_lines(req.lat, req.lon, max_dist_m=1000, max_results=5)
        primary_eff = _geo.shape_to_sol.get(res.line_id, res.line_id)
        nearby_lines = [n for n in nearby if n.get("effective_line_id", n["line_id"]) != primary_eff]
        return {
            "line_id": res.line_id,
            "line_name": res.line_name,
            "mileage_km": round(km, 3),
            "mileage_k": f"K{k_int}+{k_frac:03d}",
            "confidence": round(res.confidence, 2),
            "projection": {
                "lat": res.projection_lat,
                "lon": res.projection_lon,
            },
            "nearest_station": {
                "id": res.nearest_station_id,
                "name": res.nearest_station_name,
                "dist_m": res.nearest_station_dist_m,
            },
            "nearby_lines": nearby_lines,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/upcoming")
@limiter.limit("60/minute")
async def get_upcoming(request: Request, 
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    line_id: Optional[str] = None,
    mileage: Optional[float] = None,
    limit: int = 8,
    danger_buffer_km: Optional[float] = 2.0,
):
    """
    Given either (lat,lon) or (line_id + mileage), return list of approaching trains.
    Uses TrainLiveBoard + simple estimation.
    """
    if lat is not None and lon is not None:
        pos = _geo.find_closest(lat, lon)
        if not pos:
            raise HTTPException(404, "No track near location")
        line_id = pos.line_id
        mileage = pos.mileage
    if not line_id or mileage is None:
        raise HTTPException(400, "Provide lat+lon or line_id+mileage")

    effective_line = _geo.shape_to_sol.get(line_id, line_id)

    buffer_km = danger_buffer_km if danger_buffer_km is not None else 2.0
    # Danger zone based on FULL station list for the line (more reliable than LiveBoard reported stations)
    # Now supports configurable buffer_km (distance) for map display and custom safety radius.
    danger_ids, danger_names, danger_stations = _geo.get_danger_zone_stations(
        effective_line, mileage, num_before=2, num_after=2, buffer_km=buffer_km
    )

    # Fetch live trains (all or filtered later)
    # Use short TTL cache to avoid 429 rate limits when auto-refresh or rapid manual updates happen
    now = time.time()
    if _live_board_cache["data"] is not None and (now - _live_board_cache["ts"]) < _live_board_cache["ttl"]:
        live_boards = _live_board_cache["data"]
    else:
        # Lock prevents cache stampede: only one coroutine fetches; others wait then reuse result.
        async with _live_board_lock:
            now = time.time()  # re-check after acquiring lock
            if _live_board_cache["data"] is not None and (now - _live_board_cache["ts"]) < _live_board_cache["ttl"]:
                live_boards = _live_board_cache["data"]
            else:
                try:
                    live_boards = await _tdx.get_train_live_board(top=800)
                    _live_board_cache["data"] = live_boards
                    _live_board_cache["ts"] = now
                except Exception as e:
                    raise HTTPException(502, f"TDX live fetch failed: {e}")

    # TDX occasionally returns duplicate entries for the same TrainNo (same station, same
    # UpdateTime, but different DelayTime values). Keep the entry with the higher DelayTime
    # to avoid showing the same train twice with slightly different ETAs.
    _dedup: dict = {}
    for _lb in live_boards:
        _no = _lb.get("TrainNo")
        if _no not in _dedup or (_lb.get("DelayTime") or 0) > (_dedup[_no].get("DelayTime") or 0):
            _dedup[_no] = _lb
    live_boards = list(_dedup.values())

    # We need to know which trains are on this line.
    # TrainLiveBoard typically reports current StationID + Delay.
    # To know Line, we ideally cross with timetable, but for MVP we use StationOfLine reverse lookup
    # or fetch a sample of daily timetable for context. For speed, use live station -> possible lines.

    # Build station -> lines map (from cached station_of_lines)
    # Include both coarse and detailed so filtering works for TL-N etc.
    station_to_lines: dict[str, list[str]] = {}
    for lid, stas in _geo.station_of_lines.items():
        for s in stas:
            sid = s.get("StationID")
            if sid:
                station_to_lines.setdefault(sid, []).append(lid)
    # Also map detailed shapes to the stations of their effective
    for shape_lid, eff in _geo.shape_to_sol.items():
        if eff in _geo.station_of_lines:
            for s in _geo.station_of_lines[eff]:
                sid = s.get("StationID")
                if sid:
                    station_to_lines.setdefault(sid, []).append(shape_lid)

    # Get per-line direction info from StationOfLine ordered list (by Sequence).
    # This is the authoritative way to label directions without relying on global
    # train number parity (which reverses on 花東線/EL, 南迴線/SL, 集集線/JJ etc.)
    # and without assuming N/S orientation (many lines are E-W or branches).
    dir_info = _geo.get_line_direction_ends(line_id) or _geo.get_line_direction_ends(effective_line)
    low_end_name = (dir_info or {}).get("low_end_name") or "低序列端"
    high_end_name = (dir_info or {}).get("high_end_name") or "高序列端"
    seq_by_sid = (dir_info or {}).get("seq_by_sid", {})
    idx_by_sid = (dir_info or {}).get("index_by_sid", {})
    cum_by_sid = (dir_info or {}).get("cum_by_sid", {})

    # Also get station cum for quick lookup (use effective for detailed lines)
    station_cum: dict[str, float] = {}
    for lid, stas in _geo.station_of_lines.items():
        if lid != effective_line and lid != line_id:
            continue
        for s in stas:
            sid = s.get("StationID")
            cd = s.get("CumulativeDistance")
            if sid and cd is not None:
                station_cum[sid] = float(cd)

    now = datetime.now()
    now_min = now.hour * 60 + now.minute
    danger_set = set(danger_ids)

    # Get line name — prefer nice display names (縱貫線北段 etc.)
    line_name = getattr(_geo, "line_display_names", {}).get(line_id) or getattr(_geo, "line_display_names", {}).get(effective_line)
    if not line_name:
        line_name = (_geo.lines.get(line_id) or {}).get("LineName", line_id)
        if isinstance(line_name, dict):
            line_name = line_name.get("Zh_tw", line_id)
        if line_name in (line_id, effective_line) and effective_line in _geo.lines:
            ln2 = (_geo.lines.get(effective_line) or {}).get("LineName", effective_line)
            if isinstance(ln2, dict):
                ln2 = ln2.get("Zh_tw", effective_line)
            if ln2 not in (line_id, effective_line):
                line_name = ln2

    # Determine user's position index on the line's ordered station list (for heading comparison)
    user_idx = None
    user_closest_sid_for_dir = None
    min_diff = float("inf")
    for sid, cm in station_cum.items():
        if sid in idx_by_sid:
            d = abs(cm - mileage)
            if d < min_diff:
                min_diff = d
                user_closest_sid_for_dir = sid
    if user_closest_sid_for_dir:
        user_idx = idx_by_sid.get(user_closest_sid_for_dir)

    # === Optimized collection to reduce TDX calls (was the main cause of slow updates) ===
    # 1. First pass: line filter + rough eta, collect candidates (no timetable yet)
    # 2. Parallel gather only for the candidates (usually <15 after line filter)
    # 3. Cache in tdx_client makes repeated updates almost free (1 LiveBoard call)
    # User's quota ~5 req/s is easily satisfied with auto every 10-15s + cache.

    candidates = []  # basic info before tt
    for lb in live_boards:
        train_no = lb.get("TrainNo")
        curr_sid = lb.get("StationID")
        delay_min = lb.get("DelayTime") or 0
        direction = lb.get("Direction")
        update_time = lb.get("UpdateTime") or lb.get("SrcUpdateTime")

        lines_for_station = station_to_lines.get(curr_sid, [])
        if effective_line not in lines_for_station and line_id not in lines_for_station:
            continue

        curr_mile = station_cum.get(curr_sid)
        if curr_mile is None:
            continue

        dist_km = abs(mileage - curr_mile)
        curr_idx = idx_by_sid.get(curr_sid)

        # rough eta for pre-filter
        rough_eta = (dist_km / 70.0) * 60.0 + max(0, delay_min)
        if rough_eta > 90:
            continue

        candidates.append({
            "lb": lb,
            "train_no": train_no,
            "curr_sid": curr_sid,
            "curr_mile": curr_mile,
            "curr_idx": curr_idx,
            "dist_km": dist_km,
            "delay_min": delay_min,
            "direction": direction,
            "update_time": update_time,
            "rough_eta": rough_eta,
        })

    # Parallel fetch timetables only for candidates (huge speedup vs sequential await per lb)
    timetable_map = {}
    if candidates:
        tasks = [_tdx.get_train_daily_timetable(c["train_no"]) for c in candidates]
        tt_results = await asyncio.gather(*tasks, return_exceptions=True)
        for c, res in zip(candidates, tt_results):
            if not isinstance(res, Exception) and res is not None:
                timetable_map[c["train_no"]] = res

    # Two buckets for the two directions along *this specific line's* ordering
    toward_low = []   # trains heading toward low_end (e.g. 往二水 / 往基隆), will pass user
    toward_high = []  # trains heading toward high_end (e.g. 往車埕 / 往屏東)

    for c in candidates:
        lb = c["lb"]
        train_no = c["train_no"]
        curr_sid = c["curr_sid"]
        curr_mile = c["curr_mile"]
        curr_idx = c["curr_idx"]
        dist_km = c["dist_km"]
        delay_min = c["delay_min"]
        direction = c["direction"]
        update_time = c["update_time"]

        tt = timetable_map.get(train_no)

        # --- ETA + heading from timetable (or fallback) ---
        eta_min = None
        dest_name = ""
        train_heading_high = None
        if tt:
            try:
                train_info = tt.get("TrainInfo", {})
                end_name = train_info.get("EndingStationName") or {}
                if isinstance(end_name, dict):
                    dest_name = end_name.get("Zh_tw") or ""
                elif isinstance(end_name, str):
                    dest_name = end_name
                if not dest_name:
                    hs = train_info.get("TripHeadSign") or ""
                    if isinstance(hs, str) and hs.startswith("往"):
                        dest_name = hs.replace("往", "")

                stop_times = tt.get("StopTimes", [])
                # --- Direction detection: use LOCAL timetable context around curr_sid ---
                # The old approach (comparing first vs last seq on the user's line) breaks for
                # loop trains (環島列車) whose timetable visits the same line twice in opposite
                # directions: the final station on the line equals (or is lower than) the first,
                # so train_heading_high comes out wrong and the train gets stuck in alert after
                # it has already passed the user.
                # Fix: find curr_sid's position in StopTimes, then look at the immediately
                # adjacent stop that is also on the user's line to determine the *current*
                # direction of travel. This is always correct regardless of loop / branch routes.
                train_heading_high = None
                curr_tt_idx = next(
                    (i for i, s in enumerate(stop_times) if s.get("StationID") == curr_sid),
                    None,
                )
                if curr_tt_idx is not None:
                    curr_seq = seq_by_sid.get(curr_sid)
                    if curr_seq is not None:
                        # Look forward in timetable for next stop on this line
                        for j in range(curr_tt_idx + 1, len(stop_times)):
                            nsid = stop_times[j].get("StationID")
                            if nsid in seq_by_sid:
                                train_heading_high = seq_by_sid[nsid] > curr_seq
                                break
                        # If no forward stop on this line, infer from previous stop
                        if train_heading_high is None:
                            for j in range(curr_tt_idx - 1, -1, -1):
                                psid = stop_times[j].get("StationID")
                                if psid in seq_by_sid:
                                    train_heading_high = curr_seq > seq_by_sid[psid]
                                    break
                # Fallback: curr_sid not found in StopTimes (pass-through station, etc.)
                # Use global first-vs-last comparison as a best-effort guess.
                if train_heading_high is None:
                    _line_seqs = [
                        seq_by_sid[s.get("StationID")]
                        for s in stop_times
                        if s.get("StationID") in seq_by_sid
                    ]
                    if len(_line_seqs) >= 2:
                        train_heading_high = _line_seqs[-1] > _line_seqs[0]

                stops_with_mile = []
                for s in stop_times:
                    sid = s.get("StationID")
                    if sid in station_cum:
                        mile = station_cum[sid]
                        tstr = s.get("DepartureTime") or s.get("ArrivalTime")
                        if tstr:
                            h, m = map(int, tstr.split(":"))
                            stops_with_mile.append((mile, h * 60 + m))
                stops_with_mile.sort(key=lambda x: x[0])

                before = after = None
                for i in range(len(stops_with_mile) - 1):
                    if stops_with_mile[i][0] <= mileage <= stops_with_mile[i + 1][0]:
                        before = stops_with_mile[i]
                        after = stops_with_mile[i + 1]
                        break
                if before and after and after[0] > before[0]:
                    frac = (mileage - before[0]) / (after[0] - before[0])
                    sched = before[1] + frac * (after[1] - before[1])
                    now_min = datetime.now().hour * 60 + datetime.now().minute
                    pred = sched + delay_min
                    if pred < now_min - 60:
                        pred += 24 * 60
                    eta_min = max(0, pred - now_min)
            except Exception:
                eta_min = None

        if eta_min is None:
            speed_kmh = 70.0
            eta_min = (dist_km / speed_kmh) * 60.0 + max(0, delay_min)

        # heading + approaching using tt-derived or skip for safety
        is_approaching = False
        heading_key = None
        if train_heading_high is not None and user_idx is not None and curr_idx is not None:
            if train_heading_high and curr_idx < user_idx:
                is_approaching = True
                heading_key = "high"
            elif not train_heading_high and curr_idx > user_idx:
                is_approaching = True
                heading_key = "low"

        if not is_approaching:
            continue

        if eta_min > 90:
            continue

        # === Special monitoring rules for trains whose origin or destination is inside the danger/monitoring zone ===
        # (per user spec for field safety / glanceable UI)
        # - If 發車站 (first StopTime) is in danger zone: only start showing/monitoring this train 5 min before its (delayed) scheduled departure.
        # - If 終點站 (last StopTime) is in danger zone: once the train has arrived at the terminal (by time or LiveBoard curr), remove it from display and stop monitoring.
        # These are applied on top of the existing is_approaching + eta<90 filters. Requires tt (most candidates have it).
        if tt:
            try:
                stop_times = tt.get("StopTimes", []) or []
                if stop_times:
                    o_stop = stop_times[0]
                    d_stop = stop_times[-1]
                    o_sid = o_stop.get("StationID")
                    d_sid = d_stop.get("StationID")
                    delay_val = max(0, delay_min or 0)

                    # Origin rule
                    if o_sid and o_sid in danger_set:
                        tstr = o_stop.get("DepartureTime") or o_stop.get("ArrivalTime") or ""
                        if tstr and ":" in tstr:
                            parts = tstr.split(":")
                            h, m = int(parts[0]), int(parts[1])
                            odep = h * 60 + m
                            adj = odep
                            if adj < now_min - 60:
                                adj += 24 * 60
                            eff_dep = adj + delay_val
                            if (eff_dep - now_min) > 5:
                                continue  # still >5 min until departure from origin inside zone → don't show yet

                    # Destination rule
                    if d_sid and d_sid in danger_set:
                        arrived = False
                        if curr_sid and curr_sid == d_sid:
                            arrived = True
                        tstr = d_stop.get("ArrivalTime") or d_stop.get("DepartureTime") or ""
                        if tstr and ":" in tstr:
                            parts = tstr.split(":")
                            h, m = int(parts[0]), int(parts[1])
                            darr = h * 60 + m
                            adj = darr
                            if adj < now_min - 60:
                                adj += 24 * 60
                            eff_arr = adj + delay_val
                            if now_min >= eff_arr:
                                arrived = True
                        if arrived:
                            continue  # reached terminal inside monitoring range → remove display,解除監控
            except Exception:
                # fail open (don't hide a train we can't parse) for safety
                pass

        ttype_id = lb.get("TrainTypeID")
        ttype_name = lb.get("TrainTypeName", {}).get("Zh_tw") if isinstance(lb.get("TrainTypeName"), dict) else lb.get("TrainTypeName")

        # current station pos for map display of monitored trains
        curr_pos = None
        st = _geo.stations.get(curr_sid) if curr_sid else None
        if st:
            curr_pos = {
                "lat": st.get("PositionLat"),
                "lon": st.get("PositionLon"),
            }

        # next station pos for this specific train, to point arrow to its next stop direction
        next_station_pos = None
        tt = timetable_map.get(train_no)
        if tt and curr_sid:
            try:
                stop_times = tt.get("StopTimes", [])
                for ii, s in enumerate(stop_times):
                    if s.get("StationID") == curr_sid:
                        if ii + 1 < len(stop_times):
                            ns = stop_times[ii + 1]
                            nsid = ns.get("StationID")
                            nst = _geo.stations.get(nsid, {})
                            nlat = nst.get("PositionLat")
                            nlon = nst.get("PositionLon")
                            if nlat and nlon:
                                next_station_pos = {"lat": nlat, "lon": nlon}
                        break
            except Exception:
                pass

        # Compute local track direction (bearing 0=north, clockwise) so the map arrow
        # can be rotated to be parallel to the actual railway at this location.
        track_angle = None
        try:
            if heading_key and curr_idx is not None:
                eff_stas = _geo.station_of_lines.get(effective_line, [])
                delta = 1 if heading_key == "high" else -1
                next_idx = curr_idx + delta
                if 0 <= next_idx < len(eff_stas):
                    next_sid = eff_stas[next_idx].get("StationID")
                    cpos = _geo.stations.get(curr_sid, {})
                    npos = _geo.stations.get(next_sid, {})
                    clat = cpos.get("PositionLat") or cpos.get("lat")
                    clon = cpos.get("PositionLon") or cpos.get("lon")
                    nlat = npos.get("PositionLat") or npos.get("lat")
                    nlon = npos.get("PositionLon") or npos.get("lon")
                    if clat and clon and nlat and nlon:
                        dlat = nlat - clat
                        dlon = nlon - clon
                        mean_lat_rad = math.radians((clat + nlat) / 2)
                        angle_rad = math.atan2(dlon * math.cos(mean_lat_rad), dlat)
                        track_angle = math.degrees(angle_rad)
        except Exception:
            track_angle = None

        # Relative hint now line-end aware
        if heading_key == "high":
            direction_hint = f"列車目前在{low_end_name}側，往{high_end_name}方向通過"
        elif heading_key == "low":
            direction_hint = f"列車目前在{high_end_name}側，往{low_end_name}方向通過"
        else:
            delta_m = mileage - curr_mile
            direction_hint = "列車目前在較低里程側" if delta_m > 0 else "列車目前在較高里程側"

        item = {
            "train_no": train_no,
            "train_type": ttype_name or ttype_id,
            "current_station_id": curr_sid,
            "current_mile_km": round(curr_mile, 2) if curr_mile else None,
            "direction": direction,
            "direction_hint": direction_hint,
            "delay_min": delay_min,
            "est_eta_min": round(eta_min, 1),
            "dist_km": round(dist_km, 2),
            "update_time": update_time,
            "will_pass": True,
            "dest_name": dest_name,
            "heading": heading_key,
            "current_station_pos": curr_pos,
            "track_angle": track_angle,
            "next_station_pos": next_station_pos,
        }

        if heading_key == "low":
            toward_low.append(item)
        elif heading_key == "high":
            toward_high.append(item)

    # sort each direction group by eta
    toward_low.sort(key=lambda x: x["est_eta_min"])
    toward_high.sort(key=lambda x: x["est_eta_min"])

    # Build the two direction panels using the line's own ends (no more global parity)
    directions = [
        {
            "key": "low",
            "label": f"往{low_end_name}方向",
            "end_name": low_end_name,
            "trains": toward_low[:2],
        },
        {
            "key": "high",
            "label": f"往{high_end_name}方向",
            "end_name": high_end_name,
            "trains": toward_high[:2],
        },
    ]

    return {
        "line_id": line_id,
        "line_name": line_name,
        "user_mileage_km": round(mileage, 3),
        "user_mileage_k": f"K{int(mileage)}+{int(round((mileage - int(mileage)) * 1000)):03d}",
        "danger_zone": {
            "station_ids": danger_ids,
            "station_names": danger_names,
            "stations": danger_stations,
            "buffer_km": buffer_km,
        },
        "directions": directions,
        # Keep legacy keys for any external consumers; they will be empty or approximate
        "from_north": toward_low[:2],
        "from_south": toward_high[:2],
    }


@app.get("/api/lines")
async def list_lines():
    """Helper: list known lines for UI dropdown fallback."""
    out = []
    for lid, ln in _geo.lines.items():
        name = ln.get("LineName") or {}
        if isinstance(name, dict):
            name = name.get("Zh_tw", lid)
        out.append({"id": lid, "name": name})
    return {"lines": out}


@app.on_event("startup")
async def on_startup():
    print("TRA Position App starting...")
    print(f"  Data dir: {DATA_DIR}")
    print(f"  Loaded lines: {len(_geo.lines)}  shapes: {len(_geo.shapes)}")
    # warm token?
    try:
        tok = await _tdx.get_token()
        print("  TDX token acquired OK")
    except Exception as e:
        print(f"  Warning: could not get TDX token yet (will retry on first call): {e}")


@app.on_event("shutdown")
async def on_shutdown():
    await _tdx.close()
