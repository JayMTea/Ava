#!/usr/bin/env python3
"""Bridge a USB Arduino to Ava — both directions, ~15 lines of real logic.

The board runs the AvaClient library's handleStream(Serial) + emitReading().
This host process:
  * PULL  — exposes GET /tools, POST /call for Ava, relaying to the board.
  * PUSH  — forwards the board's readings/events to Ava.

Point your device connector's `actions.discover.base` at this process's port,
and get the push token with `ava device token <id>`.

    export AVA_URL=http://localhost:8096
    export AVA_CID=greenhouse
    export AVA_TOKEN="$(ava device token greenhouse)"
    export SERIAL_PORT=/dev/ttyACM0        # whatever YOUR board enumerates as
    python serial_bridge.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ava_device import AvaDevice, SerialRelay, Tool, serve_pull

AVA_URL = os.environ.get("AVA_URL", "http://localhost:8096")
AVA_CID = os.environ.get("AVA_CID", "greenhouse")
AVA_TOKEN = os.environ.get("AVA_TOKEN", "")
SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/ttyACM0")
BAUD = int(os.environ.get("SERIAL_BAUD", "115200"))
PORT = int(os.environ.get("PULL_PORT", "9001"))

ava = AvaDevice(AVA_URL, AVA_CID, AVA_TOKEN)
relay = SerialRelay(SERIAL_PORT, BAUD)

# Build PULL tools from whatever the board advertises over serial.
tools = [
    Tool(t["name"], t.get("description", ""),
         run=(lambda args, _n=t["name"]: relay.call(_n, args)),
         schema=t.get("inputSchema", {}).get("properties", {}))
    for t in relay.tools()
]

print(f"[serial_bridge] {SERIAL_PORT}@{BAUD} <-> {AVA_URL} (cid={AVA_CID})")
print(f"[serial_bridge] PULL facade on :{PORT}; {len(tools)} tool(s) from board")
serve_pull(tools, port=PORT, block=False)   # PULL in the background
relay.pump(ava, block=True)                  # PUSH forwarding in the foreground
