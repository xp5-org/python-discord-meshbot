#  Meshtastic bot for Discord + Flask OSM Tile Server

## Overview

Its a python discord bot using the meshtastic API to read incoming messages from a wifi node or usb-serial, and post them to a channel

it saves everything it ever sees in a node_history.db (sqlite) , and keeps a cached copy of current node info in node_cache.db. 
- This could cause a lot of writes on something like a raspberry pi and be an unneeded source of SD card wear



<br>

This Python bot will:  
- Connect to an Meshtastic device via wifi (or serial) in a background thread
- history log sqlite file updates every x minutes of all nodedb info
- node-cache in sqlite of most recent unique node entries sorted by mac-address  
- Connect to Discord and forward mesh messages to #mesh-incoming  
- Register slash commands:  
  - Show new nodes since x many hours ago
  - Show nodedb summary, limit by hop count  
  - Graph a simple diagram of hops (needs a lot of work to be useful)
  - Graph node data such as Airtxutil or channel-busy % for nodes seen in the db



Things it should have but doesnt:
- some logging visible to the admin via discord, record events like disconnects or python errors. currently need to check on it manually from the terminal where it was run from
- better status function, current one pings the node-ip and
- recovery from wifi packet loss. If your wifi drops out for a moment when you are receiving a message, its lost. This is how meshtastic works and nothing to do with this bot. Meshtastic API does not have any store or retry mechanism so if you dont catch the messages for whatever reason they are lost.

Known problems:
- meshtastic has an outstanding bug which can affect any use of python in receiving tcp events
- https://github.com/meshtastic/firmware/issues/5764
- summary of this is - the meshbot (or any other python script left running) will disconnect and fail to receive data. the error takes place in the meshtastic API and the error message is not passed on down to the listener

<br>

<br>



## Setup

```
git clone https://github.com/xp5-org/python-discord-meshbot.git
cd python-discord-meshbot
vi .env
```

contents of env file (example)
```
DISCORD_TOKEN = "your discord bot token"
CENTER_LAT = lattitude of your node
CENTER_LON = longitude of your node
MESHNODE_IP = 127.0.0.1
NODE_CACHE_DB = sqlite_cache.db
HISTORY_DB_FILE = nodes_history.db
AUTHORIZED_SEND_USERS = numeric ID of the discord user, comma separated if you want multiple users authed
MESHPORTNUM = 4403
INCOMING_CHANNEL_NAME = mesh-incoming
BOTSPAM_OUTPUT_CHANNELNAME = nodes-info
MY_NODE_ID = !1123abc987 
DISCORD_BOT_PERMS = 274877941760
```

Using serial? set the following 
```
MESHNODE_IP = serial
```


install the pip requirements and start it in screen
```
python3 -m venv myenv; source myenv/bin/activate
pip install -r requirements.txt
screen
python3 discord_meshbot.py
```
to exit screen and leave it running press control-a , then the d key. 
<br>

<br>


# Discord commands


<img width="694" height="376" alt="image" src="https://github.com/user-attachments/assets/a0999b6b-adc8-4ab8-a887-65f26ad9fae8" />


channel setup:

its set up like this

<img width="320" height="380" alt="image" src="https://github.com/user-attachments/assets/1598aff4-f18d-4c62-a427-0f2b921b8ec5" />

<br>

<br>




# using the commands
This has the following:
- hopmap
-- plot a simple map of where the nodes are (needs a lot of work and polish)
- newnodes
- nodedatagraph
- nodes_local
- send
- status
<br>

<br>

## nodedatagraph

this can plot a graph of the data being collected in the node_history.db , its useful to get a history of channel and tx utilisation 

<img width="534" height="292" alt="image" src="https://github.com/user-attachments/assets/99f3a7c6-d075-4c6a-b9a6-e432f2e551a5" />

<img width="715" height="558" alt="image" src="https://github.com/user-attachments/assets/f4389ea2-d2e9-4ab9-96e4-ce2df1a0ea78" />

<br>

<br>


## newnodes
scan over the node history DB and print which nodes are newly seen since x many hours ago

<img width="293" height="100" alt="image" src="https://github.com/user-attachments/assets/eb84ed67-9398-435f-8c7c-b643c64e5005" />


<br>


<img width="703" height="371" alt="image" src="https://github.com/user-attachments/assets/3c506928-67ed-4031-919b-550bf03c0dbb" />



<br>

<br>


## nodes_local

shows info from the node cache (not the history db) limited by hopcount , useful if you want to see hops0-2 or 0-3 for example. 
message is broken up into multiple chunks and can be large if the nodedb has many local clients

<img width="319" height="101" alt="image" src="https://github.com/user-attachments/assets/d02143af-c3b6-436b-8f45-0dbb1e05d8f4" />

<img width="713" height="246" alt="image" src="https://github.com/user-attachments/assets/43ce0be4-a615-4118-bd03-bffcfd6e9d80" />

<br>

<br>

## send

sends a message from discord only if the user interacting with the command is on the authorised list in the .env file

<img width="407" height="112" alt="image" src="https://github.com/user-attachments/assets/ce730ca1-34fe-4748-855e-1033fb4342c9" />

the message is mirrored in the mesh-incoming channel 

<img width="389" height="150" alt="image" src="https://github.com/user-attachments/assets/f84f1ba4-4f14-49f2-ac38-832f39642774" />


# Flask OSM Tile Server

I set up an openstreetmap tile server and ran a tool to convert the tiles to .png . the northwest USA region of WA/ID/OR occupies roughly 350MB space at zoom-level 12 which is zoomed in enough to identify the basic structure of city streets and individual city blocks. zoom levels less than this will take up signficantly less space. 

tile server demo: ```mytilemap.py```

convert node cache DB to json: ```nodecache2json.py```

this is taking the "node cache database" from the discord meshbot and plotting green dots for nodes it has received Lat/Lon GPS telemetry data into the meshtastic nodeDb. Nodes which have not reported telemetry are red-dots, and are lumped together to the nearest-neighbor with valid lat/lon GPS location organized by hop count.

this is a simple demo of how to take a list of node IDs, Lat/Lon gps info mixed in with no-gps-telemetry nodes, and plot them on a live map

<br>

<img width="790" height="660" alt="image" src="https://github.com/user-attachments/assets/197f5633-f027-420d-ab00-27e9ba76fd59" />

