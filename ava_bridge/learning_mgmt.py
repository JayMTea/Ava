"""
Learning Script Management — Allow Ava to update learning digest scripts
to improve recommendation descriptions, rationales, and formatting.

Handles updates to:
- ava_learning_digest.py (daily digest generation)
- ava_learning_weekly.py (weekly trends analysis)

Updates are persisted and take effect on next scheduled run.
"""

import os
import re
import logging
from pathlib import Path
from datetime import datetime

from . import config

logger = logging.getLogger(__name__)


def update_digest_scripts(script_type: str, updates: dict, content: str = None) -> dict:
    """
    Update learning digest scripts with improvements.
    
    Args:
        script_type: 'daily', 'weekly', or 'both'
        updates: dict with booleans for: improve_rationale, add_confidence, add_context, add_impact, improve_formatting
        content: optional custom Python code to merge/replace
    
    Returns:
        {"success": bool, "updated": list of files, "message": str}
    """
    
    project_root = Path(config.ROOT)
    scripts = {
        'daily': project_root / 'ava_learning_digest.py',
        'weekly': project_root / 'ava_learning_weekly.py',
    }
    
    if script_type not in ['daily', 'weekly', 'both']:
        return {"success": False, "message": "script_type must be 'daily', 'weekly', or 'both'"}

    # Arbitrary custom Python is merged straight into an executable script, so it
    # stays disabled unless the operator explicitly opts in on the host.
    if content and os.environ.get('AVA_ALLOW_SCRIPT_CONTENT') != '1':
        return {"success": False,
                "message": "Custom script-content injection is disabled. Set "
                           "AVA_ALLOW_SCRIPT_CONTENT=1 on the host to allow it."}
    
    targets = []
    if script_type == 'daily':
        targets = ['daily']
    elif script_type == 'weekly':
        targets = ['weekly']
    else:  # both
        targets = ['daily', 'weekly']
    
    updated_files = []
    errors = []
    
    for target in targets:
        script_path = scripts[target]
        
        if not script_path.exists():
            errors.append(f"{target} script not found at {script_path}")
            continue
        
        try:
            # Read current script
            with open(script_path, 'r') as f:
                current = f.read()
            
            # Apply updates based on flags
            modified = current
            
            if updates.get('improve_rationale'):
                modified = _enhance_rationale_function(modified, target)
            
            if updates.get('add_confidence'):
                modified = _add_confidence_scoring(modified, target)
            
            if updates.get('add_context'):
                modified = _add_context_snippets(modified, target)
            
            if updates.get('add_impact'):
                modified = _add_impact_section(modified, target)
            
            if updates.get('improve_formatting'):
                modified = _improve_html_formatting(modified, target)
            
            # Optionally merge custom content
            if content:
                modified = _merge_custom_content(modified, content, target)
            
            # Only write if changed
            if modified != current:
                # Never write a script that won't parse (guards against corrupt or
                # malformed injected content breaking the scheduled run).
                try:
                    compile(modified, str(script_path), 'exec')
                except SyntaxError as e:
                    errors.append(f"{target}: refusing to write invalid Python ({e})")
                    continue
                with open(script_path, 'w') as f:
                    f.write(modified)
                updated_files.append(str(script_path))
                logger.info(f"Updated {target} digest script: {script_path}")
            else:
                logger.info(f"No changes needed for {target} digest script")
        
        except Exception as e:
            error_msg = f"Failed to update {target} script: {str(e)}"
            errors.append(error_msg)
            logger.error(error_msg)
    
    if errors:
        return {
            "success": len(updated_files) > 0,
            "updated": updated_files,
            "message": f"Partial update: {len(updated_files)} scripts updated, {len(errors)} errors",
            "errors": errors,
        }
    
    return {
        "success": True,
        "updated": updated_files,
        "message": f"Successfully updated {len(updated_files)} learning digest script(s)",
    }


def _enhance_rationale_function(script: str, target: str) -> str:
    """Add detailed rationale explanations to recommendations."""
    
    # Find the format_digest_html function and enhance it
    if 'def format_rationale(' not in script:
        # Add a new helper function for building rationales
        helper_func = '''

def format_rationale(proposal):
    """
    Format a detailed rationale for a code/chat recommendation.
    Includes: why it was suggested, what makes it valuable, confidence level.
    """
    reason = proposal.get('reason', 'No reason provided')
    status = proposal.get('status', 'pending')
    
    rationale_html = f'<p><strong>Rationale:</strong> {reason}</p>'
    
    # Add confidence if available
    confidence = proposal.get('confidence')
    if confidence is not None:
        level = 'High' if confidence > 0.8 else 'Medium' if confidence > 0.5 else 'Low'
        rationale_html += f'<p><small>Confidence: {level} ({confidence*100:.0f}%)</small></p>'
    
    return rationale_html
'''
        script = script.rstrip() + helper_func
    
    # Enhance the proposal rendering to use rationale
    if 'format_rationale(' not in script:
        script = re.sub(
            r"(for p in (?:proposals|cycles\[-1\]\.get\('proposals', \[\]))\]:",
            r"\1]:\n            html += format_rationale(p)",
            script
        )
    
    return script


def _add_confidence_scoring(script: str, target: str) -> str:
    """Add confidence scores to recommendations."""
    
    if 'confidence' not in script.lower():
        # Add confidence calculation if not present
        confidence_func = '''

def calculate_confidence(proposal, history_context):
    """
    Calculate confidence score (0.0-1.0) for a recommendation based on:
    - Pattern frequency in history
    - Similar successful outcomes
    - Data quality
    """
    base_confidence = 0.5
    
    # Boost for repeated patterns
    if proposal.get('pattern_count', 0) > 2:
        base_confidence += 0.3
    
    # Boost for previously successful similar recommendations
    if proposal.get('similar_successes', 0) > 0:
        base_confidence += min(0.2, proposal.get('similar_successes', 0) * 0.1)
    
    return min(1.0, base_confidence)
'''
        script = script.rstrip() + confidence_func
    
    return script


def _add_context_snippets(script: str, target: str) -> str:
    """Add relevant context and code snippets to recommendations."""
    
    context_func = '''

def format_context(proposal):
    """
    Format relevant context for a recommendation:
    - Affected files
    - Code snippets (if code recommendation)
    - Related metrics
    """
    context_html = '<div class="context">'
    
    if proposal.get('file'):
        context_html += f'<p><strong>File:</strong> <code>{proposal["file"]}</code></p>'
    
    if proposal.get('snippet'):
        context_html += f'<pre><code>{proposal["snippet"]}</code></pre>'
    
    if proposal.get('metrics'):
        context_html += '<p><strong>Metrics:</strong>'
        for metric, value in proposal.get('metrics', {}).items():
            context_html += f' {metric}={value};'
        context_html += '</p>'
    
    context_html += '</div>'
    return context_html
'''
    
    if 'format_context(' not in script:
        script = script.rstrip() + context_func
    
    return script


def _add_impact_section(script: str, target: str) -> str:
    """Add expected impact/outcome section to recommendations."""
    
    impact_func = '''

def format_impact(proposal):
    """
    Format the expected impact of following this recommendation:
    - Performance improvements
    - Maintenance benefits
    - Risk reduction
    """
    impact_html = '<div class="impact"><h4>Expected Impact</h4><ul>'
    
    impacts = {
        'performance': 'Performance improvement',
        'maintainability': 'Improved maintainability',
        'security': 'Enhanced security',
        'readability': 'Better code readability',
        'test_coverage': 'Increased test coverage',
    }
    
    for impact_type, label in impacts.items():
        if proposal.get(impact_type):
            impact_html += f'<li>{label}</li>'
    
    impact_html += '</ul></div>'
    return impact_html
'''
    
    if 'format_impact(' not in script:
        script = script.rstrip() + impact_func
    
    return script


def _improve_html_formatting(script: str, target: str) -> str:
    """Improve HTML formatting and visual hierarchy."""
    
    # Add CSS improvements
    if '<style>' not in script:
        style_section = '''

# Enhanced CSS for better digest readability
CSS_STYLES = """
<style>
  .digest-container { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 600px; }
  .recommendation { border-left: 4px solid #4CAF50; padding: 12px; margin: 12px 0; background: #f5f5f5; border-radius: 4px; }
  .recommendation.approved { border-left-color: #2196F3; }
  .recommendation.pending { border-left-color: #FF9800; }
  .rationale { font-size: 14px; color: #555; margin: 8px 0; }
  .context { font-size: 13px; background: white; padding: 8px; border-radius: 3px; margin: 8px 0; }
  .impact { font-size: 13px; margin: 8px 0; }
  .impact ul { margin: 4px 0; padding-left: 20px; }
  code { background: #f0f0f0; padding: 2px 4px; border-radius: 2px; font-size: 12px; }
</style>
"""
'''
        script = script.rstrip() + style_section
    
    return script


def _merge_custom_content(script: str, content: str, target: str) -> str:
    """Merge custom Python code into the script."""
    
    # This allows Ava to provide custom code snippets
    # They would be inserted after imports but before main logic
    if content.strip():
        # Find the end of imports
        import_section_end = script.rfind('\nlogger = logging.getLogger')
        if import_section_end > 0:
            insert_pos = import_section_end + script[import_section_end:].find('\n') + 1
            script = script[:insert_pos] + '\n# Custom enhancements from Ava\n' + content + '\n' + script[insert_pos:]
    
    return script


def read_digest_scripts() -> dict:
    """Read and return the current digest scripts."""
    
    project_root = Path(config.ROOT)
    scripts = {}
    
    for script_type in ['daily', 'weekly']:
        script_path = project_root / ('ava_learning_digest.py' if script_type == 'daily' else 'ava_learning_weekly.py')
        try:
            with open(script_path, 'r') as f:
                scripts[script_type] = {
                    "path": str(script_path),
                    "content": f.read(),
                    "size": script_path.stat().st_size,
                    "modified": datetime.fromtimestamp(script_path.stat().st_mtime).isoformat(),
                }
        except Exception as e:
            scripts[script_type] = {"error": str(e)}
    
    return scripts
