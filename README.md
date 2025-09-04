#  Meshtastic bot for Discord

## Overview

Its a python discord bot using the meshtastic API to read incoming messages from an ESP32 node and post them to a channel



<br>

This Python bot will:  
- Connect to an Meshtastic device via wifi in a background thread
- history log sqlite file updates every x minutes of all nodedb info
- node-cache in sqlite of most recent unique node entries sorted by mac-address  
- Connect to Discord and forward mesh messages to #mesh-incoming  
- Register slash commands:  
  - Show new nodes between X and Y period  
  - Show nodes summary sorted by hop count, airTxUtil, channelUtil, battery level, uptime  
  - Graph a simple Matplotlib diagram of hops (needs a lot of work to be useful)  

it needs lots of work but it does run 




## Setup

```
git clone https://github.com/xp5-org/python-discord-meshbot.git
cd python-discord-meshbot
vi .env
> DISCORD_TOKEN = "your discord bot token"
> CENTERLAT = lattitude for center of hop map generation
> CENTERLON = longitude for center of hop map generation

python3 -m venv myenv; source myenv/bin/activate
pip install -r requirements.txt
python3 discord_meshbot.py
```


## Discord commands

<img width="547" height="322" alt="Screenshot 2025-08-23 at 6 27 37 PM" src="https://github.com/user-attachments/assets/3e5c2557-b86a-4c1a-93f6-b6aea95bff1c" />

<img width="416" height="300" alt="Screenshot 2025-08-23 at 7 16 03 PM" src="https://github.com/user-attachments/assets/6763ddb1-76cf-4232-a3e2-9018b03c2c1a" />

<img width="554" height="110" alt="Screenshot 2025-08-23 at 7 16 34 PM" src="https://github.com/user-attachments/assets/89b3e3a3-7fef-46e3-85e2-eda6b0f8cfee" />

supports querying the node cache sqlite database and sort based on the input key, and option to ignore results older than x value 

