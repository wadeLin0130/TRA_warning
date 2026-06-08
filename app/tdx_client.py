"""
TDX API Client for TRA (Taiwan Railway) data.
Handles OAuth token acquisition/refresh and authenticated requests to TDX v3 basic rail endpoints.
"""
import asyncio
import os
import time
from typing import Any, Dict, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

TDX_AUTH_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
TDX_BASE_URL = "https://tdx.transportdata.tw/api/basic/v3/Rail/TRA"

class TDXClient:
    def __init__(self, client_id: Optional[str] = None, client_secret: Optional[str] = None):
        self.client_id = client_id or os.getenv("TDX_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("TDX_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            raise ValueError("TDX_CLIENT_ID and TDX_CLIENT_SECRET must be provided via env or constructor")

        self._token: Optional[str] = None
        self._token_expires_at: float = 0
        self._client = httpx.AsyncClient(timeout=30.0)
        self._tt_cache: dict[str, tuple[dict, float]] = {}  # train_no -> (tt_data, expires_at)

    async def _fetch_token(self) -> str:
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        resp = await self._client.post(TDX_AUTH_URL, data=data, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
        access_token = payload["access_token"]
        # expires_in is seconds, usually 3600
        expires_in = int(payload.get("expires_in", 3600))
        self._token_expires_at = time.time() + expires_in - 60  # refresh 1min early
        self._token = access_token
        return access_token

    async def get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at:
            return self._token
        return await self._fetch_token()

    async def request(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        method: str = "GET",
    ) -> Dict[str, Any]:
        """
        Make authenticated request. path e.g. '/StationOfLine' or full relative.
        """
        token = await self.get_token()
        url = f"{TDX_BASE_URL}{path}" if not path.startswith("http") else path
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept-Encoding": "gzip",
        }
        if params is None:
            params = {}
        # Always ask for JSON
        if "$format" not in params:
            params["$format"] = "JSON"

        # Retry up to 3 times on 429 (TDX rate-limit) with exponential backoff.
        # This handles short bursts (e.g. cold-start timetable fetches) without surfacing
        # errors to users when TDX would have served the request moments later.
        last_exc = None
        for attempt in range(3):
            resp = await self._client.request(method, url, headers=headers, params=params)
            if resp.status_code == 429 and attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))  # 0.5 s, then 1.0 s
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()  # should not reach, but keeps type checker happy
        return resp.json()

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await self.request(path, params=params, method="GET")

    async def close(self):
        await self._client.aclose()

    # Convenience high-level methods for TRA

    async def get_lines(self) -> list:
        data = await self.get("/Line")
        return data.get("Lines", []) if "Lines" in data else data.get("data", [])

    async def get_stations(self, top: int = 1000) -> list:
        params = {"$top": top}
        data = await self.get("/Station", params=params)
        return data.get("Stations", []) if "Stations" in data else data.get("data", [])

    async def get_station_of_line(self, line_id: Optional[str] = None, top: int = 1000) -> list:
        params = {"$top": top}
        if line_id:
            params["$filter"] = f"LineID eq '{line_id}'"
        data = await self.get("/StationOfLine", params=params)
        # v3 returns { "StationOfLines": [ {LineID, Stations: [...]}, ... ] }
        return data.get("StationOfLines", []) if "StationOfLines" in data else data.get("data", [])

    async def get_shapes(self, top: int = 1000) -> list:
        params = {"$top": top}
        data = await self.get("/Shape", params=params)
        return data.get("Shapes", []) if "Shapes" in data else data.get("data", [])

    async def get_train_types(self) -> list:
        data = await self.get("/TrainType")
        return data.get("TrainTypes", []) if "TrainTypes" in data else data.get("data", [])

    async def get_train_live_board(self, train_nos: Optional[list] = None, top: int = 500) -> list:
        params = {"$top": top}
        if train_nos:
            # OData filter
            quoted = ",".join([f"'{t}'" for t in train_nos])
            params["$filter"] = f"TrainNo in ({quoted})"
        data = await self.get("/TrainLiveBoard", params=params)
        # Typically { "TrainLiveBoards": [...] }
        return data.get("TrainLiveBoards", []) if "TrainLiveBoards" in data else data.get("data", [])

    async def get_station_live_board(self, station_id: Optional[str] = None, top: int = 200) -> list:
        if station_id:
            data = await self.get(f"/StationLiveBoard/Station/{station_id}")
            return data.get("StationLiveBoards", [])
        params = {"$top": top}
        data = await self.get("/StationLiveBoard", params=params)
        return data.get("StationLiveBoards", [])

    async def get_daily_timetable_today(self, top: int = 100) -> list:
        """Light sample; full is huge, use filter or specific train when needed."""
        params = {"$top": top}
        data = await self.get("/DailyTrainTimetable/Today", params=params)
        return data.get("TrainTimetables", []) if "TrainTimetables" in data else data.get("data", [])

    async def get_train_daily_timetable(self, train_no: str):
        """Get full timetable for a specific train today. Very useful for accurate passing time prediction.
        Uses in-memory cache (TTL 1h) because daily timetables are stable.
        """
        now = time.time()
        if train_no in self._tt_cache:
            tt, expires_at = self._tt_cache[train_no]
            if now < expires_at:
                return tt
        data = await self.get(f"/DailyTrainTimetable/Today/TrainNo/{train_no}")
        if data.get("TrainTimetables"):
            tt = data["TrainTimetables"][0]
            self._tt_cache[train_no] = (tt, now + 3600)  # cache 1 hour
            return tt
        return None


# Singleton helper
_client: Optional[TDXClient] = None

def get_client() -> TDXClient:
    global _client
    if _client is None:
        _client = TDXClient()
    return _client
