# QA shim: importing this raises, which phone_bridge._ensure_loaded() treats as
# "voice extras not installed" — the deterministic, hermetic voice-less path.
# Activated by prepending qa/_shims to PYTHONPATH (subprocess tiers); the
# in-process tier does the equivalent with sys.modules["faster_whisper"] = None.
raise ImportError("qa: voice disabled for hermetic tests")
