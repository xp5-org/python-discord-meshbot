import os, io, time, json, threading, asyncio, re, random, math
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks
from meshtastic.tcp_interface import TCPInterface
from pubsub import pub  # pip install pypubsub
from discord import app_commands
import matplotlib.pyplot as plt
import contextily as ctx
import geopandas as gpd
from shapely.geometry import Point
import sqlite3, aiosqlite
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
center_lat = os.getenv("CENTERLAT")
center_lon = os.getenv("CENTERLON")




# ====== CONFIG ======
host = '192.168.1.6'
NODE_CACHE_DB = "sqlite_cache.db"       # contains only current node list no duplicates
HISTORY_DB_FILE = "nodes_history.db"    # full history log will become large
INCOMING_CHANNEL_NAME = "mesh-incoming"   # channel name of where to post the messages
NODE_REFRESH_INTERVAL = 900             # num of seconds to refresh nodedb from device
permsinteger = "274877941760"           # discord bot pernissions for channel access
# ====================


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
                    cache_cursor = cache_conn.cursor()
                    cache_cursor.execute(f"CREATE TABLE IF NOT EXISTS nodes ({CACHE_COLUMNS})")
                    cache_conn.commit()
                    print("[mesh] Cache database ready.")

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

                time.sleep(1)

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
                # builds python object from node cache
                with self.node_list_lock:
                    cursor.execute("SELECT * FROM nodes")
                    rows = cursor.fetchall()
                    self.node_list = []

                    # map database rows/cols to python dict
                    for row in rows:
                        node_info = {
                            "id": row[0],
                            "longName": row[1],
                            "shortName": row[2],
                            "role": row[3],
                            "hopsAway": row[4],
                            "snr": row[5],
                            "batteryLevel": row[6],
                            "voltage": row[7],
                            "airUtilTx": row[8],
                            "channelUtilization": row[9],
                            "hardwareModel": row[10],
                            "position": {"latitude": row[11], "longitude": row[12]} if row[11] is not None else None,
                            "lastHeard": row[13],
                            "firstSeen": row[14]
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

        try:
            print(f"[mesh] Connecting to {self.node_ip}...")
            self.interface = TCPInterface(hostname=self.node_ip)
            pub.subscribe(self.on_receive, "meshtastic.receive")
            self.online = True
            print("[mesh] Connected to host")
        except Exception as e:
            self.interface = None
            self.online = False
            print(f"[mesh] Failed to connect: {e}")


    def fetch_nodes(self, cache_conn, cache_cursor, history_conn, history_cursor):
        if not self.online or not self.interface:
            raise RuntimeError("Not connected to mesh host")

        print("[mesh] Fetching and logging nodes from mesh...")
        current_time_iso = datetime.utcnow().isoformat()
        current_time_ts = int(datetime.utcnow().timestamp())

        try:
            for node_id, node in self.interface.nodes.items():
                user = node.get("user", {})
                metrics = node.get("deviceMetrics", {})
                pos = node.get("position", {})

                hops_raw = node.get("hopsAway")
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
                    # Node exists → update everything except firstSeen
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


    def on_receive(self, packet, interface):
        # capture incoming messages from mesh device and forward to discord
        if not self.online:
            return
        try:
            decoded = packet.get("decoded") or {}
            if decoded.get("portnum") == "TEXT_MESSAGE_APP":
                payload = decoded.get("payload", b"")
                message = payload.decode("utf-8", errors="ignore")
                fromnum = packet.get("fromId")
                with self.node_list_lock:
                    info = next((info for nid, info in self.node_list if nid == fromnum), {})
                shortname = info.get("longName", "Unknown")
                hops = info.get("hopsAway", "?")
                to_id = packet.get("toId") or decoded.get("to")
                prefix = "C]" if to_id is None or to_id == "^all" else "D]" if to_id == self.my_node_id else "UNK]"
                if prefix in ("C]", "D]"):
                    asyncio.run_coroutine_threadsafe(
                        send_to_discord(f"H{hops} {prefix} {shortname}: {message}"),
                        bot.loop
                    )
        except Exception as e:
            print(f"[mesh] on_receive exception: {e}")


    def stop(self):
        """Stop the background loop and clean up interface."""
        self._stop_event.set()
        if self.interface:
            try:
                self.interface.close()
            except Exception:
                pass
        self.online = False





# helper: convert lat/lon to flat km offsets
def latlon_to_km(lat, lon, center_lat, center_lon):
    if lat is None or lon is None:
        return None, None
    delta_lat = lat - center_lat
    delta_lon = lon - center_lon
    y = delta_lat * 111
    x = delta_lon * 111 * math.cos(math.radians(center_lat))
    return x, y


async def generate_hopmap_png(max_hops: int, filename: str):
    if not os.path.exists(NODE_CACHE_DB):
        raise RuntimeError("No cache file found")

    nodes = {}
    async with aiosqlite.connect(NODE_CACHE_DB) as db:
        async with db.execute(
            "SELECT id, latitude, longitude, hopsAway FROM nodes WHERE hopsAway <= ?", 
            (max_hops,)
        ) as cursor:
            async for row in cursor:
                nid, lat, lon, hops = row
                if hops not in nodes:
                    nodes[hops] = []
                nodes[hops].append({"_id": nid, "position": {"latitude": lat, "longitude": lon}})

    randomgrouping = 1 + (max_hops * 3)
    randomvalidgrouping = 1

    plt.figure(figsize=(8, 8))
    for hop_count, node_list in nodes.items():
        valid_positions = []
        for n in node_list:
            pos = n.get("position") or {}
            lat = pos.get("latitude")
            lon = pos.get("longitude")
            x, y = latlon_to_km(lat, lon, center_lat, center_lon)
            if x is not None and y is not None and math.sqrt(x ** 2 + y ** 2) <= 200:
                valid_positions.append((x, y))

        for n in node_list:
            pos = n.get("position") or {}
            lat = pos.get("latitude")
            lon = pos.get("longitude")
            node_id = n.get("_id", "unknown")

            x, y = latlon_to_km(lat, lon, center_lat, center_lon)
            if x is not None and y is not None and math.sqrt(x ** 2 + y ** 2) <= 200:
                plt.scatter(x, y, color="green", s=50)
                plt.annotate(node_id, (x, y), xytext=(5, 5), textcoords="offset points",
                             fontsize=8, color="green")
            else:
                if valid_positions:
                    base_x, base_y = random.choice(valid_positions)
                    x = base_x + random.uniform(-randomvalidgrouping, randomvalidgrouping)
                    y = base_y + random.uniform(-randomvalidgrouping, randomvalidgrouping)
                else:
                    x = random.uniform(-randomgrouping, randomgrouping)
                    y = random.uniform(-randomgrouping, randomgrouping)
                plt.scatter(x, y, color="red", s=50)
                plt.annotate(node_id, (x, y), xytext=(5, 5), textcoords="offset points",
                             fontsize=8, color="red")

    plt.xlim(-50, 50)
    plt.ylim(-50, 50)
    plt.xlabel("East (km)")
    plt.ylabel("North (km)")
    total_nodes = sum(len(lst) for lst in nodes.values())
    plt.title(f"num nodes {total_nodes} : num hops {max_hops}")
    plt.grid(True)
    plt.axis("equal")
    plt.tight_layout()

    # save local PNG for debugging
    plt.savefig(filename, dpi=300, bbox_inches="tight")

    # return the in memory copy for discord to post to channel
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






    @bot.tree.command(name="nodes_local", description="Show cached local node info", guild=guild_obj)
    async def nodes_local(interaction: discord.Interaction, limit: int = 3):
        # get the dedicated output channel
        output_channel = discord.utils.get(interaction.guild.text_channels, name="nodes-info")
        if output_channel is None:
            await interaction.response.send_message("Output channel #nodes-info not found.", ephemeral=True)
            return

        # ack command in private to avoid spam
        await interaction.response.send_message("Processing nodes, output will go to #nodes-info", ephemeral=True)

        conn = None
        try:
            conn = sqlite3.connect(NODE_CACHE_DB)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, longName, hopsAway, snr, airUtilTx, role, lastHeard, firstSeen
                FROM nodes
                WHERE hopsAway <= ?
                ORDER BY hopsAway ASC
            """, (limit,))
            
            nodes_raw = cursor.fetchall()

            if not nodes_raw:
                await output_channel.send(f"No nodes with hops ≤ {limit} in the database.")
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




    @bot.tree.command(name="nodedata", description="Show data from recently heard nodes", guild=guild_obj)
    @app_commands.choices(
        key=[
            app_commands.Choice(name="Battery Level", value="batteryLevel"),
            app_commands.Choice(name="Air Utilization TX", value="airUtilTx"),
            app_commands.Choice(name="Channel Utilization", value="channelUtilization"),
            app_commands.Choice(name="Uptime Seconds", value="uptimeSeconds"),
        ]
    )
    async def nodedata(
        interaction: discord.Interaction,
        key: app_commands.Choice[str],
        hop_limit: int = 3,
        hours: int = 2
    ):
        output_channel = discord.utils.get(interaction.guild.text_channels, name="nodes-info")
        if output_channel is None:
            await interaction.response.send_message("Output channel #nodes-info not found.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Processing nodes for the last {hours} hours, output will go to #nodes-info",
            ephemeral=True
        )

        if key.value not in {"batteryLevel", "airUtilTx", "channelUtilization", "uptimeSeconds"}:
            await output_channel.send(
                f"Invalid key '{key.value}', must be one of batteryLevel, airUtilTx, channelUtilization, uptimeSeconds."
            )
            return


        cutoff_ts = int((datetime.utcnow() - timedelta(hours=hours)).timestamp())

        conn = None
        try:
            conn = sqlite3.connect(NODE_CACHE_DB)
            cursor = conn.cursor()

            # get node data from cache db
            cursor.execute(f"""
                SELECT id, longName, hopsAway, {key.value}
                FROM nodes
                WHERE lastHeard >= ? AND hopsAway <= ?
                ORDER BY {key.value} DESC
            """, (cutoff_ts, hop_limit))

            rows = cursor.fetchall()

            if not rows:
                await output_channel.send(
                    f"No nodes seen in the last {hours} hours within hop limit {hop_limit}."
                )
                return


            # group 1st pass of sorting by hop count
            hop_groups = {}
            for nid, longName, hops, value in rows:
                if hops not in hop_groups:
                    hop_groups[hops] = []
                hop_groups[hops].append((nid, longName, value))

            lines = [f"Nodes sorted by {key.value} (last {hours} hours, hop limit {hop_limit}):\n"]

            for hop in sorted(hop_groups.keys()):
                lines.append(f"Hop-{hop}:")
                # after sorting by hopcount, sort inner results by low-to-hgh
                group = sorted(
                    hop_groups[hop],
                    key=lambda x: (x[2] is None, x[2] if x[2] is not None else float("inf"))
                )
                for nid, longName, value in group:
                    val_str = f"{value:.2f}" if value is not None else "0.00"
                    lines.append(f"{nid} ({longName or 'Unknown'}) - {val_str}")
                lines.append("")  # blank line between hop groups

            chunk = ""
            for line in lines:
                if len(chunk) + len(line) + 1 > 2000:
                    await output_channel.send(chunk)
                    chunk = line + "\n"
                else:
                    chunk += line + "\n"
            if chunk:
                await output_channel.send(chunk)

        except sqlite3.Error as e:
            await output_channel.send(f"Failed to query database: {e}")
        finally:
            if conn:
                conn.close()
    

    @bot.tree.command(name="status", description="check bot and Heltec node status", guild=guild_obj)
    async def status(interaction: discord.Interaction):
        await interaction.response.defer()  # times out without this i guess

        count = 3
        timeout_ms = 150
        successful_pings = []

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

                # time=XXX ms
                match = re.search(r"time[=<]([0-9\.]+)", output)
                if match:
                    successful_pings.append(float(match.group(1)))
            except asyncio.TimeoutError:
                pass  # failed ping

        if successful_pings:
            avg_ping = sum(successful_pings) / len(successful_pings)
            msg = f"Bot online : Heltec online, ping avg {avg_ping:.2f} ms"
        else:
            msg = "Bot online : Heltec offline (no response)"

        await interaction.followup.send(msg)



    @bot.tree.command(name="newnodes", description="Show only new nodes seen in the last N hours", guild=guild_obj)
    async def newnodes(interaction: discord.Interaction, hours: int):
        # deduplicate results by hw addr 
        # Shows nodes whose firstSeen timestamp is within the last 'hours' number of hours,

        output_channel = discord.utils.get(interaction.guild.text_channels, name="nodes-info")
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

            conn = sqlite3.connect(NODE_CACHE_DB)
            cursor = conn.cursor()

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
                    f"  Last Seen: {last_heard_dt.strftime('%Y-%m-%d %H:%M:%S UTC')} ({hours_ago} hours ago)\n"
                    f"  First Seen: {first_seen_dt.strftime('%Y-%m-%d %H:%M:%S UTC')} ({first_hours_ago} hours ago)"
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
async def on_ready():
    print(f"Logged in as {bot.user}!")
    await register_commands()
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID)) # sync the slash commands
    print("Commands synced!")

# ====== START MESH SESSION ======
MESH_SESSION = MeshSession(host)


# Run Discord bot
bot.run(TOKEN)
