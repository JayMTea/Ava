"""
Policy management module for Ava.
Provides read/write access to egress policies.
"""

import re
import yaml
from pathlib import Path
from typing import Dict, Any
import logging

from . import config

logger = logging.getLogger(__name__)


class PolicyManager:
    """Manage reading and updating egress policies."""

    POLICIES_DIR = Path(config.ROOT) / 'agent' / 'policies'

    # Policies that cannot be modified
    PROTECTED_POLICIES = [
        'ava-knowledge',  # Core knowledge access
        'ava-weather',    # Core weather
    ]

    # Core policies that shouldn't be deleted
    CORE_POLICIES = PROTECTED_POLICIES + ['ava-learning', 'ava-logs', 'ava-config', 'ava-policies']

    @staticmethod
    def _valid_name(name: str) -> bool:
        """A policy name maps directly to <name>.yaml in POLICIES_DIR, so reject
        anything with path separators or traversal that could escape it."""
        return bool(name) and re.fullmatch(r'[A-Za-z0-9_-]+', name) is not None

    # `_normalize_name` / `_reserved_name` lived here: a case-insensitive guard
    # against overwriting a core policy. They went with the write path they
    # guarded — nothing in production had ever called them, only a test, so they
    # were a rule with no enforcement point. `PROTECTED_POLICIES` and
    # `CORE_POLICIES` stay because the read routes still LABEL rows with them,
    # which is honest information for an agent explaining its own limits.

    @staticmethod
    def list_policies() -> Dict[str, Any]:
        """
        List all available policies.

        Returns:
            {'ok': bool, 'policies': [name1, name2, ...], 'count': int}
        """
        try:
            if not PolicyManager.POLICIES_DIR.exists():
                return {'ok': False, 'error': f'Policies directory not found: {PolicyManager.POLICIES_DIR}'}

            # Enumerate through policy_inventory so this list covers generated/
            # and the overlay. It used to glob one level, so the four connector
            # egress policies were absent from the only list the agent can see.
            #
            # `editable` is the load-bearing new field. The read/write paths below
            # resolve POLICIES_DIR/<name>.yaml, so a generated policy — named by
            # its preset (`ava-persona`), living at generated/persona.yaml — is not
            # reachable through them and never should be: it is regenerated from
            # connectors/<id>/connector.yaml, so an edit here would be silently
            # overwritten. Saying so beats listing an entry that 404s on read.
            from . import policy_inventory

            policies = []
            for pol in policy_inventory.inventory():
                declared = pol.source == 'declared'
                policies.append({
                    'name': pol.name,
                    'file_stem': pol.file_stem,
                    'source': pol.source,
                    'editable': declared,
                    'path': pol.path,
                    'sha256': pol.sha256,
                    'endpoints': pol.endpoints,
                    'rules': pol.rules,
                    'protected': declared and pol.name in PolicyManager.PROTECTED_POLICIES,
                    'core': declared and pol.name in PolicyManager.CORE_POLICIES,
                })

            return {
                'ok': True,
                'policies': policies,
                'count': len(policies),
                'protected': PolicyManager.PROTECTED_POLICIES
            }

        except Exception as e:  # noqa: BLE001 — surfaced to the caller as {'ok': False, 'error': …}
            return {'ok': False, 'error': str(e)}

    @staticmethod
    def read_policy(name: str) -> Dict[str, Any]:
        """
        Read a specific policy.

        Args:
            name: Policy name (without .yaml extension)

        Returns:
            {'ok': bool, 'policy': dict, 'path': str, 'error': str?}
        """
        if not PolicyManager._valid_name(name):
            return {'ok': False, 'error': f'Invalid policy name: {name!r}'}

        try:
            policy_file = PolicyManager.POLICIES_DIR / f'{name}.yaml'

            if not policy_file.exists():
                # `list_policies()` now covers generated/ and the overlay, so
                # "not found — check the list" would point at a list containing
                # the very name that just failed. Say which case this is instead.
                from . import policy_inventory
                other = next((p for p in policy_inventory.inventory()
                              if p.name == name and p.source != 'declared'), None)
                if other is not None:
                    return {
                        'ok': False,
                        'error': (f'{name} is {"an" if other.source[0] in "aeiou" else "a"} {other.source} policy at '
                                  f'{other.rel} and is not editable here'
                                  + (' — it is regenerated from its connector '
                                     'manifest, so an edit would be overwritten'
                                     if other.source == 'generated' else '')),
                        'source': other.source,
                        'path': other.path,
                    }
                return {
                    'ok': False,
                    'error': f'Policy not found: {name}. Use list_policies() to see available.'
                }

            with open(policy_file, 'r') as f:
                content = yaml.safe_load(f)

            return {
                'ok': True,
                'name': name,
                'policy': content,
                'path': str(policy_file),
                'protected': name in PolicyManager.PROTECTED_POLICIES,
                'core': name in PolicyManager.CORE_POLICIES
            }

        except yaml.YAMLError as e:
            return {'ok': False, 'error': f'YAML parse error: {str(e)}'}
        except Exception as e:  # noqa: BLE001 — surfaced to the caller as {'ok': False, 'error': …}
            return {'ok': False, 'error': str(e)}


def list_policies() -> Dict[str, Any]:
    """List all available policies."""
    return PolicyManager.list_policies()


def read_policy(name: str) -> Dict[str, Any]:
    """Read a specific policy."""
    return PolicyManager.read_policy(name)
