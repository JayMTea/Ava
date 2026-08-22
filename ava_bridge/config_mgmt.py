"""Read-only configuration access for Ava.

She can look at her own `.env` — allowlisted keys in full, protected keys
redacted, everything else hidden — and say what it holds when asked.

There is no write path, deliberately. This module used to have one, and its
`CONFIG_PATHS` reached `agent/persona.txt.tmpl` (the agent's own system prompt)
and `ava_learning_digest.py` (executable Python), writing both with no diff, no
commit and no review — while the code-change policy of the day specifically
placed the persona template behind owner approval. Two layers, opposite answers,
one asset. The write path went with self-editing; see
tests/test_security.py::SelfEditingIsRemovedTests.
"""

from pathlib import Path
from typing import Dict, Any
import logging

from . import config

logger = logging.getLogger(__name__)


class ConfigManager:
    """Manage reading and updating configuration files."""

    # Paths to configuration files
    CONFIG_PATHS = {
        'env': Path(config.ROOT) / '.env',
    }

    # Keys whose VALUE is never shown. (Nothing can be changed here at all any
    # more — the name is historical. `ANTHROPIC_API_KEY` stays on the list even
    # though Ava no longer uses one: an upgraded install may still have it in
    # .env, and a redaction rule that forgets retired secrets is not a rule.)
    PROTECTED_KEYS = [
        'ANTHROPIC_API_KEY',
        'AVA_SECRET',
        'AVA_PASSWORD',
        'INTERNAL_TOKEN',
        'OPENCLAW_API_KEY',
    ]

    # Keys whose value is safe to show in full on a read.
    ALLOWED_ENV_KEYS = [
        'AVA_PORT',
        'AVA_OC_INFERENCE',
        'AVA_OC_PROVIDERS',
        'AVA_SNAPSHOT_KEEP',
        'AVA_SESSION_TTL_DAYS',
        'AVA_COOKIE_SECURE',
        'SPEAKER_THRESHOLD',
        'LOG_LEVEL',
        'CORS_ORIGINS',
    ]

    # Substrings that mark a key as secret regardless of the explicit list.
    _SENSITIVE_PATTERNS = ('TOKEN', 'SECRET', 'PASSWORD', 'APIKEY', '_KEY', 'INTERNAL')

    @staticmethod
    def read_config(component: str) -> Dict[str, Any]:
        """
        Read a configuration file.

        Args:
            component: 'env' (the only readable component)

        Returns:
            {'ok': bool, 'config': content or dict, 'path': str, 'error': str?}
        """
        if component not in ConfigManager.CONFIG_PATHS:
            return {
                'ok': False,
                'error': f'Invalid component. Valid: {", ".join(ConfigManager.CONFIG_PATHS.keys())}'
            }

        path = ConfigManager.CONFIG_PATHS[component]

        try:
            if not path.exists():
                return {
                    'ok': False,
                    'error': f'Config file not found: {path}'
                }

            if component == 'env':
                # Allowlist read: only surface values for keys we explicitly allow
                # to be shown. Protected keys are redacted; anything else is hidden
                # so a secret we don't know about can't leak in full.
                allowed = set(ConfigManager.ALLOWED_ENV_KEYS)
                protected = set(ConfigManager.PROTECTED_KEYS)
                config = {}
                with open(path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        if '=' in line:
                            key, val = line.split('=', 1)
                            key = key.strip()
                            if key in protected:
                                config[key] = '***REDACTED***'
                            elif key in allowed:
                                config[key] = val
                            else:
                                config[key] = '***HIDDEN***'

                return {
                    'ok': True,
                    'component': component,
                    'config': config,
                    'path': str(path),
                    'note': 'Only allowlisted keys show values; others are redacted/hidden'
                }

        except Exception as e:  # noqa: BLE001 — surfaced to the caller as {'ok': False, 'error': …}
            return {'ok': False, 'error': str(e)}

    @staticmethod
    def list_keys(component: str) -> Dict[str, Any]:
        """
        List available configuration keys for a component.

        Returns:
            {'ok': bool, 'keys': [...], 'protected': [...]}
        """
        if component == 'env':
            return {
                'ok': True,
                'component': component,
                'readable_keys': ConfigManager.ALLOWED_ENV_KEYS,
                'protected_keys': ConfigManager.PROTECTED_KEYS,
                'note': 'Read-only. Nothing here can be changed by the agent.'
            }

        return {
            'ok': False,
            'error': f'Invalid component. Valid: {", ".join(ConfigManager.CONFIG_PATHS.keys())}'
        }


def read_config(component: str) -> Dict[str, Any]:
    """Read a configuration component."""
    return ConfigManager.read_config(component)


def list_keys(component: str) -> Dict[str, Any]:
    """List available keys for a component."""
    return ConfigManager.list_keys(component)
