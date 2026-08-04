"""ava_bridge — the Experience-plane package behind phone_bridge.py.

phone_bridge.py is now a thin FastAPI app: it wires routes to the modules here.
Each module owns one concern so the bridge stays a face, not a monolith:

  config      shared paths / env knobs / constants (no state, no imports of siblings)
  state       shared mutable runtime state + locks (the single source of truth)
  auth        password gate, signed-cookie sessions, login throttle, middleware
  agent       Ava-the-agent: route a turn through the OpenClaw agent (MCP host)
  chat_store  per-chat conversation persistence (data/chats.json)
  turns       live chain-of-thought turns (poll the agent session jsonl)
  documents   uploaded-file text extraction (pdf / office / OCR) + message augment
  audio       browser-audio decode + Piper TTS

Anything a *model* can do flows through the MCP bus (the agent), never straight
from a route to a backend; routes only authenticate, serve, capture input, and
forward to the agent.
"""
