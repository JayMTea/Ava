#!/usr/bin/env python3
"""
Ava Daily Learning Digest (Phase 5)
Generates and sends a beautiful HTML email with learning summary.
Run on your deployment's schedule (e.g. a systemd timer or cron job).
"""

import sys
import os
import logging
from pathlib import Path
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

HERE = Path(__file__).resolve().parent

# Importing settings auto-loads .env (repo root + $AVA_HOME) into the
# environment; values already set (systemd EnvironmentFile=, shell) win.
from ava_bridge import settings  # noqa: E402

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger('ava-digest')

def fetch_learning_state():
    """
    Fetch learning state directly from ava_bridge.state.
    Returns: (code_state, chat_state) tuple
    """
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
    except Exception as e:  # noqa: BLE001 — logged; a digest failure must not abort the run
        logger.error(f'Failed to fetch learning state: {e}')
        return None, None

def format_digest_html(code_state, chat_state):
    """
    Format a daily digest as beautiful HTML email signed by Ava.
    Returns: tuple (html_body, text_body)
    """
    now = datetime.now().astimezone()  # aware local time, so %Z is truthful
    
    # Summarize code learning
    code_html = '<p><em>No code learning cycles yet.</em></p>'
    if code_state and code_state.get('cycles'):
        cycles = code_state['cycles']
        inline_fixes = code_state.get('inline_fixes', [])
        latest = cycles[-1] if cycles else None
        
        code_html = f'<p><strong>Total cycles:</strong> {len(cycles)} | <strong>Auto-fixes:</strong> {len(inline_fixes)}</p>'
        
        if latest:
            proposals = latest.get('proposals', [])
            approved = sum(1 for p in proposals if p.get('status') == 'approved')
            code_html += f'<p><strong>Latest cycle:</strong> ID <code>{latest.get("id", "?")}</code></p>'
            code_html += f'<p><strong>Proposals:</strong> {len(proposals)} total, {approved} approved</p>'
            
            if inline_fixes:
                code_html += '<p><strong>Recent auto-fixes:</strong></p><ul>'
                for fix in inline_fixes[-3:]:
                    fix_desc = fix.get("fix_applied") or fix.get("description", "?")
                    code_html += f'<li>{fix_desc} (critical: {fix.get("critical", False)})</li>'
                code_html += '</ul>'
    
    # Summarize chat learning
    chat_html = '<p><em>No chat learning cycles yet.</em></p>'
    if chat_state and chat_state.get('cycles'):
        cycles = chat_state['cycles']
        latest = cycles[-1] if cycles else None
        
        chat_html = f'<p><strong>Total cycles:</strong> {len(cycles)}</p>'
        
        if latest:
            proposals = latest.get('proposals', [])
            approved = sum(1 for p in proposals if p.get('status') == 'approved')
            chat_html += f'<p><strong>Latest cycle:</strong> ID <code>{latest.get("id", "?")}</code></p>'
            chat_html += f'<p><strong>Proposals:</strong> {len(proposals)} total, {approved} approved</p>'
    
    html = f'''<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #333; line-height: 1.6; }}
    .container {{ max-width: 600px; margin: 0 auto; padding: 20px; background: #fafafa; }}
    .email {{ background: white; border-radius: 10px; box-shadow: 0 2px 8px rgba(0,0,0,.1); overflow: hidden; }}
    .header {{ background: linear-gradient(135deg, #2a6df4 0%, #1f4fa8 100%); color: white; padding: 30px 20px; text-align: center; }}
    .header h1 {{ margin: 0; font-size: 26px; font-weight: 600; }}
    .header p {{ margin: 8px 0 0 0; font-size: 13px; opacity: 0.9; }}
    .content {{ padding: 25px; }}
    .section {{ background: #f9f9f9; border-radius: 8px; padding: 15px; margin-bottom: 15px; border-left: 4px solid #2a6df4; }}
    .section h2 {{ margin: 0 0 10px 0; color: #0b0d12; font-size: 16px; display: flex; align-items: center; gap: 8px; }}
    .icon {{ font-size: 20px; }}
    .section ul {{ margin: 10px 0; padding-left: 20px; }}
    .section li {{ margin-bottom: 5px; color: #555; }}
    .insight {{ background: #f0f7ff; border-left-color: #0066cc; margin-top: 15px; }}
    .insight h2 {{ color: #0066cc; }}
    code {{ background: #e8e8e8; padding: 2px 6px; border-radius: 3px; font-family: monospace; font-size: 12px; }}
    .button {{ display: inline-block; background: #2a6df4; color: white; padding: 10px 20px; border-radius: 6px; text-decoration: none; font-size: 13px; margin-top: 10px; }}
    .button:hover {{ background: #1f4fa8; }}
    .footer {{ background: #f5f5f5; padding: 20px; text-align: center; border-top: 1px solid #e0e0e0; font-size: 12px; color: #7b8394; }}
    .signature {{ margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0; color: #666; font-size: 13px; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="email">
      <div class="header">
        <h1>Ava's Daily Learning Report</h1>
        <p>{now.strftime('%A, %B %d, %Y at %I:%M %p %Z')}</p>
      </div>
      
      <div class="content">
        <div class="section">
          <h2>Code Learning</h2>
          {code_html}
          <a href="http://localhost:8096/" class="button">View Code Cycles</a>
        </div>
        
        <div class="section">
          <h2>Chat Learning</h2>
          {chat_html}
          <a href="http://localhost:8096/" class="button">View Chat Cycles</a>
        </div>
        
        <div class="section insight">
          <h2 style="color: #0066cc;">What I Learned Today</h2>
          <p>I'm continuously analyzing my own code changes, conversation patterns, and errors to improve myself. When I encounter errors during code edits, I auto-fix them (unless critical) and log the patterns for deeper analysis.</p>
          <p>Your feedback on my proposals helps me learn which improvements are most valuable. Keep an eye on my Learning dashboard to guide my growth!</p>
        </div>
        
        <div class="signature">
          <p>With curiosity & growth,<br><strong>Ava</strong></p>
          <p style="margin: 10px 0 0 0; font-size: 11px; color: #999;">This report is auto-generated by my recursive learning system.</p>
        </div>
      </div>
    </div>
  </div>
</body>
</html>'''
    
    text = f'''Ava's Daily Learning Report
{now.strftime('%A, %B %d, %Y at %I:%M %p %Z')}

CODE LEARNING
{code_html.replace('<p>', '').replace('</p>', chr(10)).replace('<li>', '  • ').replace('</li>', '').replace('<ul>', '').replace('</ul>', '').replace('<code>', '').replace('</code>', '').strip()}

CHAT LEARNING
{chat_html.replace('<p>', '').replace('</p>', chr(10)).replace('<code>', '').replace('</code>', '').strip()}

WHAT I LEARNED TODAY
I'm continuously analyzing my own code changes, conversation patterns, and errors to improve myself.
Your feedback on my proposals helps me learn which improvements are most valuable.

---
With curiosity & growth,
Ava

This report is auto-generated by my recursive learning system.
'''
    
    return html, text

def send_email(html_body, text_body):
    """
    Send email via SMTP (STARTTLS). Configure with AVA_SMTP_USER /
    AVA_SMTP_PASSWORD (use an app-specific password); the legacy
    OUTLOOK_EMAIL / OUTLOOK_PASSWORD names are still accepted.
    """
    smtp_user = (os.getenv('AVA_SMTP_USER') or os.getenv('OUTLOOK_EMAIL', '')).strip()
    smtp_password = (os.getenv('AVA_SMTP_PASSWORD') or os.getenv('OUTLOOK_PASSWORD', '')).strip()

    if not smtp_user or not smtp_password:
        logger.warning('AVA_SMTP_USER / AVA_SMTP_PASSWORD (or legacy OUTLOOK_*) not set; skipping email')
        return False

    # Where to send + which SMTP host — configurable; default to the sender's own
    # mailbox so a fork emails ITSELF, not the author.
    recipient_email = os.getenv('AVA_REPORT_EMAIL', '').strip() or smtp_user
    smtp_host = os.getenv('AVA_SMTP_HOST', 'smtp.office365.com').strip()
    smtp_port = int(os.getenv('AVA_SMTP_PORT', '587'))

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = 'Ava\'s Daily Learning Report'
        msg['From'] = smtp_user
        msg['To'] = recipient_email

        # Attach text and HTML versions
        msg.attach(MIMEText(text_body, 'plain'))
        msg.attach(MIMEText(html_body, 'html'))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        
        logger.info(f'Email sent to {recipient_email}')
        return True
    except Exception as e:  # noqa: BLE001 — logged; a digest failure must not abort the run
        logger.error(f'Failed to send email: {e}')
        return False

def save_digest_file(html_body):
    """Save digest to logs/learning_digest.html for archival."""
    # settings.logs_dir() honours paths.logs / AVA_LOGS_DIR as well as AVA_HOME;
    # re-deriving it here ignored the first two and split the archive in two.
    log_dir = Path(settings.logs_dir())
    log_dir.mkdir(parents=True, exist_ok=True)
    
    digest_file = log_dir / 'learning_digest.html'
    with open(digest_file, 'w') as f:
        f.write(html_body)
    
    logger.info(f'Digest saved to {digest_file}')
    return digest_file

def main():
    """Fetch learning states and generate/send digest."""
    logger.info('Starting daily learning digest...')
    
    code_state, chat_state = fetch_learning_state()
    
    if not code_state and not chat_state:
        logger.warning('No learning state available; skipping digest')
        return 1
    
    html_body, text_body = format_digest_html(code_state, chat_state)
    
    # Try to send email; fall back to saving file
    email_sent = send_email(html_body, text_body)
    if email_sent:
        logger.info('Digest sent via email')
    else:
        logger.info('Email not sent; saving to file')
    
    # Always save a copy
    save_digest_file(html_body)
    
    logger.info('Digest complete')
    return 0

if __name__ == '__main__':
    sys.exit(main())
