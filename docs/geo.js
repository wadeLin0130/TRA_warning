/**
 * geo.js — JavaScript port of app/geo.py
 * GPS (lat, lon) → Taiwan Railway line + official mileage (km)
 * Uses the same shapes.json / station_of_lines.json / stations.json as the backend.
 */

const SHAPE_TO_SOL = {
  "TL": "WL", "TL-N": "WL", "TL-S": "WL", "TL-M": "WL",
  "TL-C": "WL-C", "YL": "EL", "NL": "EL", "TT": "EL", "PL": "SL",
};

const LINE_DISPLAY_NAMES = {
  "TL-N": "縱貫線北段", "TL-S": "縱貫線南段", "TL-M": "縱貫線山線",
  "TL-C": "縱貫線海線", "TL": "縱貫線", "WL": "西部幹線",
  "WL-C": "西部幹線海線", "WL-M": "西部幹線山線",
  "EL": "東部幹線", "YL": "宜蘭線", "NL": "北迴線",
  "TT": "台東線", "SL": "南迴線",
};

const LINE_MILEAGE_OFFSETS = {
  "WL": 4.75, "TL": 4.75, "TL-N": 4.75, "TL-S": 4.75,
  "TL-M": 4.75, "TL-C": 4.75, "WL-C": 4.75, "WL-M": 4.75,
};

// Haversine distance in meters
function haversineM(lat1, lon1, lat2, lon2) {
  const R = 6371000;
  const dLat = (lat2 - lat1) * Math.PI / 180;
  const dLon = (lon2 - lon1) * Math.PI / 180;
  const a = Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
    Math.sin(dLon / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

// Project pt onto segment [a, b] using local cartesian approximation.
// Returns { projLat, projLon, frac, distM }
function projectPointToSegment(ptLat, ptLon, aLat, aLon, bLat, bLon) {
  const mPerDegLat = 110540;
  const mPerDegLon = 111320 * Math.cos(23.5 * Math.PI / 180);

  const x2 = (bLon - aLon) * mPerDegLon;
  const y2 = (bLat - aLat) * mPerDegLat;
  const xp = (ptLon - aLon) * mPerDegLon;
  const yp = (ptLat - aLat) * mPerDegLat;

  const dx = x2, dy = y2;
  let frac = 0, projX = 0, projY = 0;
  const lenSq = dx * dx + dy * dy;
  if (lenSq > 0) {
    frac = Math.max(0, Math.min(1, (xp * dx + yp * dy) / lenSq));
    projX = frac * dx;
    projY = frac * dy;
  }

  const projLon = aLon + projX / mPerDegLon;
  const projLat = aLat + projY / mPerDegLat;
  const distM = Math.hypot(xp - projX, yp - projY);
  return { projLat, projLon, frac, distM };
}

// Parse WKT LINESTRING or MULTILINESTRING → [[lon,lat], ...]
function parseWKT(geom) {
  if (!geom || typeof geom !== 'string') return [];
  const groups = [];
  const re = /\(([^()]+)\)/g;
  let m;
  while ((m = re.exec(geom)) !== null) {
    const pairs = m[1].trim().split(',').map(p => {
      const parts = p.trim().split(/\s+/);
      return parts.length >= 2 ? [parseFloat(parts[0]), parseFloat(parts[1])] : null;
    }).filter(Boolean);
    if (pairs.length > groups.flat().length / 2) groups.push(...pairs);
    else groups.push(...pairs);
  }
  // Return the longest group
  // Actually just return all parsed pairs in order
  const re2 = /\(([^()]+)\)/g;
  let best = [];
  while ((m = re2.exec(geom)) !== null) {
    const pairs = m[1].trim().split(',').map(p => {
      const parts = p.trim().split(/\s+/);
      return parts.length >= 2 ? [parseFloat(parts[0]), parseFloat(parts[1])] : null;
    }).filter(Boolean);
    if (pairs.length > best.length) best = pairs;
  }
  return best; // [[lon, lat], ...]
}

class TRAGeoIndex {
  constructor() {
    this.lines = {};           // lineId -> line obj
    this.stations = {};        // stationId -> {StationID, StationName, PositionLat, PositionLon}
    this.stationOfLines = {};  // lineId -> [{StationID, Sequence, CumulativeDistance, StationPosition}]
    this.shapes = {};          // lineId -> [[lon, lat], ...]
    this._lineShapeCum = {};   // lineId -> [cumulativeM, ...]
    this._lineAnchors = {};    // lineId -> [{shapeCumM, officialKm, stationId}]
    this.shapesToSol = SHAPE_TO_SOL;
    this.lineDisplayNames = LINE_DISPLAY_NAMES;
  }

  async load(base = './data') {
    const [shapesRaw, solRaw, stationsRaw, linesRaw] = await Promise.all([
      fetch(`${base}/shapes.json`).then(r => r.json()),
      fetch(`${base}/station_of_lines.json`).then(r => r.json()),
      fetch(`${base}/stations.json`).then(r => r.json()),
      fetch(`${base}/lines.json`).then(r => r.json()),
    ]);

    // lines
    for (const ln of linesRaw) {
      const lid = ln.LineID || ln.lineID;
      if (lid) this.lines[lid] = ln;
    }

    // stations
    for (const st of stationsRaw) {
      const sid = st.StationID || st.stationID;
      if (!sid) continue;
      const pos = st.StationPosition || {};
      const name = st.StationName;
      this.stations[sid] = {
        StationID: sid,
        StationName: typeof name === 'object' ? (name.Zh_tw || sid) : (name || sid),
        PositionLat: pos.PositionLat || st.PositionLat,
        PositionLon: pos.PositionLon || st.PositionLon,
      };
    }

    // station_of_lines (with mileage offset correction)
    for (const sol of solRaw) {
      const lid = sol.LineID;
      const stas = (sol.Stations || []).slice().sort((a, b) => (a.Sequence || 0) - (b.Sequence || 0));
      const offset = LINE_MILEAGE_OFFSETS[lid] || 0;
      for (const s of stas) {
        if (s.CumulativeDistance != null) s.CumulativeDistance = parseFloat(s.CumulativeDistance) + offset;
        // Enrich with position from stations map
        const sid = s.StationID;
        if (sid && this.stations[sid] && !s.StationPosition) {
          s.StationPosition = {
            PositionLat: this.stations[sid].PositionLat,
            PositionLon: this.stations[sid].PositionLon,
          };
        }
        // Normalize StationName to string
        if (s.StationName && typeof s.StationName === 'object') {
          s.StationName = s.StationName.Zh_tw || sid;
        }
      }
      this.stationOfLines[lid] = stas;
    }

    // shapes
    for (const sh of shapesRaw) {
      const lid = sh.LineID;
      const coords = parseWKT(sh.Geometry);
      if (lid && coords.length >= 2) this.shapes[lid] = coords;
    }

    this._buildIndices();
  }

  _buildIndices() {
    for (const [lid, coords] of Object.entries(this.shapes)) {
      if (coords.length < 2) continue;

      // Cumulative distances along shape (meters)
      const cum = [0];
      for (let i = 1; i < coords.length; i++) {
        const d = haversineM(coords[i-1][1], coords[i-1][0], coords[i][1], coords[i][0]);
        cum.push(cum[cum.length - 1] + d);
      }
      this._lineShapeCum[lid] = cum;

      // Anchors: for each station on this line, find closest point on shape
      const effectiveLid = SHAPE_TO_SOL[lid] || lid;
      const stas = this.stationOfLines[effectiveLid] || this.stationOfLines[lid] || [];
      const anchors = [];
      for (const s of stas) {
        const cd = s.CumulativeDistance;
        if (cd == null) continue;
        const pos = s.StationPosition || {};
        const slat = pos.PositionLat, slon = pos.PositionLon;
        if (!slat || !slon) continue;
        let bestCum = null, bestD = Infinity;
        for (let i = 0; i < coords.length; i++) {
          const d = haversineM(slat, slon, coords[i][1], coords[i][0]);
          if (d < bestD) { bestD = d; bestCum = cum[i]; }
        }
        if (bestCum != null) anchors.push({ shapeCumM: bestCum, officialKm: parseFloat(cd), stationId: s.StationID });
      }
      anchors.sort((a, b) => a.shapeCumM - b.shapeCumM);
      if (anchors.length) this._lineAnchors[lid] = anchors;
    }
  }

  _interpolateMileage(shapeCumM, anchors) {
    if (!anchors || !anchors.length) return null;
    let left = null, right = null;
    for (const a of anchors) {
      if (a.shapeCumM <= shapeCumM) left = a;
      if (a.shapeCumM >= shapeCumM && !right) { right = a; break; }
    }
    if (left && right && left.shapeCumM !== right.shapeCumM) {
      const ratio = (shapeCumM - left.shapeCumM) / (right.shapeCumM - left.shapeCumM);
      return { km: left.officialKm + ratio * (right.officialKm - left.officialKm), sid: ratio < 0.5 ? left.stationId : right.stationId };
    }
    if (left) return { km: left.officialKm, sid: left.stationId };
    if (right) return { km: right.officialKm, sid: right.stationId };
    return { km: shapeCumM < anchors[0].shapeCumM ? anchors[0].officialKm : anchors[anchors.length-1].officialKm, sid: anchors[0].stationId };
  }

  findClosest(lat, lon, maxDistM = 5000) {
    let best = null, bestD = Infinity;

    for (const [lid, coords] of Object.entries(this.shapes)) {
      if (coords.length < 2) continue;
      const cum = this._lineShapeCum[lid] || [];

      let minD = Infinity, bestProj = null, bestFrac = 0, bestSegIdx = 0;
      for (let i = 0; i < coords.length - 1; i++) {
        const r = projectPointToSegment(lat, lon, coords[i][1], coords[i][0], coords[i+1][1], coords[i+1][0]);
        if (r.distM < minD) { minD = r.distM; bestProj = r; bestFrac = r.frac; bestSegIdx = i; }
      }
      if (minD > maxDistM) continue;

      // Cumulative distance along shape to projection
      const c0 = cum[bestSegIdx] || 0, c1 = cum[bestSegIdx + 1] || 0;
      const shapeCumM = c0 + bestFrac * (c1 - c0);

      // Interpolate official km from anchors
      const effectiveLid = SHAPE_TO_SOL[lid] || lid;
      const anchors = this._lineAnchors[effectiveLid] || this._lineAnchors[lid];
      const interp = this._interpolateMileage(shapeCumM, anchors);
      if (!interp) continue;
      let mileageKm = interp.km;

      // Refine mileage using station-pair projection (matches Python second pass)
      const effStas = this.stationOfLines[effectiveLid] || [];
      if (effStas.length && bestProj) {
        let bestPairD = Infinity, bestA = null, bestB = null;
        for (let j = 0; j < effStas.length - 1; j++) {
          const pa = effStas[j].StationPosition || {}, pb = effStas[j+1].StationPosition || {};
          if (!pa.PositionLat || !pb.PositionLat) continue;
          const r = projectPointToSegment(bestProj.projLat, bestProj.projLon, pa.PositionLat, pa.PositionLon, pb.PositionLat, pb.PositionLon);
          if (r.distM < bestPairD) { bestPairD = r.distM; bestA = effStas[j]; bestB = effStas[j+1]; }
        }
        if (bestA && bestB) {
          const ca = parseFloat(bestA.CumulativeDistance || 0);
          const cb = parseFloat(bestB.CumulativeDistance || 0);
          const pa = bestA.StationPosition || {}, pb = bestB.StationPosition || {};
          if (pa.PositionLat && pb.PositionLat) {
            const r2 = projectPointToSegment(bestProj.projLat, bestProj.projLon, pa.PositionLat, pa.PositionLon, pb.PositionLat, pb.PositionLon);
            mileageKm = ca + r2.frac * (cb - ca);
          }
        }
      }

      // Nearest station (overall)
      let nsid = null, nsName = null, nsDist = Infinity;
      for (const [sid, st] of Object.entries(this.stations)) {
        if (!st.PositionLat) continue;
        const d = haversineM(lat, lon, st.PositionLat, st.PositionLon);
        if (d < nsDist) { nsDist = d; nsid = sid; nsName = st.StationName; }
      }

      const lineName = LINE_DISPLAY_NAMES[lid] || (() => {
        const ln = this.lines[lid];
        if (!ln) return lid;
        const n = ln.LineName;
        return typeof n === 'object' ? (n.Zh_tw || lid) : (n || lid);
      })();

      const confidence = Math.max(0, Math.min(1, 1 - minD / 3000));
      const km = Math.round(mileageKm * 1000) / 1000;
      const kInt = Math.floor(km);
      const kFrac = Math.round((km - kInt) * 1000);

      if (minD < bestD) {
        bestD = minD;
        best = {
          lineId: lid,
          lineName,
          mileageKm: km,
          mileageK: `K${kInt}+${String(kFrac).padStart(3, '0')}`,
          confidence,
          projectionLat: Math.round(bestProj.projLat * 1e6) / 1e6,
          projectionLon: Math.round(bestProj.projLon * 1e6) / 1e6,
          nearestStationId: nsid,
          nearestStationName: nsName,
          nearestStationDistM: Math.round(nsDist),
        };
      }
    }
    return best;
  }

  findNearbyLines(lat, lon, maxDistM = 1000, maxResults = 5) {
    const candidates = [];
    for (const [lid, coords] of Object.entries(this.shapes)) {
      if (coords.length < 2) continue;
      const cum = this._lineShapeCum[lid] || [];
      let minD = Infinity, bestFrac = 0, bestSegIdx = 0;
      for (let i = 0; i < coords.length - 1; i++) {
        const r = projectPointToSegment(lat, lon, coords[i][1], coords[i][0], coords[i+1][1], coords[i+1][0]);
        if (r.distM < minD) { minD = r.distM; bestFrac = r.frac; bestSegIdx = i; }
      }
      if (minD > maxDistM) continue;
      const c0 = cum[bestSegIdx] || 0, c1 = cum[bestSegIdx+1] || 0;
      const shapeCumM = c0 + bestFrac * (c1 - c0);
      const anchors = this._lineAnchors[lid];
      const interp = this._interpolateMileage(shapeCumM, anchors);
      if (!interp) continue;
      const km = Math.round(interp.km * 1000) / 1000;
      const kInt = Math.floor(km), kFrac = Math.round((km - kInt) * 1000);
      candidates.push({
        line_id: lid,
        line_name: LINE_DISPLAY_NAMES[lid] || lid,
        mileage_km: km,
        mileage_k: `K${kInt}+${String(kFrac).padStart(3, '0')}`,
        dist_m: Math.round(minD),
        effective_line_id: SHAPE_TO_SOL[lid] || lid,
      });
    }
    // dedup by effective line, keep closest
    const effBest = {};
    for (const c of candidates) {
      const eff = c.effective_line_id;
      if (!effBest[eff] || c.dist_m < effBest[eff].dist_m) {
        c.line_name = LINE_DISPLAY_NAMES[eff] || c.line_name;
        effBest[eff] = c;
      }
    }
    return Object.values(effBest).sort((a, b) => a.dist_m - b.dist_m).slice(0, maxResults);
  }

  getLineDirectionEnds(lineId) {
    const effective = SHAPE_TO_SOL[lineId] || lineId;
    const stas = this.stationOfLines[effective] || [];
    if (!stas.length) return null;
    const ordered = stas.slice().sort((a, b) => (a.Sequence || 0) - (b.Sequence || 0));
    const getName = n => typeof n === 'object' ? (n.Zh_tw || '') : (n || '');
    const low = ordered[0], high = ordered[ordered.length - 1];
    const seqBySid = {}, indexBySid = {}, cumBySid = {};
    ordered.forEach((s, i) => {
      const sid = s.StationID;
      if (sid) {
        seqBySid[sid] = s.Sequence || i;
        indexBySid[sid] = i;
        if (s.CumulativeDistance != null) cumBySid[sid] = parseFloat(s.CumulativeDistance);
      }
    });
    return {
      effectiveLineId: effective,
      lowEndId: low.StationID,
      lowEndName: getName(low.StationName),
      highEndId: high.StationID,
      highEndName: getName(high.StationName),
      orderedStations: ordered,
      seqBySid,
      indexBySid,
      cumBySid,
    };
  }

  getDangerZoneStations(lineId, userMileage, bufferKm = 2.0) {
    const effective = SHAPE_TO_SOL[lineId] || lineId;
    const stas = this.stationOfLines[effective] || [];
    const zone = stas.filter(s => Math.abs((parseFloat(s.CumulativeDistance) || 0) - userMileage) <= bufferKm)
                     .sort((a, b) => (a.Sequence || 0) - (b.Sequence || 0));
    const ids = [], names = [], details = [];
    for (const s of zone) {
      const sid = s.StationID;
      if (!sid) continue;
      ids.push(sid);
      const name = typeof s.StationName === 'object' ? (s.StationName.Zh_tw || sid) : (s.StationName || sid);
      names.push(name);
      const pos = s.StationPosition || this.stations[sid] || {};
      details.push({ id: sid, name, lat: pos.PositionLat, lon: pos.PositionLon });
    }
    return { ids, names, details };
  }
}

// Singleton
let _geoIndex = null;
async function getGeoIndex(base = './data') {
  if (!_geoIndex) {
    _geoIndex = new TRAGeoIndex();
    await _geoIndex.load(base);
  }
  return _geoIndex;
}
