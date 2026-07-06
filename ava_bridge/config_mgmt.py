"""
Configuration management module for Ava.
Provides read/write access to .env, digest scripts, and persona config.
"""

import os
import re
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime
import logging

from . import config

logger = logging.getLogger(__name__)


class ConfigManager:
    """Manage reading and updating configuration files."""

    # Paths to configuration files
    CONFIG_PATHS = {
        'env': Path(config.ROOT) / '.env',
        'persona': Path(config.ROOT) / 'agent' / 'persona.txt.tmpl',
        'learning': Path(config.ROOT) / 'ava_learning_digest.py',
    }

    # Protected keys that cannot be changed
    PROTECTED_KEYS = [
        'ANTHROPIC_API_KEY',
        'AVA_SECRET',
        'AVA_PASSWORD',
        'INTERNAL_TOKEN',
        'OPENCLAW_API_KEY',
    ]

    # Allowed settings that can be changed in env
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

    @staticmethod
    def read_config(component: str) -> Dict[str, Any]:
        """
        Read a configuration file.

        Args:
            component: 'env', 'persona', or 'learning'

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

            else:
                # Read file as text
                with open(path, 'r') as f:
                    content = f.read()

                return {
                    'ok': True,
                    'component': component,
                    'config': content,
                    'path': str(path)
                }

        except Exception as e:
            return {'ok': False, 'error': str(e)}

    @staticmethod
    def update_config(
        component: str,
        updates: Dict[str, Any],
        reason: str = 'No reason provided'
    ) -> Dict[str, Any]:
        """
        Update a configuration file.

        Args:
            component: 'env', 'persona', or 'learning'
            updates: Dict of key-value pairs to update
            reason: Reason for the change (audit trail)

        Returns:
            {'ok': bool, 'changed': int, 'error': str?, 'reason_logged': True}
        """
        if component not in ConfigManager.CONFIG_PATHS:
            return {
                'ok': False,
                'error': f'Invalid component. Valid: {", ".join(ConfigManager.CONFIG_PATHS.keys())}'
            }

        if component in ConfigManager.PROTECTED_KEYS:
            return {
                'ok': False,
                'error': f'Cannot update component: {component}'
            }

        # Security: Check for protected keys
        for key in updates.keys():
            if key in ConfigManager.PROTECTED_KEYS:
                return {
                    'ok': False,
                    'error': f'Cannot change protected key: {key}'
                }

        path = ConfigManager.CONFIG_PATHS[component]

        try:
            if not path.exists():
                return {'ok': False, 'error': f'Config file not found: {path}'}

            # Create backup
            backup_path = path.with_suffix(path.suffix + '.bak')
            with open(path, 'r') as f:
                original = f.read()
            with open(backup_path, 'w') as f:
                f.write(original)

            # Log the change
            log_file = Path(config.LOGS_DIR) / 'config_changes.log'
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, 'a') as f:
                f.write(f"\n[{datetime.now().isoformat()}] {component}: {reason}\n")
                for key, val in updates.items():
                    f.write(f"  {key}={val}\n")

            # Update the file
            if component == 'env':
                # Update .env file
                lines = []
                with open(path, 'r') as f:
                    lines = f.readlines()

                # Track which keys we've updated
                updated_keys = set()

                # Modify existing lines
                new_lines = []
                for line in lines:
                    if not line.strip() or line.startswith('#'):
                        new_lines.append(line)
                        continue

                    if '=' in line:
                        key = line.split('=', 1)[0].strip()
                        if key in updates:
                            new_lines.append(f'{key}={updates[key]}\n')
                            updated_keys.add(key)
                        else:
                            new_lines.append(line)
                    else:
                        new_lines.append(line)

                # Add new keys not yet in file
                for key, val in updates.items():
                    if key not in updated_keys:
                        new_lines.append(f'{key}={val}\n')
                        updated_keys.add(key)

                with open(path, 'w') as f:
                    f.writelines(new_lines)

                return {
                    'ok': True,
                    'component': component,
                    'changed': len(updated_keys),
                    'keys': list(updated_keys),
                    'reason': reason,
                    'backup': str(backup_path)
                }

            else:
                # Text file update (persona, learning)
                content = original

                # For persona: simple text replacement
                if component == 'persona':
                    for key, val in updates.items():
                        # Replace pattern like "name: Ava" with "name: {val}"
                        pattern = rf'({key}\s*[:=]\s*).*'
                        content = re.sub(pattern, rf'\1{val}', content)

                with open(path, 'w') as f:
                    f.write(content)

                return {
                    'ok': True,
                    'component': component,
                    'changed': len(updates),
                    'reason': reason,
                    'backup': str(backup_path)
                }

        except Exception as e:
            # Restore backup on error
            try:
                backup_path = path.with_suffix(path.suffix + '.bak')
                if backup_path.exists():
                    with open(backup_path, 'r') as f:
                        original = f.read()
                    with open(path, 'w') as f:
                        f.write(original)
            except:
                pass

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
                'allowed_keys': ConfigManager.ALLOWED_ENV_KEYS,
                'protected_keys': ConfigManager.PROTECTED_KEYS,
                'note': 'Only allowed_keys can be modified. Protected keys are read-only.'
            }

        else:
            return {
                'ok': True,
                'component': component,
                'note': f'Text file. Use read_config() to view current content.'
            }


def read_config(component: str) -> Dict[str, Any]:
    """Read a configuration component."""
    return ConfigManager.read_config(component)


def update_config(
    component: str,
    updates: Dict[str, Any],
    reason: str = 'No reason provided'
) -> Dict[str, Any]:
    """Update a configuration component."""
    return ConfigManager.update_config(component, updates, reason)


def list_keys(component: str) -> Dict[str, Any]:
    """List available keys for a component."""
    return ConfigManager.list_keys(component)
