"""
Log management module for Ava.
Provides read access to systemd journals and application logs.
"""

import subprocess
import re
from pathlib import Path
from typing import Optional, Dict, Any
import logging

from . import config

logger = logging.getLogger(__name__)


class LogManager:
    """Manage reading logs from systemd and application sources."""

    VALID_SERVICES = [
        'ava-bridge',
        'ava-router',
        'vllm',
        'ava-snapshot',
        'ava-arch-sync'
    ]

    VALID_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']

    APP_LOG_PATHS = {
        'bridge': Path(config.LOGS_DIR) / 'bridge.log',
    }

    # Friendly short/component names -> the real systemd unit, so callers (and the
    # model) can say "bridge" or "router" instead of the exact "ava-bridge" unit.
    SERVICE_ALIASES = {
        'bridge': 'ava-bridge', 'ava': 'ava-bridge',
        'router': 'ava-router',
    }

    @staticmethod
    def _journalctl_since(since: str) -> str:
        """Convert the tool's compact window ('5m','1h','2d') to a form journalctl
        parses ('5 minutes ago'). today/yesterday/now pass through unchanged."""
        s = (since or '1h').strip()
        if s in ('today', 'yesterday', 'now'):
            return s
        m = re.fullmatch(r'(\d+)([smhdw])', s)
        if not m:
            return s
        n, unit = m.group(1), m.group(2)
        word = {'s': 'seconds', 'm': 'minutes', 'h': 'hours',
                'd': 'days', 'w': 'weeks'}[unit]
        return f'{n} {word} ago'

    @staticmethod
    def resolve_service(name: Optional[str]) -> Optional[str]:
        """Map an alias/short name ('bridge') or bare unit to a VALID_SERVICES id."""
        if not name:
            return None
        n = name.strip().removesuffix('.service')
        if n in LogManager.VALID_SERVICES:
            return n
        if n in LogManager.SERVICE_ALIASES:
            return LogManager.SERVICE_ALIASES[n]
        if f'ava-{n}' in LogManager.VALID_SERVICES:
            return f'ava-{n}'
        return n  # let read_journalctl reject it with the valid-list message

    @staticmethod
    def read_journalctl(
        service: str,
        lines: int = 50,
        level: Optional[str] = None,
        since: str = '1h'
    ) -> Dict[str, Any]:
        """
        Read systemd journal for a service.

        Args:
            service: Service name (must be in VALID_SERVICES)
            lines: Number of lines to return (1-500)
            level: Log level filter (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            since: Time range ('5m', '1h', '1d', '7d')

        Returns:
            {'ok': bool, 'logs': [line1, line2, ...], 'count': int, 'error': str?}
        """
        if service not in LogManager.VALID_SERVICES:
            return {
                'ok': False,
                'error': f'Invalid service. Valid: {", ".join(LogManager.VALID_SERVICES)}'
            }

        if not 1 <= lines <= 500:
            return {'ok': False, 'error': 'lines must be 1-500'}

        if level and level not in LogManager.VALID_LEVELS:
            return {
                'ok': False,
                'error': f'Invalid level. Valid: {", ".join(LogManager.VALID_LEVELS)}'
            }

        # Constrain `since` to simple relative/keyword forms so nothing unexpected
        # reaches journalctl's time parser.
        if not re.fullmatch(r'\d+[smhdw]|today|yesterday|now', since or ''):
            return {'ok': False,
                    'error': "Invalid since. Use e.g. '5m', '1h', '1d', '7d', 'today'."}

        # Map level to journalctl priority
        level_map = {
            'DEBUG': '7',
            'INFO': '6',
            'WARNING': '4',
            'ERROR': '3',
            'CRITICAL': '2'
        }

        # journalctl can't parse the compact "1h"/"5m" form the tool accepts
        # ("Failed to parse timestamp: 1h") — convert it to the "N unit ago" form
        # journalctl understands. Keyword forms (today/yesterday/now) pass through.
        since_arg = LogManager._journalctl_since(since)

        try:
            cmd = [
                'journalctl',
                '--user-unit', f'{service}.service',
                f'--since={since_arg}',
                '-n', str(lines),
                '--no-pager',
                '-o', 'short-iso'
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                return {'ok': False, 'error': f'journalctl error: {result.stderr}'}

            logs = result.stdout.strip().split('\n') if result.stdout.strip() else []

            # Filter by level if specified
            if level:
                priority_threshold = int(level_map[level])
                filtered = []
                for line in logs:
                    # journalctl -o short-iso includes level like "INFO" or "WARN"
                    if any(lvl in line for lvl in LogManager.VALID_LEVELS):
                        filtered.append(line)
                logs = filtered

            return {
                'ok': True,
                'service': service,
                'logs': logs,
                'count': len(logs),
                'level': level,
                'since': since
            }

        except subprocess.TimeoutExpired:
            return {'ok': False, 'error': 'journalctl timeout'}
        except Exception as e:  # noqa: BLE001 — surfaced to the caller as {'ok': False, 'error': …}
            return {'ok': False, 'error': str(e)}

    @staticmethod
    def read_app_logs(
        component: str,
        lines: int = 50,
        level: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Read application log files directly.

        Args:
            component: 'bridge' (the only app log)
            lines: Number of lines to return (1-500)
            level: Filter by level string in log (DEBUG, ERROR, WARNING, etc.)

        Returns:
            {'ok': bool, 'logs': [...], 'count': int, 'error': str?}
        """
        if component not in LogManager.APP_LOG_PATHS:
            return {
                'ok': False,
                'error': f'Invalid component. Valid: {", ".join(LogManager.APP_LOG_PATHS.keys())}'
            }

        if not 1 <= lines <= 500:
            return {'ok': False, 'error': 'lines must be 1-500'}

        try:
            path = LogManager.APP_LOG_PATHS[component]

            if path.is_file():
                with open(path, 'r') as f:
                    all_lines = f.readlines()

                # Get last N lines
                selected = all_lines[-lines:] if len(all_lines) > lines else all_lines

                # Filter by level if specified
                if level:
                    level_upper = level.upper()
                    selected = [
                        line for line in selected
                        if level_upper in line
                    ]

                result_logs = [line.rstrip('\n') for line in selected]

                return {
                    'ok': True,
                    'component': component,
                    'logs': result_logs,
                    'count': len(result_logs),
                    'level': level,
                    'path': str(path)
                }

            elif path.is_dir():
                # For directories, list recent log files
                log_files = sorted(path.glob('*.log'), key=lambda x: x.stat().st_mtime, reverse=True)
                if not log_files:
                    return {'ok': True, 'component': component, 'logs': [], 'count': 0, 'path': str(path)}

                # Read most recent file
                with open(log_files[0], 'r') as f:
                    all_lines = f.readlines()

                selected = all_lines[-lines:] if len(all_lines) > lines else all_lines

                if level:
                    level_upper = level.upper()
                    selected = [
                        line for line in selected
                        if level_upper in line
                    ]

                result_logs = [line.rstrip('\n') for line in selected]

                return {
                    'ok': True,
                    'component': component,
                    'logs': result_logs,
                    'count': len(result_logs),
                    'level': level,
                    'path': str(log_files[0])
                }

            else:
                return {'ok': False, 'error': f'Path not found: {path}'}

        except Exception as e:  # noqa: BLE001 — surfaced to the caller as {'ok': False, 'error': …}
            return {'ok': False, 'error': str(e)}

    @staticmethod
    def list_available() -> Dict[str, Any]:
        """List all available log sources."""
        return {
            'ok': True,
            'systemd_services': LogManager.VALID_SERVICES,
            'app_logs': list(LogManager.APP_LOG_PATHS.keys()),
            'supported_levels': LogManager.VALID_LEVELS,
            'supported_time_ranges': ['5m', '30m', '1h', '6h', '1d', '7d']
        }


def read_logs(
    source: str = 'systemd',
    service: Optional[str] = None,
    component: Optional[str] = None,
    lines: int = 50,
    level: Optional[str] = None,
    since: str = '1h'
) -> Dict[str, Any]:
    """
    Unified function to read logs.

    Args:
        source: 'systemd' or 'app'
        service: Service name (for systemd)
        component: Component name (for app logs)
        lines: Number of lines (1-500)
        level: Log level filter
        since: Time range (for systemd)

    Returns:
        Log data dict
    """
    if source == 'systemd':
        # Be forgiving about how the caller names the unit: accept an explicit
        # `service`, fall back to a `component` name, and if neither is given
        # default to Ava's own bridge (the usual intent of "check your logs").
        # Aliases/short names ("bridge", "router") resolve to the real unit.
        svc = LogManager.resolve_service(service or component) or 'ava-bridge'
        return LogManager.read_journalctl(svc, lines, level, since)

    elif source == 'app':
        if not component:
            return {'ok': False, 'error': 'component required for app source'}
        return LogManager.read_app_logs(component, lines, level)

    else:
        return {'ok': False, 'error': 'Invalid source. Valid: systemd, app'}
