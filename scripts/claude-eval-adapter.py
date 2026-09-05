#!/usr/bin/env python3
"""Claude CLI adapter with actual model usage and applied settings evidence."""
import json
import os
import subprocess
import sys

request = json.load(sys.stdin)
settings = request.get('settings', {})
if not isinstance(settings, dict) or set(settings) - {'effort'}:
    raise SystemExit('unsupported adapter settings; allowed: effort')
effort = settings.get('effort', 'high')
if effort not in {'low', 'medium', 'high'}:
    raise SystemExit('unsupported effort')
command = ['claude', '--print', '--model', request['model'], '--effort', effort, '--strict-mcp-config', '--no-session-persistence', '--safe-mode', '--settings', '{"switchModelsOnFlag":false}', '--tools', '', '--output-format', 'json']
result = subprocess.run(command, input=json.dumps(request, ensure_ascii=False), text=True, capture_output=True, timeout=540)
if result.returncode:
    sys.stderr.write(result.stderr)
    raise SystemExit(result.returncode)
data = json.loads(result.stdout)
if data.get('is_error'):
    raise SystemExit('model returned error')
usage = data.get('modelUsage', {})
if request['model'] not in usage or not isinstance(usage[request['model']], dict) or not usage[request['model']].get('outputTokens', 0):
    raise SystemExit('requested model missing from actual output usage: ' + json.dumps(usage))
print(json.dumps({'model': request['model'], 'output': data.get('result', ''), 'usage': data.get('usage'), 'model_usage': usage, 'adapter_settings': {'effort': effort, 'tools': [], 'strict_mcp_config': True, 'session_persistence': False, 'safe_mode': True, 'switchModelsOnFlag': False}, 'auth_environment': {key: bool(os.environ.get(key)) for key in ['ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_BASE_URL']}}, ensure_ascii=False))
