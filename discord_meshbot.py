import os, io, time, json, threading, asyncio, re, random, math
from datetime import datetime, timedelta

import discord
from discord.ext import commands
from discord import Interaction
from discord import app_commands

from meshtastic.tcp_interface import TCPInterface
from meshtastic.serial_interface import SerialInterface
from pubsub import pub  # pip install pypubsub

import matplotlib
matplotlib.use("Agg")  # Must come before importing pyplot
import matplotlib.pyplot as plt
import aiosqlite
import io
import math
import matplotlib.pyplot as plt
import os
import random
import sqlite3, aiosqlite
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CENTER_LAT = os.getenv("CENTER_LAT")
CENTER_LON = os.getenv("CENTER_LON")
host = os.getenv("MESHNODE_IP")
NODE_CACHE_DB = os.getenv("NODE_CACHE_DB")
HISTORY_DB_FILE = os.getenv("HISTORY_DB_FILE")
meshportnum = os.getenv("MESHPORTNUM")
INCOMING_CHANNEL_NAME = os.getenv("INCOMING_CHANNEL_NAME")
MY_ID = os.getenv("MY_NODE_ID") # count num of hops for map from this node as center of map
permsinteger = os.getenv("DISCORD_BOT_PERMS")
botspam_output_channel = os.getenv("BOTSPAM_OUTPUT_CHANNELNAME")

# ====== CONFIG ======
NODE_REFRESH_INTERVAL = 900             # num of seconds to refresh nodedb from device
MAX_DISTANCE_KM = 500 # ignore client gps info if its bigger than this value
OUTLIER_MARGIN = 0.85 # variance tolerance on mapping
CLEANUP_INTERVAL = 14400  # node_history deduplication every 4hrs

AUTHORIZED_SEND_USERS = [
    int(x.strip()) for x in os.getenv("AUTHORIZED_SEND_USERS", "").split(",") if x.strip()
]



intents = discord.Intents.default()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)
MESH_SESSION = None
GUILD_ID = None


class MeshSession:
    def __init__(self, host):
        self.node_ip = host
        self.node_list = []
        self.node_list_lock = threading.Lock()
        self.my_node_id = None
        self.interface = None
        self.online = False
        self._stop_event = threading.Event()

        # background thread for db connections, cant block the mesh api or messages will be lost
        threading.Thread(target=self._background_loop, daemon=True).start()


    def _background_loop(self):
        last_refresh = 0
        last_cleanup = 0
        cache_conn, cache_cursor = None, None
        history_conn, history_cursor = None, None
        

        # key values were valid for 2.6.11 , might change in the future
        COMMON_COLUMNS = """
            id TEXT,
            longName TEXT,
            shortName TEXT,
            role TEXT,
            macaddr TEXT,
            hwModel TEXT,
            isLicensed INTEGER,
            isConfigured INTEGER,
            hopsAway INTEGER,
            snr REAL,
            rxSnr REAL,
            channel TEXT,
            firmwareVersion TEXT,
            batteryLevel INTEGER,
            voltage REAL,
            airUtilTx REAL,
            channelUtilization REAL,
            uptimeSeconds INTEGER,
            txPower INTEGER,
            temperature REAL,
            humidity REAL,
            barometricPressure REAL,
            latitude REAL,
            longitude REAL,
            latitudeI INTEGER,
            longitudeI INTEGER,
            altitude REAL,
            altitudeM INTEGER,
            positionTime INTEGER,
            positionTimestamp INTEGER,
            positionAccuracy REAL,
            altitudeAccuracy REAL,
            gpsState INTEGER
        """
        CACHE_COLUMNS = COMMON_COLUMNS + ", lastHeard INTEGER, firstSeen TEXT"
        HISTORY_COLUMNS = "timestamp TEXT, " + COMMON_COLUMNS

        while not self._stop_event.is_set():
            try:
                if cache_conn is None:
                    print("[mesh] Connecting to cache database...")
                    cache_conn = sqlite3.connect(NODE_CACHE_DB)
                    cache_conn.row_factory = sqlite3.Row  # enables dict-like rows
                    cache_cursor = cache_conn.cursor()
                    cache_cursor.execute(f"CREATE TABLE IF NOT EXISTS nodes ({CACHE_COLUMNS})")
                    cache_conn.commit()


                if history_conn is None:
                    print("[mesh] Connecting to history database...")
                    history_conn = sqlite3.connect(HISTORY_DB_FILE)
                    history_cursor = history_conn.cursor()
                    history_cursor.execute(f"CREATE TABLE IF NOT EXISTS node_history ({HISTORY_COLUMNS})")
                    history_conn.commit()
                    print("[mesh] History database ready.")

                if not self.online or not self._reader_alive():
                    self.connect()

                # only pull from mesh device if x amount of time has passed
                now_ts = time.time()

                if self.online and (now_ts - last_refresh >= NODE_REFRESH_INTERVAL):
                    self.fetch_nodes(cache_conn, cache_cursor, history_conn, history_cursor)
                    last_refresh = now_ts

                if now_ts - last_cleanup >= CLEANUP_INTERVAL:
                    threading.Thread(target=cleanup_history_db, args=(HISTORY_DB_FILE,), daemon=True).start()
                    last_cleanup = now_ts

                time.sleep(1) # small delay for thread to establish

            except sqlite3.Error as e:
                print(f"[mesh] Database error: {e}")
                if cache_conn: cache_conn.close()
                if history_conn: history_conn.close()
                cache_conn, cache_cursor, history_conn, history_cursor = None, None, None, None
            except Exception as e:
                print(f"[mesh] General error in background loop: {e}")
                self.online = False
                if cache_conn: cache_conn.close()
                if history_conn: history_conn.close()
                cache_conn, cache_cursor, history_conn, history_cursor = None, None, None, None

        if cache_conn: cache_conn.close()
        if history_conn: history_conn.close()
        print("[mesh] All database connections closed.")

    def _load_nodes_from_db(self, cursor):
        with self.node_list_lock:
            cursor.execute("SELECT * FROM nodes")
            rows = cursor.fetchall()
            self.node_list = []

            for row in rows:
                node_info = {
                    "id": row["id"],
                    "longName": row["longName"],
                    "shortName": row["shortName"],
                    "role": row["role"],
                    "hopsAway": row["hopsAway"],
                    "snr": row["snr"],
                    "batteryLevel": row["batteryLevel"],
                    "voltage": row["voltage"],
                    "airUtilTx": row["airUtilTx"],
                    "channelUtilization": row["channelUtilization"],
                    "hwModel": row["hwModel"] if row["hwModel"] is not None else row["hwModel"],
                    "position": {
                        "latitude": row["latitude"],
                        "longitude": row["longitude"]
                    } if row["latitude"] is not None else None,
                    "lastHeard": row["lastHeard"],
                    "firstSeen": row["firstSeen"]
                }

                self.node_list.append((node_info["id"], node_info))

                self.my_node_id = next(
                    (nid for nid, info in self.node_list if info.get("role") == "self"),
                    None
                )



    def _reader_alive(self):
        return self.online and self.interface is not None


    def connect(self):
        if self.interface:
            try:
                self.interface.close()
            except Exception:
                pass
            self.interface = None
            self.online = False


        # serial connect path
        if self.node_ip.lower() == "serial":
            print("[mesh] Connecting via serial interface...")
            try:
                self.interface = SerialInterface()
                pub.subscribe(self.on_receive, "meshtastic.receive")
                self.online = True
                print("[mesh] Serial connection established")
            except Exception as e:
                self.interface = None
                self.online = False
                print(f"[mesh] Failed to connect via serial: {e}")
            return

        # tcp/ip path
        print(f"[mesh] Connecting to {self.node_ip} via TCP...")
        try:
            self.interface = TCPInterface(hostname=self.node_ip, portNumber="4404")
            pub.subscribe(self.on_receive, "meshtastic.receive")
            self.online = True
            print("[mesh] TCP connection established")
        except Exception as e:
            self.interface = None
            self.online = False
            print(f"[mesh] Failed to connect via TCP: {e}")









    def fetch_nodes(self, cache_conn, cache_cursor, history_conn, history_cursor):
        if not self.online or not self.interface:
            raise RuntimeError("Not connected to mesh host")

        print("[mesh] Fetching and logging nodes from mesh...")
        current_time_iso = datetime.utcnow().isoformat()
        current_time_ts = int(datetime.utcnow().timestamp())
        nodes_to_process = self.interface.nodes.copy()
        try:
            for node_id, node in nodes_to_process.items():
                user = node.get("user", {})
                metrics = node.get("deviceMetrics", {})
                pos = node.get("position", {})

                hops_raw = node.get("hopsAway")

                if node_id == MY_ID:
                    hops = 0

                try:
                    hops = int(hops_raw)
                    if hops < 0 or hops > 99:
                        hops = 99
                except (TypeError, ValueError):
                    hops = 99

                # arrange node data
                values = {
                    "id": node_id,
                    "longName": user.get("longName", "Unknown"),
                    "shortName": user.get("shortName"),
                    "role": user.get("role", "none"),
                    "macaddr": user.get("macaddr"),
                    "hwModel": user.get("hwModel") or node.get("hardwareModel"),
                    "isLicensed": int(user.get("isLicensed", 0)),
                    "isConfigured": int(user.get("isConfigured", 0)),

                    "hopsAway": hops,
                    "snr": node.get("snr"),
                    "rxSnr": node.get("rxSnr"),
                    "channel": node.get("channel"),
                    "firmwareVersion": node.get("firmwareVersion"),

                    # deviceMetrics
                    "batteryLevel": metrics.get("batteryLevel"),
                    "voltage": metrics.get("voltage"),
                    "airUtilTx": metrics.get("airUtilTx"),
                    "channelUtilization": metrics.get("channelUtilization"),
                    "uptimeSeconds": metrics.get("uptimeSeconds"),
                    "txPower": metrics.get("txPower"),
                    "temperature": metrics.get("temperature"),
                    "humidity": metrics.get("humidity"),
                    "barometricPressure": metrics.get("barometricPressure"),

                    # position
                    "latitude": pos.get("latitude"),
                    "longitude": pos.get("longitude"),
                    "latitudeI": pos.get("latitudeI"),
                    "longitudeI": pos.get("longitudeI"),
                    "altitude": pos.get("altitude"),
                    "altitudeM": pos.get("altitudeM"),
                    "positionTime": pos.get("time"),
                    "positionTimestamp": pos.get("timestamp"),
                    "positionAccuracy": pos.get("accuracy"),
                    "altitudeAccuracy": pos.get("altitudeAccuracy"),
                    "gpsState": pos.get("gpsState"),

                    # lifecycle
                    "lastHeard": current_time_ts,
                    "firstSeen": datetime.utcnow().isoformat()
                }

                # check if node exists in cache
                cache_cursor.execute("SELECT firstSeen FROM nodes WHERE id = ?", (node_id,))
                existing_first_seen = cache_cursor.fetchone()

                if existing_first_seen:
                    # node exists → update everything except firstSeen
                    cache_cursor.execute("""
                        UPDATE nodes SET
                            longName=?, shortName=?, role=?, macaddr=?, hwModel=?, isLicensed=?, isConfigured=?,
                            hopsAway=?, snr=?, rxSnr=?, channel=?, firmwareVersion=?,
                            batteryLevel=?, voltage=?, airUtilTx=?, channelUtilization=?, uptimeSeconds=?,
                            txPower=?, temperature=?, humidity=?, barometricPressure=?,
                            latitude=?, longitude=?, latitudeI=?, longitudeI=?, altitude=?, altitudeM=?,
                            positionTime=?, positionTimestamp=?, positionAccuracy=?, altitudeAccuracy=?, gpsState=?,
                            lastHeard=?
                        WHERE id=?
                    """, (
                        values["longName"], values["shortName"], values["role"], values["macaddr"], values["hwModel"],
                        values["isLicensed"], values["isConfigured"],
                        values["hopsAway"], values["snr"], values["rxSnr"], values["channel"], values["firmwareVersion"],
                        values["batteryLevel"], values["voltage"], values["airUtilTx"], values["channelUtilization"], values["uptimeSeconds"],
                        values["txPower"], values["temperature"], values["humidity"], values["barometricPressure"],
                        values["latitude"], values["longitude"], values["latitudeI"], values["longitudeI"],
                        values["altitude"], values["altitudeM"], values["positionTime"], values["positionTimestamp"],
                        values["positionAccuracy"], values["altitudeAccuracy"], values["gpsState"],
                        values["lastHeard"], node_id
                    ))
                else:
                    # new node → insert including firstSeen
                    cache_cursor.execute("""
                        INSERT INTO nodes (
                            id, longName, shortName, role, macaddr, hwModel, isLicensed, isConfigured,
                            hopsAway, snr, rxSnr, channel, firmwareVersion,
                            batteryLevel, voltage, airUtilTx, channelUtilization, uptimeSeconds,
                            txPower, temperature, humidity, barometricPressure,
                            latitude, longitude, latitudeI, longitudeI, altitude, altitudeM,
                            positionTime, positionTimestamp, positionAccuracy, altitudeAccuracy, gpsState,
                            lastHeard, firstSeen
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        values["id"], values["longName"], values["shortName"], values["role"], values["macaddr"], values["hwModel"],
                        values["isLicensed"], values["isConfigured"],
                        values["hopsAway"], values["snr"], values["rxSnr"], values["channel"], values["firmwareVersion"],
                        values["batteryLevel"], values["voltage"], values["airUtilTx"], values["channelUtilization"], values["uptimeSeconds"],
                        values["txPower"], values["temperature"], values["humidity"], values["barometricPressure"],
                        values["latitude"], values["longitude"], values["latitudeI"], values["longitudeI"],
                        values["altitude"], values["altitudeM"], values["positionTime"], values["positionTimestamp"],
                        values["positionAccuracy"], values["altitudeAccuracy"], values["gpsState"],
                        values["lastHeard"], values["firstSeen"]
                    ))

                # log history
                history_cursor.execute("""
                    INSERT INTO node_history (
                        timestamp, id, longName, shortName, role, macaddr, hwModel, isLicensed, isConfigured,
                        hopsAway, snr, rxSnr, channel, firmwareVersion,
                        batteryLevel, voltage, airUtilTx, channelUtilization, uptimeSeconds,
                        txPower, temperature, humidity, barometricPressure,
                        latitude, longitude, latitudeI, longitudeI, altitude, altitudeM,
                        positionTime, positionTimestamp, positionAccuracy, altitudeAccuracy, gpsState
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    current_time_iso,
                    values["id"], values["longName"], values["shortName"], values["role"], values["macaddr"], values["hwModel"],
                    values["isLicensed"], values["isConfigured"],
                    values["hopsAway"], values["snr"], values["rxSnr"], values["channel"], values["firmwareVersion"],
                    values["batteryLevel"], values["voltage"], values["airUtilTx"], values["channelUtilization"], values["uptimeSeconds"],
                    values["txPower"], values["temperature"], values["humidity"], values["barometricPressure"],
                    values["latitude"], values["longitude"], values["latitudeI"], values["longitudeI"],
                    values["altitude"], values["altitudeM"], values["positionTime"], values["positionTimestamp"],
                    values["positionAccuracy"], values["altitudeAccuracy"], values["gpsState"]
                ))

            cache_conn.commit()
            history_conn.commit()

            # reload into memory
            self._load_nodes_from_db(cache_cursor)

            print(f"[mesh] Node list refreshed. {len(self.interface.nodes)} nodes updated.")

        except Exception as e:
            print(f"[mesh] Exception fetching nodes: {e}")
            self.online = False

# this forwards received messages onto discord
    def on_receive(self, packet, interface):
        if not self.online:
            return
        try:
            decoded_packet = packet.get('decoded', {})
            if decoded_packet.get('portnum') == 'TEXT_MESSAGE_APP':
                message = decoded_packet['payload'].decode('utf-8', errors='ignore')
                fromnum = packet.get('fromId', 'unknown')

                with self.node_list_lock:
                    info = next((info for nid, info in self.node_list if nid == fromnum), {})

                long_name = info.get('longName', f"Meshtastic {fromnum[-4:]}")
                hops = info.get('hopsAway', '?')
                snr = info.get('snr', None)
                role = info.get('role', 'none')
                channel_utilization = info.get("channelUtilization", None)


                to_id = packet.get('toId') or decoded_packet.get('to')
                if to_id is None or to_id == '^all':
                    prefix = "C]"
                elif to_id == self.my_node_id:
                    prefix = "D]"
                else:
                    prefix = "UNK]"

                if prefix in ("C]", "D]"):
                    formatted_text = (
                        # fromnum is the device ID name where shortname is derived
                        f"Received From: {long_name} - {fromnum}\n"
                        "```\n"
                        f"{message}\n"
                        "```\n Stats: \n"
                        f"`hops = {hops}`\n"
                        f"`snr  = {snr}`\n"
                       # f"`role = {role}`"
                        f"`channel busy % = {channel_utilization}`"
                    )



                    asyncio.run_coroutine_threadsafe(
                        send_to_discord(formatted_text),
                        bot.loop
                    )
        except (KeyError, UnicodeDecodeError):
            pass





    def stop(self):
        self._stop_event.set()  # signal loops to stop
        if self.interface:
            try:
                self.interface.close()
            except Exception:
                pass
        self.online = False


def cleanup_history_db(db_path):
    table_name = "node_history"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table_name}")
    before_count = cur.fetchone()[0]

    cur.execute(f"PRAGMA table_info({table_name})")
    cols = [row[1] for row in cur.fetchall()]

    # exclude the timestamp column
    cols_no_ts = [c for c in cols if c != "timestamp"]
    cols_str = ", ".join(cols_no_ts)

    # drop any rows (excluding timestamp) which are the same value
    # removes duplicate entries
    cur.execute(f"""
        DELETE FROM {table_name}
        WHERE rowid NOT IN (
            SELECT MIN(rowid)
            FROM {table_name}
            GROUP BY {cols_str}
        )
    """)

    # get final row count for log message
    cur.execute(f"SELECT COUNT(*) FROM {table_name}")
    after_count = cur.fetchone()[0]
    
    # save the output, 
    conn.commit()
    cur.execute("VACUUM")
    conn.close()

    removed = before_count - after_count
    print(f"Before: {before_count}, After: {after_count}, Removed: {removed}")


def get_cache_cursor():
    conn = sqlite3.connect(NODE_CACHE_DB)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    return conn, cursor


def latlon_to_km(lat, lon, center_lat, center_lon):
    try:
        if lat is None or lon is None or center_lat is None or center_lon is None:
            return None, None
        
        lat = float(lat)
        lon = float(lon)
        center_lat = float(center_lat)
        center_lon = float(center_lon)

        earth_radius_km = 6371.0

        lat_rad = math.radians(lat)
        lon_rad = math.radians(lon)
        center_lat_rad = math.radians(center_lat)
        center_lon_rad = math.radians(center_lon)

        x = earth_radius_km * (lon_rad - center_lon_rad) * math.cos((lat_rad + center_lat_rad) / 2)
        y = earth_radius_km * (lat_rad - center_lat_rad)
        return x, y
    except (ValueError, TypeError):
        return None, None


async def generate_hopmap_png(max_hops: int, filename: str):
    if not os.path.exists(NODE_CACHE_DB):
        raise RuntimeError("No cache file found")

    nodes = {}
    async with aiosqlite.connect(NODE_CACHE_DB) as db:
        async with db.execute(
            "SELECT id, latitude, longitude, hopsAway FROM nodes WHERE hopsAway BETWEEN 0 AND ?",
            (max_hops,)
        ) as cursor:
            async for row in cursor:
                nid, lat, lon, hops = row
                #print('NodeID: ', nid)

                try:
                    lat = float(lat)
                except (ValueError, TypeError):
                    lat = None

                try:
                    lon = float(lon)
                except (ValueError, TypeError):
                    lon = None

                try:
                    hops = int(hops)
                except (ValueError, TypeError):
                    hops = None
                
                print(nid)

                if nid == MY_ID:
                    #print('found my node and fixing the hop count')
                    hops = 0
                    if lat is None or lon is None:
                        lat = CENTER_LAT
                        lon = CENTER_LON
                        
                if hops not in nodes:
                    nodes[hops] = []

                nodes[hops].append({
                    "_id": nid,
                    "position": {"latitude": lat, "longitude": lon},
                    "hopsAway": hops
                })

    plt.figure(figsize=(8, 8))
    gps_positions_by_hop = {i: [] for i in range(max_hops + 1)}
    
    for hop_count in sorted(nodes.keys()):
        node_list = nodes[hop_count]
        valid_positions = []
        outliers = []

        for n in node_list:
            lat = n["position"]["latitude"]
            lon = n["position"]["longitude"]

            if lat is None or lon is None:
                n["xy"] = None
                continue  # skip invalid coordinates

            x, y = latlon_to_km(lat, lon, CENTER_LAT, CENTER_LON)

            if x is None or y is None:
                n["xy"] = None
                continue  # skip failed conversion

            n["xy"] = (x, y)
            distance = math.hypot(x, y)

            if distance <= MAX_DISTANCE_KM:
                valid_positions.append((x, y))
            else:
                outliers.append((x, y))

        # Only store the valid positions in gps_positions_by_hop
        gps_positions_by_hop[hop_count] = valid_positions




    
    gps_positions = [pos for sublist in gps_positions_by_hop.values() for pos in sublist]
    
    # --- Scaling Logic ---
    if gps_positions:
        xs, ys = zip(*gps_positions)
        max_dist = max(max(abs(x) for x in xs), max(abs(y) for y in ys)) if xs and ys else 0

        MIN_MAP_RANGE_HALF = 10  # 10km from center

        # For hop‑0, force your node to (0,0) and center the map
        if max_hops == 0:
            # Override gps_positions to contain only your node at 0,0
            gps_positions = [(0.0, 0.0)]
            max_plot_range = MIN_MAP_RANGE_HALF
            plt.xlim(-max_plot_range, max_plot_range)
            plt.ylim(-max_plot_range, max_plot_range)
        else:
            # Determine final map range based on max distance or a minimum
            if max_dist < MIN_MAP_RANGE_HALF:
                max_plot_range = MIN_MAP_RANGE_HALF
            else:
                max_plot_range = max_dist * 1.40

            plt.xlim(-max_plot_range, max_plot_range)
            plt.ylim(-max_plot_range, max_plot_range)
    else:
        max_plot_range = 20 * max_hops
        plt.xlim(-max_plot_range, max_plot_range)
        plt.ylim(-max_plot_range, max_plot_range)

    scatter_radius = (plt.xlim()[1] - plt.xlim()[0]) * 0.05
    

    for hop_count in sorted(nodes.keys()):
        node_list = nodes[hop_count]
        valid_positions = []
        outliers = []

        # classify nodes as valid or outlier
        for n in node_list:
            lat = n["position"]["latitude"]
            lon = n["position"]["longitude"]

            if lat is None or lon is None:
                n["xy"] = None
                continue

            x, y = latlon_to_km(lat, lon, CENTER_LAT, CENTER_LON)
            n["xy"] = (x, y)

            if x is None or y is None:
                continue

            distance = math.hypot(x, y)
            if distance <= MAX_DISTANCE_KM:
                valid_positions.append((x, y))
            else:
                outliers.append((x, y))

        gps_positions_by_hop[hop_count] = valid_positions

        # Plot nodes
        for n in node_list:
            node_id = n["_id"]

            if n["xy"] is not None and n["xy"] in valid_positions:
                x, y = n["xy"]
                color = "green"
            elif n["xy"] is not None and n["xy"] in outliers:
                # Place outliers near the edge
                x, y = n["xy"]
                distance = math.hypot(x, y)
                scale = OUTLIER_MARGIN * max_plot_range / distance
                x *= scale
                y *= scale
                color = "red"
            else:
                # Fallback: scatter around other valid nodes
                if valid_positions:
                    base_x, base_y = random.choice(valid_positions)
                    x = base_x + random.uniform(-scatter_radius, scatter_radius)
                    y = base_y + random.uniform(-scatter_radius, scatter_radius)
                else:
                    x = y = 0
                color = "red"

            plt.scatter(x, y, color=color, s=50)
            plt.annotate(node_id, (x, y), xytext=(5, 5), textcoords="offset points",
                        fontsize=8, color=color)



    plt.xlabel("East (km)")
    plt.ylabel("North (km)")
    total_nodes = sum(len(lst) for lst in nodes.values())
    plt.title(f"num nodes {total_nodes} : num hops {max_hops}")
    plt.grid(True)
    plt.axis("equal")
    plt.tight_layout()

    plt.savefig(filename, dpi=300, bbox_inches="tight")
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=300, bbox_inches="tight")
    buf.seek(0)
    plt.close()
    return buf


async def send_to_discord(text):
    channel = discord.utils.get(bot.get_all_channels(), name=INCOMING_CHANNEL_NAME)
    if channel:
        await channel.send(text)
    else:
        print(f"[warn] No channel named #{INCOMING_CHANNEL_NAME} found")


def generate_nodedata_graph(node_id, hwaddr, key):
    conn = None
    try:
        conn = sqlite3.connect(HISTORY_DB_FILE)
        cursor = conn.cursor()

        valid_keys = {
            "batteryLevel": ("Battery Level", "Battery Level (%)", (0.0, 100.0)),
            "airUtilTx": ("Air Utilization TX", "Air Utilization TX (%)", (0.0, 100.0)),
            "channelUtilization": ("Channel Utilization", "Channel Utilization (%)", (0.0, 100.0)),
            "uptimeSeconds": ("Uptime Seconds", "Uptime (s)", None)
        }

        if key not in valid_keys:
            return None, f"Invalid key: {key}"

        title, ylabel, ylim = valid_keys[key]
        query = f"SELECT timestamp, {key} FROM node_history WHERE macaddr = ? ORDER BY timestamp ASC"
        cursor.execute(query, (hwaddr,))
        rows = cursor.fetchall()

        if not rows:
            return None, "No historical data found for this node."

        seen_entries = set()
        unique_data = []
        for row in rows:
            entry_tuple = tuple(row[1:])
            if entry_tuple not in seen_entries:
                seen_entries.add(entry_tuple)
                unique_data.append(row)

        timestamps = [datetime.fromisoformat(row[0]) for row in unique_data]
        values = [row[1] for row in unique_data]

        if not timestamps:
            return None, "No unique data points to graph."

        plt.figure(figsize=(10.24, 7.68), dpi=300)
        plt.plot(timestamps, values, marker='o', linestyle='-')

        plt.title(f"{title} for Node {node_id} ({hwaddr})")
        plt.xlabel("Timestamp (UTC)")
        plt.ylabel(ylabel)
        if ylim:
            plt.ylim(*ylim)
        plt.grid(True)
        plt.xticks(rotation=45, ha='right')
        plt.tight_layout()

        filename = f"{node_id}_{key}_graph.png"
        plt.savefig(filename)
        plt.close()

        return filename, None

    except sqlite3.Error as e:
        return None, f"Database error: {e}"
    except Exception as e:
        return None, f"An unexpected error occurred: {e}"
    finally:
        if conn:
            conn.close()


async def nodesearch_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    conn = None
    try:
        conn, cursor = get_cache_cursor()
        # Find node IDs and longNames that start with the current input
        query = "SELECT DISTINCT id FROM nodes WHERE id LIKE ? LIMIT 25"
        cursor.execute(query, (f'{current}%',))
        
        results = cursor.fetchall()
        
        choices = []
        for row in results:
            choices.append(app_commands.Choice(name=row[0], value=row[0]))
        
        return choices

    except Exception:
        return []
    finally:
        if conn:
            conn.close()


async def register_commands():
    global GUILD_ID
    if not bot.guilds:
        print("[warn] No guilds found, cannot register guild commands.")
        return

    GUILD_ID = bot.guilds[0].id
    guild_obj = discord.Object(id=GUILD_ID)



    @bot.tree.command(name="hopmap", description="Plot mesh nodes up to N hops", guild=guild_obj)
    async def hopmap(interaction: discord.Interaction, hops: int = 1):
        await interaction.response.defer()

        try:
            filename = f"meshplot_{hops}.png"
            buf = await generate_hopmap_png(hops, filename)

            # send to #bot-commands channel
            output_channel = discord.utils.get(interaction.guild.text_channels, name="bot-commands")
            if output_channel is None:
                await interaction.followup.send("Channel #bot-commands not found.")
                return

            file = discord.File(buf, filename=filename)
            await output_channel.send(file=file)
            await interaction.followup.send(f"Hopmap for {hops} hops generated.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed to generate hopmap: {e}", ephemeral=True)


    @bot.tree.command(name="nodes_local", description="Show cached local nodedb", guild=guild_obj)
    async def nodes_local(interaction: discord.Interaction, hopcountlimit: int = 3):
        # get the dedicated output channel
        output_channel = discord.utils.get(interaction.guild.text_channels, name=botspam_output_channel)
        if output_channel is None:
            await interaction.response.send_message("Output channel #nodes-info not found.", ephemeral=True)
            return

        # ack command in private to avoid spam
        await interaction.response.send_message("Processing nodes, output will go to #nodes-info", ephemeral=True)

        conn = None
        try:
            conn, cursor = get_cache_cursor()

            cursor.execute("""
                SELECT id, longName, hopsAway, snr, airUtilTx, role, lastHeard, firstSeen
                FROM nodes
                WHERE hopsAway <= ?
                ORDER BY hopsAway ASC
            """, (hopcountlimit,))
            
            nodes_raw = cursor.fetchall()

            if not nodes_raw:
                await output_channel.send(f"No nodes with hops ≤ {hopcountlimit} in the database.")
                return

            # timestamp of the most recent node update
            cursor.execute("SELECT MAX(lastHeard) FROM nodes")
            latest_last_heard_ts = cursor.fetchone()[0]
            if latest_last_heard_ts:
                ts_utc = datetime.fromtimestamp(latest_last_heard_ts).isoformat() + "Z"
            else:
                ts_utc = "unknown"
            header = f"Cached node list (last seen {ts_utc}):\n"
            fmt = lambda v: f"{v:.2f}" if v is not None else "0.00"
            lines = []

            for row in nodes_raw:
                nid, longName, hops, snr, airtime, role, _, _ = row
                lines.append(
                    f"{nid} ({longName or 'Unknown'}) - hops: {hops}, "
                    f"SNR: {fmt(snr)}, "
                    f"AirTime: {fmt(airtime)}, "
                    f"role: {role or 'unknown'}"
                )

            # chunk message for discord
            current_chunk = header
            for line in lines:
                if len(current_chunk) + len(line) + 1 > 2000:
                    await output_channel.send(current_chunk)
                    current_chunk = line + "\n"
                else:
                    current_chunk += line + "\n"
            if current_chunk:
                await output_channel.send(current_chunk)

        except sqlite3.Error as e:
            await output_channel.send(f"Failed to query database: {e}")
        finally:
            if conn:
                conn.close()


    @bot.tree.command(name="status", description="check bot and Heltec node status", guild=guild_obj)
    async def status(interaction: discord.Interaction):
        await interaction.response.defer()

        if host.lower() == "serial":
            await interaction.followup.send("Bot online : Heltec connected via serial (ping skipped)")
            return

        count = 3
        timeout_ms = 150
        successful_pings = []

        try:
            for i in range(count):
                try:
                    proc = await asyncio.wait_for(
                        asyncio.create_subprocess_exec(
                            "ping", "-c", "1", "-W", str(timeout_ms // 1000), host,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        ),
                        timeout=(timeout_ms / 1000) + 0.01
                    )
                    stdout, _ = await proc.communicate()
                    output = stdout.decode()
                    match = re.search(r"time[=<]([0-9\.]+)", output)
                    if match:
                        successful_pings.append(float(match.group(1)))
                except asyncio.TimeoutError:
                    pass
        except FileNotFoundError:
            await interaction.followup.send("Bot online : Ping command not found on system")
            return

        if successful_pings:
            avg_ping = sum(successful_pings) / len(successful_pings)
            msg = f"Bot online : Heltec online, ping avg {avg_ping:.2f} ms"
        else:
            msg = "Bot online : Heltec offline (no response)"

        await interaction.followup.send(msg)




    @bot.tree.command(
        name="send",
        description="Send a general message over the mesh network",
        guild=guild_obj
    )
    @app_commands.describe(message="The text to send over the mesh")
    async def send(interaction: Interaction, message: str):
        if interaction.user.id not in AUTHORIZED_SEND_USERS:
            print(f"[error] User {interaction.user.id} failed to send message")
            await interaction.response.send_message(
                "You are not authorized to use this command", ephemeral=True
            )
            return

        try:
            if MESH_SESSION.interface:
                MESH_SESSION.interface.sendText(message)
                await interaction.response.send_message(f"[mesh] Sent: {message}", ephemeral=True)

                formatted_text = (
                    f"Sent From: Discord - {interaction.user.display_name}\n"
                    "```\n"
                    f"{message}\n"
                    "```\n"
                )


                asyncio.run_coroutine_threadsafe(
                    send_to_discord(formatted_text),
                    bot.loop
                )

            else:
                await interaction.response.send_message("[mesh] Interface not connected", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"[mesh] Failed to send: {e}", ephemeral=True)


    @bot.tree.command(name="nodedatagraph", description="Search for a specific node and generate a data graph", guild=guild_obj)
    @app_commands.autocomplete(node_id=nodesearch_autocomplete)
    @app_commands.choices(
        key=[
            app_commands.Choice(name="Battery Level", value="batteryLevel"),
            app_commands.Choice(name="Air Utilization TX", value="airUtilTx"),
            app_commands.Choice(name="Channel Utilization", value="channelUtilization"),
            app_commands.Choice(name="Uptime Seconds", value="uptimeSeconds")
        ]
    )
    async def nodesearch(
        interaction: discord.Interaction,
        node_id: str,
        key: app_commands.Choice[str],
        months: int
    ):
        output_channel = discord.utils.get(interaction.guild.text_channels, name=botspam_output_channel)
        if output_channel is None:
            await interaction.response.send_message("Output channel #nodes-info not found.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Processing node `{node_id}` with data key `{key.name}` for the past `{months}` month(s). Output will go to #nodes-info.",
            ephemeral=True
        )

        conn = None
        try:
            conn, cursor = get_cache_cursor()

            cursor.execute("""
                SELECT macaddr, id, longName, hopsAway, snr, airUtilTx, role, lastHeard, firstSeen
                FROM nodes
                WHERE id = ? OR longName LIKE ?
                LIMIT 1
            """, (node_id, f'%{node_id}%'))

            node_data = cursor.fetchone()

            if not node_data:
                await output_channel.send(f"Node '{node_id}' not found.")
                return

            macaddr, nid, longName, hops, snr, airtime, role, lastHeard_ts, firstSeen_iso = node_data
            last_heard_dt = datetime.fromtimestamp(lastHeard_ts)
            first_seen_dt = datetime.fromisoformat(firstSeen_iso)

            hours_ago = int((datetime.utcnow() - last_heard_dt).total_seconds() // 3600)
            first_hours_ago = int((datetime.utcnow() - first_seen_dt).total_seconds() // 3600)

            fmt = lambda v: f"{v:.2f}" if v is not None else "0.00"

            message = (
                f"**Node Details for '{nid}' ({longName or 'Unknown'})**\n"
                f"MAC: `{macaddr}`\n"
                f"Hops: `{hops}`\n"
                f"SNR: `{fmt(snr)}`\n"
                f"AirTime: `{fmt(airtime)}`\n"
                f"Role: `{role or 'unknown'}`\n"
                f"Last Seen: `{last_heard_dt.strftime('%Y-%m-%d %H:%M:%S UTC')} ({hours_ago} hours ago)`\n"
                f"First Seen: `{first_seen_dt.strftime('%Y-%m-%d %H:%M:%S UTC')} ({first_hours_ago} hours ago)`"
            )

            await output_channel.send(message)

            filename, error_message = generate_nodedata_graph(nid, macaddr, key.value)
            if filename:
                try:
                    graph_file = discord.File(filename)
                    await output_channel.send(file=graph_file)
                finally:
                    os.remove(filename)
            else:
                await output_channel.send(f"Could not generate graph: {error_message}")

        except sqlite3.Error as e:
            await output_channel.send(f"Failed to query database: {e}")
        except Exception as e:
            await output_channel.send(f"An unexpected error occurred: {e}")
        finally:
            if conn:
                conn.close()



    @bot.tree.command(name="newnodes", description="Show only new nodes seen in the last N hours", guild=guild_obj)
    async def newnodes(interaction: discord.Interaction, hours: int):
        # deduplicate results by hw addr 
        # Shows nodes whose firstSeen timestamp is within the last 'hours' number of hours,

        output_channel = discord.utils.get(interaction.guild.text_channels, name=botspam_output_channel)
        if output_channel is None:
            await interaction.response.send_message("Output channel #nodes-info not found.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Processing new nodes seen in the last {hours} hours, output will go to #nodes-info",
            ephemeral=True
        )

        if hours <= 0:
            await output_channel.send("Please provide a positive number of hours.")
            return

        conn = None
        try:
            # cutoff timestamp
            cutoff_dt = datetime.utcnow() - timedelta(hours=hours)
            cutoff_ts = int(cutoff_dt.timestamp())

            conn, cursor = get_cache_cursor()


            # sort by firstSeen ascending-order
            cursor.execute("""
                SELECT macaddr, id, longName, hopsAway, snr, airUtilTx, role, lastHeard, firstSeen
                FROM nodes
                ORDER BY firstSeen DESC
            """)
            nodes_raw = cursor.fetchall()

            if not nodes_raw:
                await output_channel.send(f"No nodes in the database.")
                return

            seen_hwaddrs = set()
            # process rows and skip anything with a missing hwaddr or macaddr to avoid an error
            new_nodes = []
            for row in nodes_raw:
                macaddr, nid, longName, hops, snr, airtime, role, lastHeard_ts, firstSeen_iso = row
                if not macaddr:
                    continue  # skip if no hardware addr

                first_seen_dt = datetime.fromisoformat(firstSeen_iso)
                first_seen_ts = int(first_seen_dt.timestamp())
                if first_seen_ts >= cutoff_ts:
                    new_nodes.append(row)


            if not new_nodes:
                await output_channel.send(f"No new nodes seen in the last {hours} hours.")
                return

            header = f"New nodes seen in the last {hours} hours (deduped by hwaddr):\n"
            fmt = lambda v: f"{v:.2f}" if v is not None else "0.00"
            lines = []

            for row in new_nodes:
                macaddr, nid, longName, hops, snr, airtime, role, lastHeard_ts, firstSeen_iso = row
                last_heard_dt = datetime.fromtimestamp(lastHeard_ts)
                first_seen_dt = datetime.fromisoformat(firstSeen_iso)

                hours_ago = int((datetime.utcnow() - last_heard_dt).total_seconds() // 3600)
                first_hours_ago = int((datetime.utcnow() - first_seen_dt).total_seconds() // 3600)

                lines.append(
                    f"{nid} ({longName or 'Unknown'}) [MAC: {macaddr}] - hops: {hops}, "
                    f"SNR: {fmt(snr)}, AirTime: {fmt(airtime)}, role: {role or 'unknown'}\n"
                    f"`  Last Seen: {last_heard_dt.strftime('%Y-%m-%d %H:%M:%S UTC')} ({hours_ago} hours ago)` \n"
                    f"`  First Seen: {first_seen_dt.strftime('%Y-%m-%d %H:%M:%S UTC')} ({first_hours_ago} hours ago)` \n"
                )

            # send discord-safe chunks
            current_chunk = header
            for line in lines:
                if len(current_chunk) + len(line) + 1 > 2000:
                    await output_channel.send(current_chunk)
                    current_chunk = line + "\n"
                else:
                    current_chunk += line + "\n"
            if current_chunk:
                await output_channel.send(current_chunk)

        except sqlite3.Error as e:
            await output_channel.send(f"Failed to query database: {e}")
        except Exception as e:
            await output_channel.send(f"An unexpected error occurred: {e}")
        finally:
            if conn:
                conn.close()




@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if isinstance(message.channel, discord.DMChannel):
        # message was sent to the bot in a DM
        print(f"Received DM from {message.author.name}: {message.content}")
        await message.channel.send("Hello! I received your message in a DM.")
    
    await bot.process_commands(message)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}!")
    await register_commands()
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID)) # sync the slash commands
    print("Commands synced!")
    

# ====== START MESH SESSION ======
MESH_SESSION = MeshSession(host)


# Run Discord bot
bot.run(TOKEN)