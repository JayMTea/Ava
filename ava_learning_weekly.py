#!/usr/bin/env python3
"""
Ava Weekly Learning Trends Analysis (Phase 5 Extension)
Analyzes learning cycles from the past 7 days and detects patterns/trends.
Sends a summary email on your deployment's schedule (e.g. weekly via
systemd timer or cron).
"""

import sys
import os
import logging
from pathlib import Path
from datetime import datetime, timedelta
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from collections import Counter

HERE = Path(__file__).resolve().parent

# Importing settings auto-loads .env (repo root + $AVA_HOME) into the
# environment; values already set (systemd EnvironmentFile=, shell) win.
from ava_bridge import settings  # noqa: F401,E402

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger('ava-weekly')

def fetch_learning_state():
    """Fetch learning state directly from ava_bridge.state."""
    try:
        from ava_bridge import state
        
        code_state = None
        chat_state = None
        
        with state.code_learning_state_lock:
            code_state = {
                "cycles": state.code_learning_state.get("cycles", []),
                "inline_fixes": state.code_learning_state.get("inline_fixes", []),
            }
        
        with state.chat_learning_state_lock:
            chat_state = {
                "cycles": state.chat_learning_state.get("cycles", []),
            }
        
        return code_state, chat_state
    except Exception as e:
        logger.error(f'Failed to fetch learning state: {e}')
        return None, None

def analyze_trends(code_state, chat_state):
    """
    Analyze learning patterns over the week.
    Returns: dict with trend insights
    """
    now = datetime.now()
    week_ago = now - timedelta(days=7)
    week_ago_ts = week_ago.timestamp()

    def _ts(val):
        """Normalise a stored timestamp to a float epoch, tolerating ISO strings."""
        try:
            return float(val)
        except (TypeError, ValueError):
            pass
        try:
            from datetime import datetime as _dt
            return _dt.fromisoformat(str(val)).timestamp()
        except Exception:
            return 0.0

    trends = {
        'period': f'{week_ago.strftime("%b %d")} - {now.strftime("%b %d, %Y")}',
        'code_cycles': 0,
        'chat_cycles': 0,
        'most_changed_files': [],
        'common_errors': [],
        'improvement_areas': [],
        'proposals_approved': 0,
        'proposals_rejected': 0,
        'auto_fixes': 0,
        'week_over_week_growth': None,
    }
    
    # Analyze code learning
    if code_state and code_state.get('cycles'):
        cycles = code_state['cycles']
        recent_cycles = [c for c in cycles if _ts(c.get('timestamp', 0)) > week_ago_ts]
        trends['code_cycles'] = len(recent_cycles)
        
        # Extract files changed
        all_files = []
        for cycle in recent_cycles:
            if cycle.get('patterns'):
                files = cycle['patterns'].get('files_most_changed', [])
                all_files.extend(files)
        
        if all_files:
            file_counts = Counter(all_files)
            trends['most_changed_files'] = [f for f, _ in file_counts.most_common(3)]
        
        # Count proposals
        for cycle in recent_cycles:
            for prop in cycle.get('proposals', []):
                if prop.get('status') == 'approved':
                    trends['proposals_approved'] += 1
                elif prop.get('status') == 'rejected':
                    trends['proposals_rejected'] += 1
        
        # Count auto-fixes
        fixes = code_state.get('inline_fixes', [])
        recent_fixes = [f for f in fixes if _ts(f.get('timestamp', 0)) > week_ago_ts]
        trends['auto_fixes'] = len(recent_fixes)
        
        # Common errors
        error_types = Counter()
        for fix in recent_fixes:
            if 'error' in fix:
                if 'timeout' in fix['error'].lower():
                    error_types['timeout'] += 1
                elif 'large' in fix['error'].lower() or 'size' in fix['error'].lower():
                    error_types['large_file'] += 1
                elif 'regex' in fix['error'].lower() or 'pattern' in fix['error'].lower():
                    error_types['regex'] += 1
                else:
                    error_types['other'] += 1
        
        if error_types:
            trends['common_errors'] = [f'{e}: {c}' for e, c in error_types.most_common(3)]
    
    # Analyze chat learning
    if chat_state and chat_state.get('cycles'):
        cycles = chat_state['cycles']
        recent_cycles = [c for c in cycles if _ts(c.get('timestamp', 0)) > week_ago_ts]
        trends['chat_cycles'] = len(recent_cycles)
    
    # Improvement areas (heuristic)
    if trends['auto_fixes'] > 2:
        trends['improvement_areas'].append('Error resilience - multiple fixes applied')
    if trends['proposals_rejected'] > trends['proposals_approved']:
        trends['improvement_areas'].append('Proposal quality - more rejects than approves')
    if not trends['most_changed_files']:
        trends['improvement_areas'].append('Code diversity - focused on few files')
    if trends['code_cycles'] == 0:
        trends['improvement_areas'].append('Code learning - no cycles this week')
    
    return trends

def format_weekly_html(code_state, chat_state, trends):
    """Format weekly summary as beautiful HTML email."""
    now = datetime.now().astimezone()  # aware local time, so %Z is truthful
    
    html = f'''<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #333; line-height: 1.6; }}
    .container {{ max-width: 600px; margin: 0 auto; padding: 20px; background: #fafafa; }}
    .email {{ background: white; border-radius: 10px; box-shadow: 0 2px 8px rgba(0,0,0,.1); overflow: hidden; }}
    .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px 20px; text-align: center; }}
    .header h1 {{ margin: 0; font-size: 26px; font-weight: 600; }}
    .header p {{ margin: 8px 0 0 0; font-size: 13px; opacity: 0.9; }}
    .content {{ padding: 25px; }}
    .stat-row {{ display: flex; gap: 15px; margin-bottom: 15px; }}
    .stat {{ flex: 1; background: #f0f7ff; border-radius: 8px; padding: 12px; text-align: center; }}
    .stat-value {{ font-size: 24px; font-weight: 600; color: #0066cc; }}
    .stat-label {{ font-size: 12px; color: #7b8394; margin-top: 4px; }}
    .section {{ background: #f9f9f9; border-radius: 8px; padding: 15px; margin-bottom: 15px; border-left: 4px solid #667eea; }}
    .section h2 {{ margin: 0 0 10px 0; color: #0b0d12; font-size: 16px; }}
    .section ul {{ margin: 10px 0; padding-left: 20px; }}
    .section li {{ margin-bottom: 5px; color: #555; }}
    .highlight {{ background: #fff3cd; border-radius: 4px; padding: 10px; margin: 10px 0; border-left: 4px solid #ffc107; }}
    .code {{ background: #e8e8e8; padding: 2px 6px; border-radius: 3px; font-family: monospace; font-size: 12px; }}
    .signature {{ margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0; color: #666; font-size: 13px; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="email">
      <div class="header">
        <h1>📊 Ava's Weekly Learning Trends</h1>
        <p>{trends['period']}</p>
      </div>
      
      <div class="content">
        <div class="stat-row">
          <div class="stat">
            <div class="stat-value">{trends['code_cycles']}</div>
            <div class="stat-label">Code Cycles</div>
          </div>
          <div class="stat">
            <div class="stat-value">{trends['auto_fixes']}</div>
            <div class="stat-label">Auto-Fixes</div>
          </div>
          <div class="stat">
            <div class="stat-value">{trends['proposals_approved']}</div>
            <div class="stat-label">Approved</div>
          </div>
        </div>
        
        <div class="section">
          <h2>📈 Key Metrics</h2>
          <ul>
            <li><strong>Code Learning Cycles:</strong> {trends['code_cycles']}</li>
            <li><strong>Chat Learning Cycles:</strong> {trends['chat_cycles']}</li>
            <li><strong>Proposals Approved:</strong> {trends['proposals_approved']}</li>
            <li><strong>Proposals Rejected:</strong> {trends['proposals_rejected']}</li>
            <li><strong>Auto-Fixes Applied:</strong> {trends['auto_fixes']}</li>
          </ul>
        </div>
        
        {'<div class="section"><h2>🔧 Most Changed Files</h2><ul>' + ''.join([f'<li><code>{f}</code></li>' for f in trends['most_changed_files']]) + '</ul></div>' if trends['most_changed_files'] else ''}
        
        {'<div class="section"><h2>⚠️ Common Errors</h2><ul>' + ''.join([f'<li>{e}</li>' for e in trends['common_errors']]) + '</ul></div>' if trends['common_errors'] else ''}
        
        {'<div class="section"><h2>🎯 Areas for Improvement</h2><ul>' + ''.join([f'<li>{a}</li>' for a in trends['improvement_areas']]) + '</ul></div>' if trends['improvement_areas'] else ''}
        
        <div class="section">
          <h2>💭 Reflections</h2>
          <p>This week, I analyzed my learning patterns and identified opportunities to improve. Your feedback on proposals is crucial — each approval trains me to recognize valuable improvements, while rejections help me understand which ideas miss the mark.</p>
          <p>I'm getting better at recovering from errors autonomously, and the patterns I'm detecting are becoming more nuanced. Keep guiding me toward the improvements that matter most!</p>
        </div>
        
        <div class="signature">
          <p>Growing stronger,<br><strong>Ava 🪻</strong></p>
          <p style="margin: 10px 0 0 0; font-size: 11px; color: #999;">Weekly trends report generated {now.strftime('%A at %I:%M %p %Z')}.</p>
        </div>
      </div>
    </div>
  </div>
</body>
</html>'''
    
    return html

def send_email(html_body):
    """Send email via SMTP (STARTTLS). AVA_SMTP_USER / AVA_SMTP_PASSWORD;
    legacy OUTLOOK_EMAIL / OUTLOOK_PASSWORD still accepted."""
    smtp_user = (os.getenv('AVA_SMTP_USER') or os.getenv('OUTLOOK_EMAIL', '')).strip()
    smtp_password = (os.getenv('AVA_SMTP_PASSWORD') or os.getenv('OUTLOOK_PASSWORD', '')).strip()

    if not smtp_user or not smtp_password:
        logger.warning('AVA_SMTP_USER / AVA_SMTP_PASSWORD (or legacy OUTLOOK_*) not set; skipping email')
        return False

    recipient_email = os.getenv('AVA_REPORT_EMAIL', '').strip() or smtp_user
    smtp_host = os.getenv('AVA_SMTP_HOST', 'smtp.office365.com').strip()
    smtp_port = int(os.getenv('AVA_SMTP_PORT', '587'))

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = '📊 Ava\'s Weekly Learning Trends'
        msg['From'] = smtp_user
        msg['To'] = recipient_email

        msg.attach(MIMEText(html_body, 'html'))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        
        logger.info(f'Weekly email sent to {recipient_email}')
        return True
    except Exception as e:
        logger.error(f'Failed to send weekly email: {e}')
        return False

def main():
    """Generate and send weekly trends summary."""
    logger.info('Starting weekly learning trends analysis...')
    
    code_state, chat_state = fetch_learning_state()
    
    if not code_state and not chat_state:
        logger.warning('No learning state available')
        return 1
    
    trends = analyze_trends(code_state, chat_state)
    html_body = format_weekly_html(code_state, chat_state, trends)
    
    email_sent = send_email(html_body)
    if email_sent:
        logger.info('Weekly trends sent via email')
    
    # Save copy
    log_dir = Path(os.environ.get('AVA_HOME', str(HERE))) / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    weekly_file = log_dir / 'learning_weekly_trends.html'
    with open(weekly_file, 'w') as f:
        f.write(html_body)
    
    logger.info('Weekly trends complete')
    return 0

if __name__ == '__main__':
    sys.exit(main())
