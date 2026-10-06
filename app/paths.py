"""Source locations shared by the app and its installed agent integration.

Instance data is resolved by app.backend.settings, independently of these paths.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_ROOT = ROOT / "agent-platform"
INTEGRATION_ROOT = PLATFORM_ROOT / "integrations" / "ava"
