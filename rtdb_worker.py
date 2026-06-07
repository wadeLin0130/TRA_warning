#!/usr/bin/env python3
"""
RTDB Worker — standalone, no uvicorn dependency.

Directly calls TDX API and computes train ETAs, then pushes results to Firebase RTDB.
One LiveBoard fetch per cycle is shared across all watched positions (more efficient).

Run:
  cd /Users/weidilin/tra-position-app
  source .venv/bin/activate
  python rtdb_worker.py
"""

import asyncio
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, db

from app.tdx_client import TDXClient
from app.geo import get_geo_index

_DIR = Path(__file__).parent.resolve()

SERVICE_ACCOUNT_FILE = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS",
    str(_DIR / "tra-trainwarning-firebase-adminsdk-fbsvc-8a3ec14a06.json"),
)
DB_URL = os.getenv("FIREBASE_DB_URL", "https://tra-trainwarning-default-rtdb.asia-southeast1.firebasedatabase.app")

POLL_INTERVAL = 6          # seconds between cycles
CLEANUP_AGE_SECONDS = 300  # remove watchers idle >5 min


def init_firebase():
    cred = credentials.Certificate(SERVICE_ACCOUNT_FILE) if os.path.exists(SERVICE_ACCOUNT_FILE) \
        else credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred, {"databaseURL": DB_URL})
    print("[worker] Firebase initialized.")


async def compute_upcoming(line_id: str, mileage: float, buffer_km: float, geo, tdx, live_boards: list) -> dict:
    """
    Compute approaching trains for a given line + mileage.
    live_boards is pre-fetched for the cycle (shared across all positions).
    Logic mirrors app/main.py get_upcoming endpoint.
    """
    effective_line = geo.shape_to_sol.get(line_id, line_id)
    danger_ids, danger_names, danger_stations = geo.get_danger_zone_stations(
        effective_line, mileage, num_before=2, num_after=2, buffer_km=buffer_km
    )

    # Deduplicate live boards by TrainNo (keep higher DelayTime)
    dedup: dict = {}
    for lb in live_boards:
        no = lb.get("TrainNo")
        if no not in dedup or (lb.get("DelayTime") or 0) > (dedup[no].get("DelayTime") or 0):
            dedup[no] = lb
    boards = list(dedup.values())

    # Build station → lines map
    station_to_lines: dict[str, list[str]] = {}
    for lid, stas in geo.station_of_lines.items():
        for s in stas:
            sid = s.get("StationID")
            if sid:
                station_to_lines.setdefault(sid, []).append(lid)
    for shape_lid, eff in geo.shape_to_sol.items():
        if eff in geo.station_of_lines:
            for s in geo.station_of_lines[eff]:
                sid = s.get("StationID")
                if sid:
                    station_to_lines.setdefault(sid, []).append(shape_lid)

    # Direction info
    dir_info = geo.get_line_direction_ends(line_id) or geo.get_line_direction_ends(effective_line) or {}
    low_end_name  = dir_info.get("low_end_name")  or "低序列端"
    high_end_name = dir_info.get("high_end_name") or "高序列端"
    seq_by_sid    = dir_info.get("seq_by_sid", {})
    idx_by_sid    = dir_info.get("index_by_sid", {})

    # Station cumulative distances for this line
    station_cum: dict[str, float] = {}
    for lid, stas in geo.station_of_lines.items():
        if lid not in (effective_line, line_id):
            continue
        for s in stas:
            sid = s.get("StationID")
            cd  = s.get("CumulativeDistance")
            if sid and cd is not None:
                station_cum[sid] = float(cd)

    # User's index on the ordered station list
    user_idx = None
    min_diff = float("inf")
    for sid, cm in station_cum.items():
        if sid in idx_by_sid:
            d = abs(cm - mileage)
            if d < min_diff:
                min_diff = d
                user_idx = idx_by_sid[sid]

    # Line display name
    line_name = geo.line_display_names.get(line_id) or geo.line_display_names.get(effective_line)
    if not line_name:
        ln = (geo.lines.get(line_id) or {}).get("LineName", line_id)
        line_name = ln.get("Zh_tw", line_id) if isinstance(ln, dict) else ln

    now_min = datetime.now().hour * 60 + datetime.now().minute
    danger_set = set(danger_ids)

    # Station list sorted by mileage — used to find which interval a train is in
    eff_stas_sorted = sorted(
        geo.station_of_lines.get(effective_line, []),
        key=lambda s: float(s.get("CumulativeDistance") or 0)
    )

    def _sname(s):
        if not s:
            return None
        sid = s.get("StationID")
        if sid and sid in geo.stations:
            return geo.stations[sid].get("StationName")
        n = s.get("StationName") or {}
        return n.get("Zh_tw") if isinstance(n, dict) else (n or sid)

    # First pass: filter by line + rough ETA
    candidates = []
    for lb in boards:
        train_no  = lb.get("TrainNo")
        curr_sid  = lb.get("StationID")
        delay_min = lb.get("DelayTime") or 0
        direction = lb.get("Direction")
        update_time = lb.get("UpdateTime") or lb.get("SrcUpdateTime")

        if effective_line not in station_to_lines.get(curr_sid, []) and \
           line_id not in station_to_lines.get(curr_sid, []):
            continue

        curr_mile = station_cum.get(curr_sid)
        if curr_mile is None:
            continue

        dist_km   = abs(mileage - curr_mile)
        curr_idx  = idx_by_sid.get(curr_sid)
        rough_eta = (dist_km / 70.0) * 60.0 + max(0, delay_min)
        if rough_eta > 90:
            continue

        candidates.append({
            "lb": lb, "train_no": train_no, "curr_sid": curr_sid,
            "curr_mile": curr_mile, "curr_idx": curr_idx,
            "dist_km": dist_km, "delay_min": delay_min,
            "direction": direction, "update_time": update_time,
        })

    # Parallel timetable fetch
    timetable_map: dict = {}
    if candidates:
        tasks = [tdx.get_train_daily_timetable(c["train_no"]) for c in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for c, res in zip(candidates, results):
            if not isinstance(res, Exception) and res is not None:
                timetable_map[c["train_no"]] = res

    toward_low, toward_high = [], []

    for c in candidates:
        lb        = c["lb"]
        train_no  = c["train_no"]
        curr_sid  = c["curr_sid"]
        curr_mile = c["curr_mile"]
        curr_idx  = c["curr_idx"]
        dist_km   = c["dist_km"]
        delay_min = c["delay_min"]
        update_time = c["update_time"]
        tt = timetable_map.get(train_no)

        eta_min = None
        dest_name = ""
        train_heading_high = None

        if tt:
            try:
                train_info = tt.get("TrainInfo", {})
                end_name = train_info.get("EndingStationName") or {}
                dest_name = (end_name.get("Zh_tw") if isinstance(end_name, dict) else end_name) or ""
                if not dest_name:
                    hs = train_info.get("TripHeadSign") or ""
                    if isinstance(hs, str) and hs.startswith("往"):
                        dest_name = hs[1:]

                stop_times = tt.get("StopTimes", [])
                line_seqs = [seq_by_sid[s["StationID"]] for s in stop_times if s.get("StationID") in seq_by_sid]
                if len(line_seqs) >= 2:
                    train_heading_high = line_seqs[-1] > line_seqs[0]

                stops_with_mile = []
                for s in stop_times:
                    sid = s.get("StationID")
                    if sid in station_cum:
                        tstr = s.get("DepartureTime") or s.get("ArrivalTime")
                        if tstr:
                            h, m = map(int, tstr.split(":"))
                            stops_with_mile.append((station_cum[sid], h * 60 + m))
                stops_with_mile.sort(key=lambda x: x[0])

                for i in range(len(stops_with_mile) - 1):
                    if stops_with_mile[i][0] <= mileage <= stops_with_mile[i+1][0]:
                        before, after = stops_with_mile[i], stops_with_mile[i+1]
                        if after[0] > before[0]:
                            frac  = (mileage - before[0]) / (after[0] - before[0])
                            sched = before[1] + frac * (after[1] - before[1])
                            pred  = sched + delay_min
                            cur   = datetime.now().hour * 60 + datetime.now().minute
                            if pred < cur - 60:
                                pred += 24 * 60
                            eta_min = max(0, pred - cur)
                        break
            except Exception:
                eta_min = None

        if eta_min is None:
            eta_min = (dist_km / 70.0) * 60.0 + max(0, delay_min)

        # Determine heading
        is_approaching = False
        heading_key = None
        if train_heading_high is not None and user_idx is not None and curr_idx is not None:
            if train_heading_high and curr_idx < user_idx:
                is_approaching = True; heading_key = "high"
            elif not train_heading_high and curr_idx > user_idx:
                is_approaching = True; heading_key = "low"

        if not is_approaching or eta_min > 90:
            continue

        # Origin / destination rules (safety: don't show trains not yet departed from inside zone,
        # or already arrived at terminal inside zone)
        if tt:
            try:
                stop_times = tt.get("StopTimes", []) or []
                if stop_times:
                    cur_min = datetime.now().hour * 60 + datetime.now().minute
                    delay_val = max(0, delay_min or 0)
                    o_stop, d_stop = stop_times[0], stop_times[-1]
                    o_sid, d_sid   = o_stop.get("StationID"), d_stop.get("StationID")

                    if o_sid and o_sid in danger_set:
                        tstr = o_stop.get("DepartureTime") or o_stop.get("ArrivalTime") or ""
                        if ":" in tstr:
                            h, m = map(int, tstr.split(":"))
                            odep = h * 60 + m
                            if odep < cur_min - 60: odep += 24 * 60
                            if (odep + delay_val - cur_min) > 5:
                                continue

                    if d_sid and d_sid in danger_set:
                        arrived = (curr_sid == d_sid)
                        tstr = d_stop.get("ArrivalTime") or d_stop.get("DepartureTime") or ""
                        if ":" in tstr:
                            h, m = map(int, tstr.split(":"))
                            darr = h * 60 + m
                            if darr < cur_min - 60: darr += 24 * 60
                            if cur_min >= darr + delay_val:
                                arrived = True
                        if arrived:
                            continue
            except Exception:
                pass

        ttype_name = (lb.get("TrainTypeName") or {}).get("Zh_tw") \
            if isinstance(lb.get("TrainTypeName"), dict) else lb.get("TrainTypeName")

        # Current station position for map
        curr_pos = None
        st = geo.stations.get(curr_sid) if curr_sid else None
        if st:
            curr_pos = {"lat": st.get("PositionLat"), "lon": st.get("PositionLon")}

        # Next station position for arrow direction
        next_station_pos = None
        if tt and curr_sid:
            try:
                for ii, s in enumerate(tt.get("StopTimes", [])):
                    if s.get("StationID") == curr_sid and ii + 1 < len(tt["StopTimes"]):
                        ns   = tt["StopTimes"][ii + 1]
                        nsst = geo.stations.get(ns.get("StationID"), {})
                        if nsst.get("PositionLat"):
                            next_station_pos = {"lat": nsst["PositionLat"], "lon": nsst["PositionLon"]}
                        break
            except Exception:
                pass

        # Track bearing for map arrow
        track_angle = None
        try:
            if heading_key and curr_idx is not None:
                eff_stas = geo.station_of_lines.get(effective_line, [])
                ni = curr_idx + (1 if heading_key == "high" else -1)
                if 0 <= ni < len(eff_stas):
                    cp = geo.stations.get(curr_sid, {})
                    np_ = geo.stations.get(eff_stas[ni].get("StationID"), {})
                    clat, clon = cp.get("PositionLat"), cp.get("PositionLon")
                    nlat, nlon = np_.get("PositionLat"), np_.get("PositionLon")
                    if clat and nlat:
                        track_angle = math.degrees(
                            math.atan2((nlon - clon) * math.cos(math.radians((clat + nlat) / 2)), nlat - clat)
                        )
        except Exception:
            pass

        direction_hint = (
            f"列車目前在{low_end_name}側，往{high_end_name}方向通過" if heading_key == "high"
            else f"列車目前在{high_end_name}側，往{low_end_name}方向通過"
        )

        # Find which two stations the train is currently between
        _before = _after = None
        for _s in eff_stas_sorted:
            _cd = float(_s.get("CumulativeDistance") or 0)
            if _cd <= curr_mile:
                _before = _s
            else:
                _after = _s
                break
        _bn, _an = _sname(_before), _sname(_after)
        between_stations = f"{_bn}－{_an}" if _bn and _an else (_bn or _an)

        item = {
            "train_no": train_no,
            "train_type": ttype_name or lb.get("TrainTypeID"),
            "current_station_id": curr_sid,
            "current_mile_km": round(curr_mile, 2) if curr_mile else None,
            "between_stations": between_stations,
            "direction": c["direction"],
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
        (toward_low if heading_key == "low" else toward_high).append(item)

    toward_low.sort(key=lambda x: x["est_eta_min"])
    toward_high.sort(key=lambda x: x["est_eta_min"])

    km_int  = int(mileage)
    km_frac = int(round((mileage - km_int) * 1000))
    return {
        "line_id": line_id,
        "line_name": line_name,
        "user_mileage_km": round(mileage, 3),
        "user_mileage_k": f"K{km_int}+{km_frac:03d}",
        "danger_zone": {
            "station_ids": danger_ids,
            "station_names": danger_names,
            "stations": danger_stations,
            "buffer_km": buffer_km,
        },
        "directions": [
            {"key": "low",  "label": f"往{low_end_name}方向",  "end_name": low_end_name,  "trains": toward_low[:2]},
            {"key": "high", "label": f"往{high_end_name}方向", "end_name": high_end_name, "trains": toward_high[:2]},
        ],
        "from_north": toward_low[:2],
        "from_south": toward_high[:2],
    }


async def main():
    print("=== RTDB Worker (standalone) starting ===")
    init_firebase()

    geo = get_geo_index(_DIR / "data")
    tdx = TDXClient()

    # Warm TDX token
    try:
        await tdx.get_token()
        print("[worker] TDX token OK")
    except Exception as e:
        print(f"[worker] TDX token warning: {e}")

    while True:
        cycle_start = time.time()
        try:
            watched = db.reference("/watched_positions").get() or {}
            if not watched:
                print("[worker] No active watchers.")
            else:
                # Fetch LiveBoard ONCE per cycle, shared across all positions
                try:
                    live_boards = await tdx.get_train_live_board(top=800)
                except Exception as e:
                    print(f"[worker] LiveBoard fetch failed: {e}")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                print(f"[worker] {len(watched)} watcher(s), {len(live_boards)} live trains")

                for client_id, position in watched.items():
                    line_id = position.get("line_id")
                    mileage = position.get("mileage")
                    buffer  = float(position.get("buffer_km") or 2.0)
                    if not line_id or mileage is None:
                        continue
                    try:
                        result = await compute_upcoming(line_id, float(mileage), buffer, geo, tdx, live_boards)
                        payload = {
                            **result,
                            "client_id": client_id,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                        db.reference(f"/user_results/{client_id}").set(payload)
                        low_n  = len(result["directions"][0]["trains"])
                        high_n = len(result["directions"][1]["trains"])
                        print(f"[worker] {client_id[:16]} @ {result['user_mileage_k']} "
                              f"往低端:{low_n} 往高端:{high_n}")
                    except Exception as e:
                        print(f"[worker] Error for {client_id}: {e}")

            # Cleanup stale watchers
            now_ts = time.time()
            for client_id, pos in list((watched or {}).items()):
                updated = pos.get("updated_at", 0)
                if isinstance(updated, str):
                    try:
                        updated = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        updated = 0
                if now_ts - updated > CLEANUP_AGE_SECONDS:
                    db.reference(f"/watched_positions/{client_id}").delete()
                    db.reference(f"/user_results/{client_id}").delete()
                    print(f"[worker] Cleaned stale {client_id}")

        except Exception as e:
            print(f"[worker] Cycle error: {e}")

        elapsed = time.time() - cycle_start
        await asyncio.sleep(max(0, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[worker] Stopped.")
