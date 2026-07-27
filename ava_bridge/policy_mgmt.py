"""
Policy management module for Ava.
Provides read/write access to egress policies.
"""

import re
import yaml
from pathlib import Path
from typing import Dict, Any, List
from datetime import datetime
import logging

from . import config

logger = logging.getLogger(__name__)


class PolicyManager:
    """Manage reading and updating egress policies."""

    POLICIES_DIR = Path(config.ROOT) / 'agent' / 'policies'

    # Policies that cannot be modified
    PROTECTED_POLICIES = [
        'ava-knowledge',  # Core knowledge access + GPU workloads callback
        'ava-weather',    # Core weather
    ]

    # Core policies that shouldn't be deleted
    CORE_POLICIES = PROTECTED_POLICIES + ['ava-learning', 'ava-logs', 'ava-config', 'ava-policies']

    @staticmethod
    def _valid_name(name: str) -> bool:
        """A policy name maps directly to <name>.yaml in POLICIES_DIR, so reject
        anything with path separators or traversal that could escape it."""
        return bool(name) and re.fullmatch(r'[A-Za-z0-9_-]+', name) is not None

    @staticmethod
    def _normalize_name(name: str) -> str:
        """Canonical policy name: trimmed + lowercased, so ' Ava-Knowledge ' and
        'ava-knowledge' resolve to the same reserved/system policy."""
        return (name or '').strip().lower()

    @staticmethod
    def _reserved_name(name: str) -> bool:
        """True if a name collides with a core/system policy (which callers must
        not create or overwrite). Matches case/whitespace-insensitively."""
        norm = PolicyManager._normalize_name(name)
        return norm in {p.lower() for p in PolicyManager.CORE_POLICIES}

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

            policies = []
            for policy_file in sorted(PolicyManager.POLICIES_DIR.glob('*.yaml')):
                name = policy_file.stem
                policies.append({
                    'name': name,
                    'path': str(policy_file),
                    'protected': name in PolicyManager.PROTECTED_POLICIES,
                    'core': name in PolicyManager.CORE_POLICIES
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

    @staticmethod
    def update_policy(
        name: str,
        updates: Dict[str, Any],
        reason: str = 'No reason provided'
    ) -> Dict[str, Any]:
        """
        Update a policy (add/modify/remove rules).

        Args:
            name: Policy name
            updates: Dict with 'add' or 'remove' keys containing rules
            reason: Reason for change (audit trail)

        Returns:
            {'ok': bool, 'changed': bool, 'error': str?}
        """
        if not PolicyManager._valid_name(name):
            return {'ok': False, 'error': f'Invalid policy name: {name!r}'}

        if name in PolicyManager.PROTECTED_POLICIES:
            return {
                'ok': False,
                'error': f'Cannot modify protected policy: {name}. Use a different policy or create a new one.'
            }

        try:
            policy_file = PolicyManager.POLICIES_DIR / f'{name}.yaml'

            if not policy_file.exists():
                return {
                    'ok': False,
                    'error': f'Policy not found: {name}. Use create_policy() to make a new one.'
                }

            # Read current policy
            with open(policy_file, 'r') as f:
                policy = yaml.safe_load(f) or {}

            # Create backup
            backup_path = policy_file.with_suffix(policy_file.suffix + '.bak')
            with open(policy_file, 'r') as f:
                original = f.read()
            with open(backup_path, 'w') as f:
                f.write(original)

            # Log the change
            log_file = Path(config.LOGS_DIR) / 'policy_changes.log'
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, 'a') as f:
                f.write(f"\n[{datetime.now().isoformat()}] {name}: {reason}\n")
                f.write(f"  Changes: {updates}\n")

            # Apply updates
            if 'add' in updates:
                # Add new rules to policy
                if 'rules' not in policy:
                    policy['rules'] = []

                for rule in updates['add']:
                    if rule not in policy['rules']:
                        policy['rules'].append(rule)

            if 'remove' in updates:
                # Remove rules from policy
                if 'rules' in policy:
                    for rule in updates['remove']:
                        if rule in policy['rules']:
                            policy['rules'].remove(rule)

            # Validate policy structure
            if 'name' not in policy or 'rules' not in policy:
                # Restore from backup
                with open(backup_path, 'r') as f:
                    original = f.read()
                with open(policy_file, 'w') as f:
                    f.write(original)
                return {
                    'ok': False,
                    'error': 'Policy validation failed. Must have "name" and "rules" keys.'
                }

            # Write updated policy
            with open(policy_file, 'w') as f:
                yaml.dump(policy, f, default_flow_style=False, sort_keys=False)

            return {
                'ok': True,
                'name': name,
                'changed': True,
                'reason': reason,
                'backup': str(backup_path),
                'new_rule_count': len(policy.get('rules', []))
            }

        except yaml.YAMLError as e:
            return {'ok': False, 'error': f'YAML error: {str(e)}'}
        except Exception as e:  # noqa: BLE001 — surfaced to the caller as {'ok': False, 'error': …}
            return {'ok': False, 'error': str(e)}

    @staticmethod
    def create_policy(
        name: str,
        rules: List[Dict[str, Any]],
        description: str = ''
    ) -> Dict[str, Any]:
        """
        Create a new policy.

        Args:
            name: Policy name (will be used as filename)
            rules: List of rule dicts
            description: Human-readable description

        Returns:
            {'ok': bool, 'path': str, 'error': str?}
        """
        try:
            policy_file = PolicyManager.POLICIES_DIR / f'{name}.yaml'

            if policy_file.exists():
                return {
                    'ok': False,
                    'error': f'Policy already exists: {name}. Use update_policy() to modify.'
                }

            # Build policy dict
            policy = {
                'name': name,
                'description': description,
                'rules': rules
            }

            # Validate structure
            if not isinstance(rules, list):
                return {'ok': False, 'error': 'rules must be a list'}

            # Write policy
            PolicyManager.POLICIES_DIR.mkdir(parents=True, exist_ok=True)
            with open(policy_file, 'w') as f:
                yaml.dump(policy, f, default_flow_style=False, sort_keys=False)

            # Log creation
            log_file = Path(config.LOGS_DIR) / 'policy_changes.log'
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, 'a') as f:
                f.write(f"\n[{datetime.now().isoformat()}] CREATE POLICY: {name}\n")
                f.write(f"  Rules: {len(rules)}\n")

            return {
                'ok': True,
                'name': name,
                'path': str(policy_file),
                'rule_count': len(rules)
            }

        except Exception as e:  # noqa: BLE001 — surfaced to the caller as {'ok': False, 'error': …}
            return {'ok': False, 'error': str(e)}


def list_policies() -> Dict[str, Any]:
    """List all available policies."""
    return PolicyManager.list_policies()


def read_policy(name: str) -> Dict[str, Any]:
    """Read a specific policy."""
    return PolicyManager.read_policy(name)


def update_policy(name: str, updates: Dict[str, Any], reason: str = '') -> Dict[str, Any]:
    """Update a specific policy."""
    return PolicyManager.update_policy(name, updates, reason)


def create_policy(name: str, rules: List[Dict[str, Any]], description: str = '') -> Dict[str, Any]:
    """Create a new policy."""
    return PolicyManager.create_policy(name, rules, description)
