"""Exercise the actual image, including a fresh container over retained state."""
import base64
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

image = sys.argv[1]
suffix = secrets.token_hex(5)
name = 'openseo-smoke-' + suffix
volume = name + '-data'
password = secrets.token_hex(24)
username = 'admin'
base = None
basic = 'Basic ' + base64.b64encode(('admin:' + password).encode()).decode()
bearer = 'Bearer ' + password

def docker(*args, check=True):
    return subprocess.run(['docker', *args], check=check, text=True, capture_output=True).stdout.strip()

def call(path, headers=None, body=None):
    headers = {'Host': 'openseo.test', 'X-Forwarded-Proto': 'https', **(headers or {})}
    request = urllib.request.Request(base + path, headers=headers, data=body)
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    return response.status, response.read().decode(), response.headers

def rpc(method, params=None):
    status, body, _ = call('/mcp', {
        'Authorization': bearer, 'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
    }, json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}).encode())
    assert status == 200, (method, status, body[:200])
    result = json.loads(body)
    assert 'error' not in result, (method, result)
    return result['result']

def start(runtime_env=None, stale_snapshot=False):
    global base
    extra_env = [arg for key, value in (runtime_env or {}).items() for arg in ('-e', key + '=' + value)]
    command = ('--entrypoint', '/bin/sh', image, '-c',
               "printf 'AUTH_MODE=local_noauth\\nDATAFORSEO_API_KEY=Y2lAZXhhbXBsZS5jb206c3RhbGUtYnVpbGQtZml4dHVyZQ==\\n' > /app/dist/server/.dev.vars; exec node /opt/railway/railway-entrypoint.mjs") if stale_snapshot else (image,)
    docker('run', '-d', '--name', name, '--cpus', '0.5', '--memory', '1g',
           '-p', '127.0.0.1::8080', '-v', volume + ':/data',
           '-e', 'ACCESS_PASSWORD=' + password, '-e', 'OPENSEO_TELEMETRY_DISABLED=1',
           '-e', 'ACCESS_USERNAME=' + username, '-e', 'RAILWAY_PUBLIC_DOMAIN=openseo.test', *extra_env, *command)
    port = json.loads(docker('inspect', name))[0]['NetworkSettings']['Ports']['8080/tcp'][0]['HostPort']
    base = 'http://127.0.0.1:' + port
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        try:
            if call('/healthz')[:2] == (200, 'ok'):
                return
        except (OSError, urllib.error.URLError):
            pass
        state = json.loads(docker('inspect', name))[0]['State']
        assert state['Running'], state
        time.sleep(2)
    raise AssertionError('Image did not become healthy within four minutes.')

try:
    docker('volume', 'create', volume)
    start(stale_snapshot=True)
    for path in ['/', '/mcp', '/mcp/', '/mcp?test=1', '/api/health']:
        status, _, headers = call(path)
        assert status == 401, (path, status)
        assert not headers.get('Set-Cookie'), 'Unauthenticated requests must never receive a session.'
    assert call('/cdn-cgi/handler/scheduled')[0] == 404
    assert call('/mcp', {'Authorization': 'Bearer invalid'})[0] == 401
    assert call('/', {'Cookie': 'openseo_gate=invalid'})[0] == 401
    wrong_basic = 'Basic ' + base64.b64encode(b'admin:invalid').decode()
    assert not call('/', {'Authorization': wrong_basic})[2].get('Set-Cookie')
    status, _, headers = call('/', {'Authorization': basic})
    assert status == 200
    cookie = headers['Set-Cookie'].split(';')[0]
    assert call('/', {'Cookie': cookie})[0] == 200
    assert call('/', {'Cookie': 'prefix_' + cookie})[0] == 401
    assert call('/mcp', {'Cookie': cookie})[0] == 401
    status, body, _ = call('/api/health', {'Authorization': basic})
    assert status == 200
    health = json.loads(body)
    assert health['checks']['database']['status'] == 'ok', health
    assert health['checks']['dataforseo']['status'] == 'warn', health
    rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                       'clientInfo': {'name': 'railway-image-smoke', 'version': '1.0'}})
    tools = rpc('tools/list')['tools']
    assert len(tools) > 0
    created = rpc('tools/call', {'name': 'create_project',
                  'arguments': {'name': 'Persistence check', 'domain': 'example.com'}})
    assert not created.get('isError'), created
    before = rpc('tools/call', {'name': 'list_projects', 'arguments': {}})
    assert 'Persistence check' in json.dumps(before), before
    assert before['_meta']['url'] == 'https://openseo.test/', before
    stats = docker('stats', '--no-stream', '--format', '{{.MemUsage}} {{.CPUPerc}}', name)
    docker('stop', '-t', '15', name)
    logs = docker('logs', name)
    assert 'No startup build.' in logs
    assert 'Building client + server' not in logs
    docker('rm', name)
    username = 'personal-user'
    # This fixture validates configuration only; no paid vendor request is made.
    dataforseo_key = base64.b64encode(b'ci@example.com:runtime-only-fixture').decode()
    start({'DATAFORSEO_API_KEY': dataforseo_key})
    assert call('/', {'Authorization': basic})[0] == 401
    assert call('/', {'Cookie': cookie})[0] == 401
    custom_basic = 'Basic ' + base64.b64encode((username + ':' + password).encode()).decode()
    assert call('/', {'Authorization': custom_basic})[0] == 200
    status, body, _ = call('/api/health', {'Authorization': custom_basic})
    assert status == 200
    assert json.loads(body)['checks']['dataforseo']['status'] == 'ok', body
    after = rpc('tools/call', {'name': 'list_projects', 'arguments': {}})
    assert not after.get('isError'), after
    assert after == before, (before, after)
    print(json.dumps({'authentication': 'passed', 'custom_username': 'passed', 'mcp_tools': len(tools),
                      'persistence': 'passed', 'runtime_integration_variables': 'passed', 'startup_build': False,
                      'limits': {'cpu': 0.5, 'memory': '1 GiB'}, 'runtime_snapshot': stats}))
finally:
    state = subprocess.run(['docker', 'inspect', name], text=True, capture_output=True)
    if state.returncode == 0:
        if sys.exc_info()[0] is not None:
            print(docker('logs', '--tail', '100', name, check=False), file=sys.stderr)
        docker('rm', '-f', name, check=False)
    docker('volume', 'rm', volume, check=False)
