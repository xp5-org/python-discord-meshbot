import sys
import os
from datetime import datetime
import threading
import time
from pubsub import pub
import curses
import os
import queue


# using local git branch instead of importing meshapi from pip
local_repo = r"/bigpool/data/code_projects/meshtastic_github/xp5fork_meshastic-python-library"
if os.path.isdir(local_repo):
    sys.path.insert(0, local_repo)

import meshtastic.tcp_interface
from meshtastic.tcp_interface import TCPInterface

#print("using the following meshtcp library path: ", meshtastic.tcp_interface.__file__)

# capture mesh-api logging and save to debuglog.txt
import logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    filename="debuglog.txt",
    filemode="a"
)
logging.info("\n\n========== CLIENT STARTING ==========\n\n")

host='127.0.0.1'



class MeshSession:
    def __init__(self, host, meshportnum, msgq=None):
        self.node_ip = host
        self.meshportnum = meshportnum
        self.node_list = []
        self.node_list_lock = threading.Lock()
        self.my_node_id = None
        self.interface = None
        self.online = False
        self.msgq = msgq
        self._stop_event = threading.Event()
        self._shutdown_requested = False
        # add pub subscribe for disconnect events
        pub.subscribe(self._set_tcpshutdown_disconnect, 'meshtastic.connection.lost')
        # thread for connection to mesh api
        self._conn_thread = threading.Thread(target=self._meshconnection_loop, daemon=True)
        self._conn_thread.start()



    def _meshconnection_loop(self):
        if not self.msgq:
            raise RuntimeError("msgq must be set before starting connection loop")
        last_attempt = 0
        cooldown = 5
        while not self._stop_event.is_set() and not self._shutdown_requested:
            try:
                now = time.time()
                if not self.online and (now - last_attempt) > cooldown:
                    self.msgq.put("[Mesh] debug1 begin connect")
                    self.connect_node()
                    last_attempt = now
                time.sleep(2)
            except Exception as e:
                logging.exception("Exception in _meshconnection_loop")
                if self.msgq:
                    self.msgq.put(f"[Mesh] Exception in connection loop: {e}")
                time.sleep(5)


    def connect_node(self):
        if self.interface and self.online:
            if hasattr(self, "msgq") and self.msgq:
                self.msgq.put("[Mesh] Already connected, skipping reconnect")
            return

        if self.interface and not self.online:
            try:
                self.interface.close()
                if hasattr(self, "msgq") and self.msgq:
                    self.msgq.put("[Mesh] Closed existing interface for reconnect")
            except Exception:
                if hasattr(self, "msgq") and self.msgq:
                    self.msgq.put("[Mesh] Exception closing existing interface")
            self.interface = None

        if hasattr(self, "msgq") and self.msgq:
            self.msgq.put("[Mesh] Sleeping 5 seconds before attempting connection")
        time.sleep(5)

        if self.node_ip.lower() == "serial":
            if hasattr(self, "msgq") and self.msgq:
                self.msgq.put("[Mesh] Connecting via serial interface...")
            try:
                self.interface = SerialInterface()
                pub.subscribe(self.on_receive, "meshtastic.receive")
                self.online = True
                if hasattr(self, "msgq") and self.msgq:
                    self.msgq.put("[Mesh] Serial connection established")
            except Exception as e:
                self.interface = None
                self.online = False
                if hasattr(self, "msgq") and self.msgq:
                    self.msgq.put(f"[Mesh] Failed to connect via serial: {e}")
            return

        if hasattr(self, "msgq") and self.msgq:
            self.msgq.put(f"[Mesh] Connecting to {self.node_ip} via TCP...")
        try:
            self.interface = TCPInterface(hostname=self.node_ip, portNumber=self.meshportnum)
            pub.subscribe(self.on_receive, "meshtastic.receive")
            self.online = True
            if hasattr(self, "msgq") and self.msgq:
                self.msgq.put("[Mesh] TCP connection established")
        except Exception as e:
            self.interface = None
            self.online = False
            if hasattr(self, "msgq") and self.msgq:
                self.msgq.put(f"[Mesh] Failed to connect via TCP: {e}")


    def _set_tcpshutdown_disconnect(self, interface=None):
        if self.interface:
            try:
                self.msgq.put("[Mesh] debug2 closing interface")
                self.interface.close()
            except Exception:
                return
                ##print("[mesh] Exception closing TCP interface")
        else:
            return
            ##print("[mesh] tcp shutdown called. No interface to close")

        self.interface = None
        self.online = False


    def fetch_nodes(self):
        devices = []
        for node_id, node in self.interface.nodes.items():
            user = node.get("user", {})
            metrics = node.get("deviceMetrics", {})

            long_name = user.get("longName", "Unknown")
            role = user.get("role", "none")
            hops = node.get("hopsAway", "?")
            snr = metrics.get("snr", None)

            if hops == "?":
                hops_val = 99
            else:
                try:
                    hops_val = int(hops)
                except (TypeError, ValueError):
                    hops_val = 99

            info = {
                "longName": long_name,
                "role": role,
                "hopsAway": hops_val,
                "snr": snr
            }
            devices.append((node_id, info))

        devices.sort(key=lambda item: item[1].get("hopsAway", 99))

        with self.node_list_lock:
            self.node_list[:] = devices
            self.my_node_id = next(
                (nid for nid, info in devices if info.get("role") == "self"),
                None
            )

        if hasattr(self, "nodelist_q") and self.nodelist_q is not None:
            self.nodelist_q.put("=== Node list refreshed ===")
            for node_id, info in devices:
                line = f"{node_id} ({info['longName']}) - hops: {info['hopsAway']}, SNR: {info['snr']}, role: {info['role']}"
                self.nodelist_q.put(line)


    def on_receive(self, packet, interface):
        try:
            decoded = packet.get('decoded', {})
            if decoded.get('portnum') != 'TEXT_MESSAGE_APP':
                return

            message = decoded['payload'].decode('utf-8')
            fromnum = packet['fromId']

            with self.node_list_lock:
                info = next((info for nid, info in self.node_list if nid == fromnum), {})
            shortname = info.get('longName', 'Unknown')
            hops = info.get('hopsAway', '?')

            to_id = packet.get('toId') or decoded.get('to')
            if to_id is None or to_id == '^all':
                prefix = "C]"
            elif to_id == self.my_node_id:
                prefix = "D]"
            else:
                prefix = "UNK]"

            line = "H%s %s %s: %s" % (hops, prefix, shortname, message)
            self.msgq.put(line)
        except:
            pass


    def listen_messages(self):
        stop_event = threading.Event()
        try:
            while not stop_event.is_set():
                stop_event.wait(1)
        except KeyboardInterrupt:
            if self.interface:
                self.interface.close()




def mainScreen(win):
    win.erase()
    y, x = win.getmaxyx()
    win.border(124,124,45,95)
    win.addstr(1, 40, "Mesh Terminal")
    win.addstr(2, 38, f"Screen size Y: {y} X: {x}")
    
    # draw borders for sub-windows
    win.hline(16, 1, '-', 96)  # input separator
    win.vline(1, 71, '|', 15)  # node list separator
    win.refresh()


class WindowManager:
    def __init__(self, win):
        self.win = win
        self.chatwin = win.subwin(15, 70, 1, 1)
        self.chatwin.scrollok(1)
        self.chatwin.idlok(1)

        self.listwin = win.subwin(15, 25, 1, 72)
        self.listwin.scrollok(1)
        self.listwin.idlok(1)

        max_y, max_x = win.getmaxyx()
        self.inputwin = win.subwin(3, max_x - 2, max_y - 4, 1) # text input box

        self.inputwin.scrollok(1)
        self.inputwin.idlok(1)
        self.inputwin.keypad(1)
        curses.curs_set(1)
        self.msgq = queue.Queue() # chat messages
        self.nodelist_q = queue.Queue() # node list messages
        self.node_list = []
        self.node_list_lock = threading.Lock()
        self.my_node_id = None

    def poll_nodelist(self):
        while True:
            try:
                line = self.nodelist_q.get_nowait()
            except queue.Empty:
                break
            else:
                self.listwin.addstr(line + "\n")
                self.listwin.refresh()


    def add_chat_line(self, line):
        self.chatwin.addstr(line + "\n")
        self.chatwin.refresh()

    def poll_chat(self):
        while True:
            try:
                line = self.msgq.get_nowait()
            except queue.Empty:
                break
            else:
                self.add_chat_line(line)

    def mainloop(self, session):
        mainScreen(self.win)
        input_buffer = ""
        self.inputwin.border()
        self.inputwin.refresh()
        self.inputwin.nodelay(1)

        while True:
            self.poll_chat()
            self.poll_nodelist()
            self.inputwin.move(1, 2)
            self.inputwin.clrtoeol()
            self.inputwin.addstr("> " + input_buffer)
            self.inputwin.refresh()
            self.chatwin.refresh()
            self.listwin.refresh()

            ch = self.inputwin.getch()
            if ch == -1:
                time.sleep(0.05)
                continue
            if ch in (curses.KEY_BACKSPACE, 127):
                input_buffer = input_buffer[:-1]
            elif ch in (curses.KEY_ENTER, 10, 13):
                if input_buffer.upper() == "X":
                    break
                elif input_buffer.startswith("]"):
                    session.fetch_nodes()
                elif input_buffer.strip():
                    if session.interface and session.online:
                        try:
                            session.interface.sendText(input_buffer.strip())
                            session.msgq.put(f"You: {input_buffer.strip()}")
                        except Exception as e:
                            session.msgq.put(f"[Error sending message: {e}]")
                    else:
                        session.msgq.put("[Not connected to mesh network]")
                input_buffer = ""
            else:
                try:
                    input_buffer += chr(ch)
                except:
                    pass






def main():
    import argparse
    import curses

    parser = argparse.ArgumentParser(description="meshtastic ncurses client")
    parser.add_argument("--host", default="127.0.0.1", help="IP address to connect to")
    parser.add_argument("--port", type=int, default=4003, help="TCP port to connect to (default: 4003)")
    args = parser.parse_args()

    stdscr = curses.initscr()
    curses.start_color()
    curses.use_default_colors()
    curses.curs_set(1)

    wm = WindowManager(stdscr)
    session = MeshSession(args.host, args.port, msgq=wm.msgq)
    session.nodelist_q = wm.nodelist_q

    listener_thread = threading.Thread(target=session.listen_messages, daemon=True)
    listener_thread.start()

    try:
        wm.mainloop(session)
    finally:
        curses.endwin()




if __name__ == "__main__":
    main()
