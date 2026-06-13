#!/usr/bin/env python3
"""
Fetch and cache static TRA data from TDX for the app.
Run after setting TDX_CLIENT_ID / TDX_CLIENT_SECRET in env.
Usage:
  python scripts/fetch_static_data.py
"""
import asyncio
import json
from pathlib import Path
import sys

# add parent for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.tdx_client import get_client
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


async def fetch_and_save():
    client = get_client()
    print("Fetching Lines...")
    lines = await client.get_lines()
    (DATA_DIR / "lines.json").write_text(json.dumps(lines, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Saved {len(lines)} lines")

    print("Fetching Stations...")
    stations = await client.get_stations(top=2000)
    (DATA_DIR / "stations.json").write_text(json.dumps(stations, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Saved {len(stations)} stations")

    print("Fetching StationOfLine (all lines)...")
    # Note: may be paginated or large; TDX allows $top large or multiple calls, here ask high
    sol = await client.get_station_of_line(top=5000)
    (DATA_DIR / "station_of_lines.json").write_text(json.dumps(sol, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Saved {len(sol)} line entries (with stations)")

    print("Fetching Shapes...")
    shapes = await client.get_shapes(top=2000)
    (DATA_DIR / "shapes.json").write_text(json.dumps(shapes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Saved {len(shapes)} shapes")

    print("Fetching TrainTypes...")
    ttypes = await client.get_train_types()
    (DATA_DIR / "train_types.json").write_text(json.dumps(ttypes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Saved {len(ttypes)} train types")

    await client.close()
    print("\nAll static data cached to ./data/")
    print("You can now run the app.")


if __name__ == "__main__":
    asyncio.run(fetch_and_save())
