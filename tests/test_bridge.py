import asyncio
import hashlib
import hmac
import http.server
import json
import os
import socket
import tempfile
import threading
import time
import uuid
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient


os.environ["MCP_API_KEY"] = "test-mcp"
os.environ["INBOUND_WEBHOOK_SECRET"] = "test-inbound"
os.environ["CURSOR_WEBHOOK_URL"] = "https://automation.invalid/hook"
os.environ["CURSOR_WEBHOOK_API_KEY"] = "x"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "bridge.db")
os.environ["ALLOWED_HOSTS"] = "testserver"
os.environ["CALLBACK_ALLOW_HTTP"] = "1"

from app import main


@pytest.fixture(scope="session")
def client():
    with TestClient(main.app) as test_client:
        yield test_client


def signed_headers(body: bytes) -> dict[str, str]:
    signature = hmac.new(b"test-inbound", body, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/json",
        "x-webhook-signature": f"sha256={signature}",
    }


def test_health_and_mcp_auth_and_tools(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.post("/mcp").status_code == 401
    response = client.post(
        "/mcp",
        headers={
            "x-api-key": "test-mcp",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert response.status_code == 200
    if "application/json" in response.headers.get("content-type", ""):
        payload = response.json()
    else:
        data = next(line[6:] for line in response.text.splitlines() if line.startswith("data:"))
        payload = json.loads(data)
    tools = payload["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert {"get_grokbot_run", "wait_for_grokbot_answer", "list_grokbot_runs"} <= names


def test_bad_signature(client):
    response = client.post("/hooks/grokbot", content=b"{}", headers={
        "x-webhook-signature": "sha256=bad",
        "content-type": "application/json",
    })
    assert response.status_code == 401


def test_inbound_without_callback(client):
    body = b'{"event":"finished"}'
    response = client.post("/hooks/grokbot", content=body, headers=signed_headers(body))
    assert response.status_code == 200
    assert response.json()["callback"] == "none"
    assert response.json()["note"] == "コールバックURLなし"


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        self.__class__.requests.append(json.loads(self.rfile.read(length)))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args):
        pass


def test_inbound_delivers_callback(client):
    CallbackHandler.requests = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/callback"
        body = json.dumps({"callback_url": url}).encode()
        response = client.post("/hooks/grokbot", content=body, headers=signed_headers(body))
        assert response.status_code == 200
        assert response.json()["callback"] == "delivered"
        assert CallbackHandler.requests
        delivered = CallbackHandler.requests[-1]
        assert delivered["ok"] is True
        assert "event_id" in delivered["answer"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_callback_url_safety(monkeypatch):
    monkeypatch.setattr(main, "CALLBACK_ALLOW_HTTP", False)
    assert main._is_safe_callback_url("https://127.0.0.1/")[0] is False
    assert main._is_safe_callback_url("https://localhost/")[0] is False
    assert main._is_safe_callback_url("http://example.com/")[0] is False
    assert main._is_safe_callback_url("https://user@example.com/")[0] is False
    assert main._is_safe_callback_url("https://testserver/")[0] is False

    def public_dns(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(main.socket, "getaddrinfo", public_dns)
    assert main._is_safe_callback_url("https://example.com/")[0] is True

    def private_dns(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0))]

    monkeypatch.setattr(main.socket, "getaddrinfo", private_dns)
    assert main._is_safe_callback_url("https://example.com/")[0] is False


def test_ask_grokbot_callback_capture_and_duplicate(client, monkeypatch):
    captured = []

    async def fake_post(_self, url, **kwargs):
        captured.append((url, kwargs))
        return httpx.Response(
            200,
            json={"success": True, "runUuid": "r1"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(main.httpx.AsyncClient, "post", fake_post)
    result = json.loads(asyncio.run(main.ask_grokbot({
        "prompt": "hello",
        "run_id": "caller-run-id",
    }, wait_seconds=0)))
    assert result["ok"] is True
    assert result["run_id"]
    correlation = uuid.UUID(result["run_id"])
    assert correlation.version == 4
    assert result["request_id"] == result["run_id"]
    assert captured[0][1]["json"]["run_id"] == result["run_id"]
    assert captured[0][1]["json"]["request_id"] == result["run_id"]
    assert captured[0][1]["json"]["run_id"] != "caller-run-id"
    callback_values = [
        captured[0][1]["json"][key] for key in ("callback_url", "reply_url", "response_url")
    ]
    assert callback_values[0] == callback_values[1] == callback_values[2]
    assert callback_values[0].startswith(main.PUBLIC_BASE_URL + "/callbacks/")

    callback_path = urlparse(result["callback_url"]).path
    response = client.post(
        callback_path, json={"ok": True, "answer": "done", "run_id": result["run_id"]}
    )
    assert response.status_code == 200
    run = json.loads(asyncio.run(main.get_grokbot_run(result["run_id"])))
    assert run["status"] == "answered"
    assert run["answer"]["answer"] == "done"
    assert run["answer_text"] == "done"
    assert client.post(
        callback_path, json={"ok": True, "run_id": result["run_id"]}
    ).status_code == 409
    assert client.post(
        "/callbacks/unknown-token", json={"run_id": str(uuid.uuid4())}
    ).status_code == 404


def test_ask_grokbot_preserves_caller_callback_url(monkeypatch):
    captured = {}

    async def fake_post(_self, url, **kwargs):
        captured.update(kwargs["json"])
        return httpx.Response(
            200,
            json={"success": True},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(main.httpx.AsyncClient, "post", fake_post)
    asyncio.run(main.ask_grokbot({
        "callback_url": "https://example.com/callback",
        "reply_url": "https://example.com/reply",
        "response_url": "https://example.com/response",
    }, wait_seconds=0))
    assert captured["callback_url"] == "https://example.com/callback"
    assert captured["reply_url"] == "https://example.com/reply"
    assert captured["response_url"] == "https://example.com/response"


def test_overlapping_grokbot_runs_require_matching_uuid(client, monkeypatch):
    async def fake_post(_self, url, **kwargs):
        return httpx.Response(
            200,
            json={"success": True},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(main.httpx.AsyncClient, "post", fake_post)
    result_a = json.loads(asyncio.run(
        main.ask_grokbot({"prompt": "A"}, wait_seconds=0)
    ))
    result_b = json.loads(asyncio.run(
        main.ask_grokbot({"prompt": "B"}, wait_seconds=0)
    ))
    path_a = urlparse(result_a["callback_url"]).path
    path_b = urlparse(result_b["callback_url"]).path

    response = client.post(path_b, json={
        "ok": True, "answer": "B", "run_id": result_b["run_id"],
    })
    assert response.status_code == 200
    assert json.loads(asyncio.run(main.get_grokbot_run(result_a["run_id"])))["status"] == "pending"
    assert json.loads(asyncio.run(main.get_grokbot_run(result_b["run_id"])))["answer"]["answer"] == "B"

    response = client.post(path_a, json={
        "ok": True, "answer": "wrong", "run_id": result_b["run_id"],
    })
    assert response.status_code == 400
    assert response.json()["error"] == "run_id_mismatch"
    assert json.loads(asyncio.run(main.get_grokbot_run(result_a["run_id"])))["status"] == "pending"
    response = client.post(path_a, json={"ok": True, "answer": "missing"})
    assert response.status_code == 400
    assert response.json()["error"] == "run_id_required"
    response = client.post(path_a, json={
        "ok": True, "answer": "A", "run_id": result_a["run_id"],
    })
    assert response.status_code == 200
    assert json.loads(asyncio.run(main.get_grokbot_run(result_a["run_id"])))["answer"]["answer"] == "A"


def test_tokenless_grokbot_callback_resolution(client, monkeypatch):
    async def fake_post(_self, url, **kwargs):
        return httpx.Response(
            200,
            json={"success": True},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(main.httpx.AsyncClient, "post", fake_post)
    result = json.loads(asyncio.run(
        main.ask_grokbot({"prompt": "C"}, wait_seconds=0)
    ))
    response = client.post("/callbacks", json={
        "request_id": result["run_id"], "ok": True, "answer": "C",
    })
    assert response.status_code == 200
    assert response.json()["run_id"] == result["run_id"]
    assert json.loads(asyncio.run(main.get_grokbot_run(result["run_id"])))["status"] == "answered"
    assert client.post("/callbacks", json={"run_id": str(uuid.uuid4())}).status_code == 404
    assert client.post("/callbacks", json={
        "run_id": result["run_id"], "ok": True, "answer": "again",
    }).status_code == 409


def test_concurrent_grokbot_callbacks_are_correlated(client, monkeypatch):
    async def fake_post(_self, url, **kwargs):
        return httpx.Response(
            200,
            json={"success": True},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(main.httpx.AsyncClient, "post", fake_post)
    results = [
        json.loads(asyncio.run(
            main.ask_grokbot({"prompt": str(index)}, wait_seconds=0)
        ))
        for index in range(20)
    ]

    def post_callback(result):
        path = urlparse(result["callback_url"]).path
        return client.post(path, json={
            "ok": True, "answer": result["run_id"], "run_id": result["run_id"],
        })

    with ThreadPoolExecutor(max_workers=20) as executor:
        responses = list(executor.map(post_callback, results))
    assert all(response.status_code == 200 for response in responses)
    for result in results:
        run = json.loads(asyncio.run(main.get_grokbot_run(result["run_id"])))
        assert run["status"] == "answered"
        assert run["answer"]["answer"] == result["run_id"]


def test_ask_grokbot_waits_for_callback_event(client, monkeypatch):
    captured = {}
    errors = []

    async def fake_post(_self, url, **kwargs):
        captured.update(kwargs["json"])
        return httpx.Response(
            200,
            json={"success": True},
            request=httpx.Request("POST", url),
        )

    def post_later():
        try:
            deadline = time.monotonic() + 2
            while "callback_url" not in captured and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.5)
            path = urlparse(captured["callback_url"]).path
            client.post(path, json={
                "ok": True,
                "answer": "A1",
                "message": "A1",
                "run_id": captured["run_id"],
            })
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(main.httpx.AsyncClient, "post", fake_post)
    thread = threading.Thread(target=post_later)
    thread.start()
    started = time.monotonic()
    result = json.loads(asyncio.run(
        main.ask_grokbot({"prompt": "wait"}, wait_seconds=5)
    ))
    elapsed = time.monotonic() - started
    thread.join(timeout=2)
    assert not errors
    assert result["answer_status"] == "answered"
    assert result["answer_text"] == "A1"
    assert elapsed < 4


def test_extract_answer_text():
    assert main._extract_answer_text({"answer": "x"}) == "x"
    assert main._extract_answer_text({"message": "m"}) == "m"
    assert main._extract_answer_text({
        "content": [
            {"type": "text", "text": "a"},
            {"type": "text", "text": "b"},
        ],
    }) == "a\nb"
    assert main._extract_answer_text({"text": "t"}) == "t"
    assert main._extract_answer_text("plain") == "plain"
    assert main._extract_answer_text({"ok": True}) is None


def test_ask_grokbot_pending_summary(client, monkeypatch):
    async def fake_post(_self, url, **kwargs):
        return httpx.Response(
            200,
            json={"success": True},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(main.httpx.AsyncClient, "post", fake_post)
    result = json.loads(asyncio.run(
        main.ask_grokbot({"prompt": "pending"}, wait_seconds=1)
    ))
    assert result["answer_status"] == "pending"
    assert "wait_for_grokbot_answer" in result["summary"]
    assert result["run_id"] in result["summary"]
