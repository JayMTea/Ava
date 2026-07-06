"""
Log management module for Ava.
Provides read access to systemd journals and application logs.
"""

import subprocess
import json
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
import logging

from . import config

logger = logging.getLogger(__name__)


class LogManager:
    """Manage reading logs from systemd and application sources."""

    VALID_SERVICES = [
        'ava-bridge',
        'ava-gpusvc',
        'vllm',
        'ava-snapshot',
        'ava-learning-digest',
        'ava-learning-weekly',
        'ava-arch-sync'
    ]

    VALID_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']

    APP_LOG_PATHS = {
        'bridge': Path(config.LOGS_DIR) / 'bridge.log',
        'gpusvc': Path(config.ROOT) / 'gpusvc' / 'logs',
        'learning': Path(config.LOGS_DIR) / 'learning.log',
    }

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

        try:
            cmd = [
                'journalctl',
                '--user-unit', f'{service}.service',
                f'--since={since}',
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
        except Exception as e:
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
            component: 'bridge', 'gpusvc', or 'learning'
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

        except Exception as e:
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
        if not service:
            return {'ok': False, 'error': 'service required for systemd source'}
        return LogManager.read_journalctl(service, lines, level, since)

    elif source == 'app':
        if not component:
            return {'ok': False, 'error': 'component required for app source'}
        return LogManager.read_app_logs(component, lines, level)

    else:
        return {'ok': False, 'error': 'Invalid source. Valid: systemd, app'}
