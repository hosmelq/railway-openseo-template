import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

image = sys.argv[1]
suffix = secrets.token_hex(5)
name = "openseo-smoke-" + suffix
network = name + "-net"
storage = name + "-storage"
volume = name + "-data"
storage_volume = storage + "-data"
access_key = "smoke" + suffix
secret_key = secrets.token_hex(24)
bucket = "openseo"
email = "admin@example.com"
password = secrets.token_hex(16)
origin = "https://openseo.test"
base = None
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
STORAGE_IMAGE = "rustfs/rustfs:1.0.3@sha256:9ca9559533b537d10482fe923288b676ad08f92a7069d9a17327fbae3cab688b"

def docker(*args, check=True):
    return subprocess.run(
        ["docker", *args], check=check, text=True, capture_output=True
    ).stdout.strip()

def logs_of(container):
    return subprocess.run(
        ["docker", "logs", container], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    ).stdout

def call(path, headers=None, body=None, method=None):
    headers = {
        "Host": "openseo.test",
        "Origin": origin,
        "X-Forwarded-Proto": "https",
        **(headers or {}),
    }
    request = urllib.request.Request(
        base + path, headers=headers, data=body, method=method
    )
    try:
        response = urllib.request.urlopen(request, timeout=60)
    except urllib.error.HTTPError as error:
        response = error
    return response.status, response.read().decode(), response.headers

def post_json(path, payload, headers=None):
    return call(
        path,
        {"Content-Type": "application/json", **(headers or {})},
        json.dumps(payload).encode(),
    )

def sign_in(user_email, user_password):
    status, body, headers = post_json(
        "/api/auth/sign-in/email", {"email": user_email, "password": user_password}
    )
    cookies = [
        value.split(";")[0] for key, value in headers.items() if key == "Set-Cookie"
    ]
    return status, "; ".join(cookies)

def rpc(key, method, params=None):
    status, body, _ = post_json(
        "/mcp",
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        {
            "Authorization": "Bearer " + key,
            "Accept": "application/json, text/event-stream",
        },
    )
    assert status == 200, (method, status, body[:200])
    result = json.loads(body[body.index("{") :])
    assert "error" not in result, (method, result)
    return result["result"]

def tool(key, tool_name, arguments):
    result = rpc(key, "tools/call", {"name": tool_name, "arguments": arguments})
    assert not result.get("isError"), (tool_name, result)
    return result

def app_env(extra):
    env = {
        "CELLD_BUCKET": bucket,
        "S3_ENDPOINT": "http://" + storage + ":9000",
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "PORT": "3000",
        "RAILWAY_PUBLIC_DOMAIN": "openseo.test",
        **extra,
    }
    return [arg for key, value in env.items() for arg in ("-e", key + "=" + value)]

def start(extra=None):
    global base
    docker(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        network,
        "--cpus",
        "1",
        "--memory",
        "2g",
        "-p",
        "127.0.0.1::3000",
        "-v",
        volume + ":/data",
        *app_env(extra or {}),
        image,
    )
    port = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"][
        "3000/tcp"
    ][0]["HostPort"]
    base = "http://127.0.0.1:" + port
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            status, body, _ = call("/api/health")
        except (OSError, urllib.error.URLError):
            pass
        else:
            assert status == 200, (status, body[:200])
            assert json.loads(body)["checks"]["database"]["status"] == "ok", body
            return
        state = json.loads(docker("inspect", name))[0]["State"]
        assert state["Running"], (state, logs_of(name)[-3000:])
        time.sleep(0.5)
    raise AssertionError("Image did not become healthy within five minutes.")

def stop():
    docker("stop", "-t", "60", name)
    logs = logs_of(name)
    docker("rm", name)
    return logs

def refused(extra, message, legacy=False):
    probe = name + "-refused"
    if legacy:
        docker("run", "--rm", "-u", "0", "-v", probe + ":/data", "--entrypoint", "mkdir", image, "-p", "/data/state/v3")
    docker(
        "run",
        "--name",
        probe,
        "--network",
        network,
        "-v",
        probe + ":/data",
        *app_env(extra),
        image,
        check=False,
    )
    logs = logs_of(probe)
    state = json.loads(docker("inspect", probe))[0]["State"]
    docker("rm", "-f", probe, check=False)
    docker("volume", "rm", probe, check=False)
    assert state["ExitCode"] != 0, logs[-3000:]
    assert message in logs, logs[-3000:]

try:
    docker("network", "create", network)
    docker(
        "run",
        "-d",
        "--name",
        storage,
        "--network",
        network,
        "-v",
        storage_volume + ":/data",
        "-u",
        "0",
        "-e",
        "RUSTFS_ACCESS_KEY=" + access_key,
        "-e",
        "RUSTFS_SECRET_KEY=" + secret_key,
        STORAGE_IMAGE,
    )
    deadline = time.monotonic() + 60
    while True:
        created = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                network,
                "--entrypoint",
                "curl",
                image,
                "-sS",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--aws-sigv4",
                "aws:amz:us-east-1:s3",
                "--user",
                access_key + ":" + secret_key,
                "-H",
                "x-amz-content-sha256: " + EMPTY_SHA256,
                "-X",
                "PUT",
                "http://" + storage + ":9000/" + bucket,
            ],
            text=True,
            capture_output=True,
        ).stdout
        if created in ("200", "409"):
            break
        assert time.monotonic() < deadline, created
        time.sleep(1)

    refused({}, "Set ADMIN_EMAIL and ADMIN_PASSWORD")
    refused({"PORT": "8080"}, "PORT must be an integer")
    refused({"AUTH_MODE": "local_noauth"}, "AUTH_MODE must be password")
    refused(
        {"ADMIN_EMAIL": email, "ADMIN_PASSWORD": password},
        "data from the previous OpenSEO image",
        legacy=True,
    )

    start({"ADMIN_EMAIL": email, "ADMIN_PASSWORD": password})
    operator_secret = json.loads(
        docker("exec", name, "cat", "/config/secrets.json")
    )["OPENSEO_OPERATOR_SECRET"]
    for path in ["/api/standalone/admin", "/api/standalone/import", "/api/standalone/admin/"]:
        status, body, _ = post_json(
            path, {"command": "begin"}, {"x-openseo-operator-secret": operator_secret}
        )
        assert (status, body) == (404, "Not found"), (path, status, body[:200])
    assert call("/cdn-cgi/handler/scheduled")[0] == 404
    status, body, _ = post_json(
        "/api/auth/sign-up/email",
        {"email": "intruder@example.com", "password": "intruder-password", "name": "x"},
    )
    assert status == 403 and "Registration is closed" in body, (status, body)
    assert sign_in(email, "wrong-password")[0] == 401
    status, cookie = sign_in(email, password)
    assert status == 200 and cookie, status
    status, body, _ = post_json(
        "/api/auth/api-key/create", {"name": "smoke"}, {"Cookie": cookie}
    )
    assert status == 200, (status, body[:200])
    key = json.loads(body)["key"]
    rpc(
        key,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "railway-image-smoke", "version": "1.0"},
        },
    )
    tools = rpc(key, "tools/list")["tools"]
    assert len(tools) > 0
    created = tool(key, "create_project", {"name": "Persistence check", "domain": "example.com"})
    project = created["structuredContent"]["project"]["id"]
    assert created["_meta"]["url"].startswith(origin + "/"), created

    tool(key, "run_site_audit", {"projectId": project, "url": "https://example.com", "maxPages": 10})
    deadline = time.monotonic() + 120
    while True:
        audits = tool(key, "list_site_audits", {"projectId": project})
        audit = audits["structuredContent"]["audits"][0]
        if audit["status"] in ("completed", "failed"):
            break
        assert time.monotonic() < deadline, audit
        time.sleep(2)
    pages = tool(key, "get_audit_pages", {"projectId": project, "auditId": audit["id"]})
    assert "| 200 | ok |" in pages["content"][0]["text"], pages

    before = tool(key, "list_projects", {})
    stats = docker("stats", "--no-stream", "--format", "{{.MemUsage}} {{.CPUPerc}}", name)
    logs = stop()
    assert "create-user succeeded" in logs, logs

    other_password = secrets.token_hex(16)
    start({"ADMIN_EMAIL": "other@example.com", "ADMIN_PASSWORD": other_password})
    assert sign_in(email, password)[0] == 200
    assert sign_in("other@example.com", other_password)[0] == 401
    after = tool(key, "list_projects", {})
    assert after == before, (before, after)
    logs = stop()
    assert "create-user" not in logs, logs

    print(
        json.dumps(
            {
                "administrator": "passed",
                "registration_closed": "passed",
                "private_endpoints": "passed",
                "mcp_tools": len(tools),
                "crawl_gateway": "passed",
                "persistence": "passed",
                "limits": {"cpu": 1, "memory": "2 GiB"},
                "runtime_snapshot": stats,
            }
        )
    )
finally:
    state = subprocess.run(["docker", "inspect", name], text=True, capture_output=True)
    if state.returncode == 0:
        if sys.exc_info()[0] is not None:
            print(logs_of(name)[-6000:], file=sys.stderr)
        docker("rm", "-f", name, check=False)
    docker("rm", "-f", storage, check=False)
    docker("volume", "rm", volume, storage_volume, check=False)
    docker("network", "rm", network, check=False)
