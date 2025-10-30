import sqlite3
import os
import json

NODE_CACHE_DB = "nodes_cache.db"

def generate_hopmap_json(max_hops):
    if not os.path.exists(NODE_CACHE_DB):
        raise RuntimeError("No cache file found")

    conn = sqlite3.connect(NODE_CACHE_DB)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, latitude, longitude, hopsAway FROM nodes "
        "WHERE lastHeard >= strftime('%s','now') - ? "
        "AND hopsAway BETWEEN 0 AND ?",
        (7 * 86400, max_hops) # results with lastheard between now and 7 days ago, 86400 seconds per day
    )
    rows = cursor.fetchall()
    conn.close()

    result = []
    for nid, lat, lon, hops in rows:
        try:
            lat_val = float(lat) if lat is not None else None
            lon_val = float(lon) if lon is not None else None
            hops_val = int(hops)
        except (ValueError, TypeError):
            continue
        color = "green" if lat_val is not None and lon_val is not None else "red"
        result.append({
            "id": nid,
            "lat": lat_val,
            "lon": lon_val,
            "hops": hops_val,
            "color": color
        })

    with open("nodes.json", "w") as f:
        json.dump(result, f, indent=2)

if __name__ == "__main__":
    generate_hopmap_json(4) # generate up to 4 hops away, limiting it to make testing simple
