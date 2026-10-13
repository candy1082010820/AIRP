import base64
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

ADMIN = {"Authorization": "Bearer admin-test-key"}
REDIRECT = "https://chatgpt.com/connector_platform/oauth/callback"
VERIFIER = "a-valid-pkce-verifier-that-is-at-least-forty-three-characters-long"
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=tmp_path / "mcp.sqlite3", admin_key="admin-test-key", read_key="read-test-key", public_url="https://example.com")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url="https://example.com") as client:
        yield client


def register(client, **overrides):
    response = client.post("/register", json={"client_name": "ChatGPT", "redirect_uris": [REDIRECT], **overrides})
    assert response.status_code == 201
    return response.json()


def begin(client, app, **overrides):
    response = client.get("/authorize", params={"client_id": app["client_id"], "response_type": "code", "redirect_uri": REDIRECT,
        "scope": "airp:read", "state": "my-game-session", "code_challenge": CHALLENGE, "code_challenge_method": "S256",
        "resource": "https://example.com/mcp", **overrides}, follow_redirects=False)
    assert response.status_code == 302
    page = client.get(response.headers["location"])
    assert page.status_code == 200
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    flow_id = re.search(r'name="flow_id" value="([^"]+)"', page.text).group(1)
    return {"flow_id": flow_id, "csrf": csrf}, page


def consent(client, form, **overrides):
    return client.post("/oauth/consent", data={**form, "read_key": "read-test-key", "decision": "approve", **overrides}, follow_redirects=False)


def grant(client, app):
    form, _ = begin(client, app)
    response = consent(client, form)
    assert response.status_code == 302
    callback = parse_qs(urlsplit(response.headers["location"]).query)
    assert callback["state"] == ["my-game-session"]
    return callback["code"][0]


def token_form(app, **overrides):
    fields = {"client_id": app["client_id"], **overrides}
    if app.get("client_secret"):
        fields["client_secret"] = app["client_secret"]
    return fields


def exchange(client, app, code, **overrides):
    values = {"grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER, "redirect_uri": REDIRECT, **overrides}
    return client.post("/token", data=token_form(app, **values))


def connect(client):
    app = register(client)
    response = exchange(client, app, grant(client, app))
    assert response.status_code == 200
    return app, response.json()


def rpc(client, method, params=None, token="read-test-key", **headers):
    return client.post("/mcp", headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-03-26", **headers},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def create(client, **overrides):
    response = client.post("/api/entries", json={"kind": "world", "title": "霍格華茲模擬器", "content": "最初設定", "status": "published", **overrides}, headers=ADMIN)
    assert response.status_code == 201
    return response.json()


def test_discovery_and_readonly_mcp_tools(client):
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["registration_endpoint"] == "https://example.com/register"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == "https://example.com/mcp"
    assert resource["scopes_supported"] == ["airp:read"]
    denied = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert denied.status_code == 401
    assert "resource_metadata" in denied.headers["www-authenticate"]
    assert rpc(client, "tools/list", token="admin-test-key").status_code == 401
    response = rpc(client, "tools/list")
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    assert {tool["name"] for tool in tools} == {"search_entries", "get_entry", "get_roleplay_context"}
    assert all(tool["annotations"]["readOnlyHint"] is True and tool["annotations"]["destructiveHint"] is False for tool in tools)


def test_oauth_connection_reads_latest_published_data_but_never_drafts(client):
    world = create(client)
    draft = create(client, title="秘密草稿", content="尚未公開的內容", status="draft")
    _, tokens = connect(client)
    token = tokens["access_token"]
    assert tokens["scope"] == "airp:read"
    result = rpc(client, "tools/call", {"name": "search_entries", "arguments": {}}, token=token).json()["result"]
    assert result["structuredContent"]["total"] == 1
    assert "尚未公開" not in str(result)
    denied = rpc(client, "tools/call", {"name": "get_entry", "arguments": {"entry_id": draft["id"]}}, token=token).json()["result"]
    assert denied["isError"] is True
    assert "尚未公開" not in str(denied)
    updated = client.put("/api/entries/" + world["id"], headers=ADMIN, json={"kind": "world", "title": world["title"], "content": "設定已更新", "status": "published", "expected_revision": 1})
    assert updated.status_code == 200
    fetched = rpc(client, "tools/call", {"name": "get_entry", "arguments": {"entry_id": world["id"]}}, token=token).json()["result"]["structuredContent"]
    assert fetched["revision"] == 2 and fetched["content"] == "設定已更新"
    context = rpc(client, "tools/call", {"name": "get_roleplay_context", "arguments": {"world_id": world["id"]}}, token=token).json()["result"]["structuredContent"]
    assert "設定已更新" in context["context"]
    assert "秘密草稿" not in context["context"]
    assert client.post("/api/entries", headers={"Authorization": "Bearer " + token}, json={"kind": "lore", "title": "非法寫入"}).status_code == 401


def test_consent_requires_read_key_and_csrf_and_allows_cancel(client):
    app = register(client)
    form, page = begin(client, app)
    assert "AIRP_READ_KEY" in page.text and app["client_secret"] not in page.text
    assert consent(client, form, read_key="admin-test-key").status_code == 401
    assert consent(client, {**form, "csrf": "forged"}).status_code == 403
    assert client.post("/oauth/consent", data={**form, "decision": "approve", "read_key": "read-test-key"}, headers={"Origin": "https://evil.example"}).status_code == 403
    response = consent(client, form, decision="deny", read_key="")
    assert response.status_code == 302
    callback = parse_qs(urlsplit(response.headers["location"]).query)
    assert callback["error"] == ["access_denied"] and callback["state"] == ["my-game-session"]
    assert consent(client, form).status_code == 400
    with client.app.state.store.connect() as db:
        assert db.execute("SELECT count(*) FROM airp_oauth_tokens").fetchone()[0] == 0


def test_bad_pkce_bad_redirect_and_replayed_codes_are_rejected(client):
    app = register(client)
    code = grant(client, app)
    assert exchange(client, app, code, code_verifier="wrong-verifier").status_code == 400
    assert exchange(client, app, code, redirect_uri="https://evil.example/callback").status_code == 400
    assert exchange(client, app, code).status_code == 200
    assert exchange(client, app, code).status_code == 400
    other = register(client)
    other_code = grant(client, app)
    assert exchange(client, other, other_code).status_code == 400


def test_concurrent_code_exchange_only_issues_one_grant(client):
    app = register(client)
    code = grant(client, app)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: exchange(client, app, code), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 400]


def test_tokens_clients_and_refresh_survive_restart_and_rotate(settings):
    with TestClient(create_app(settings), base_url="https://example.com") as original:
        app, tokens = connect(original)
    with TestClient(create_app(settings), base_url="https://example.com") as restarted:
        assert rpc(restarted, "tools/list", token=tokens["access_token"]).status_code == 200
        refreshed = restarted.post("/token", data=token_form(app, grant_type="refresh_token", refresh_token=tokens["refresh_token"]))
        assert refreshed.status_code == 200
        fresh = refreshed.json()
        assert fresh["refresh_token"] != tokens["refresh_token"]
        assert rpc(restarted, "tools/list", token=fresh["access_token"]).status_code == 200
        assert rpc(restarted, "tools/list", token=tokens["access_token"]).status_code == 401
        assert restarted.post("/token", data=token_form(app, grant_type="refresh_token", refresh_token=tokens["refresh_token"])).status_code == 400
        assert restarted.post("/token", data=token_form(app, grant_type="refresh_token", refresh_token=fresh["refresh_token"], scope="airp:write")).status_code == 400


def test_revoke_removes_access_and_refresh_and_storage_hides_credentials(client):
    app, tokens = connect(client)
    with client.app.state.store.connect() as db:
        raw_tokens = str([dict(row) for row in db.execute("SELECT * FROM airp_oauth_tokens")])
        raw_client = db.execute("SELECT encrypted_metadata FROM airp_oauth_clients").fetchone()[0]
    assert tokens["access_token"] not in raw_tokens and tokens["refresh_token"] not in raw_tokens
    assert app["client_secret"] not in raw_client and "read-test-key" not in raw_client
    backup = client.get("/api/export", headers=ADMIN).json()
    assert set(backup) == {"format", "exported_at", "entries", "histories"}
    revoked = client.post("/revoke", data=token_form(app, token=tokens["refresh_token"]))
    assert revoked.status_code == 200
    assert rpc(client, "tools/list", token=tokens["access_token"]).status_code == 401
    assert client.post("/token", data=token_form(app, grant_type="refresh_token", refresh_token=tokens["refresh_token"])).status_code == 400


def test_rotating_read_key_invalidates_existing_connections(settings):
    with TestClient(create_app(settings), base_url="https://example.com") as old:
        app, tokens = connect(old)
    changed = Settings(db_path=settings.db_path, admin_key=settings.admin_key, read_key="new-read-test-key", public_url=settings.public_url)
    with TestClient(create_app(changed), base_url="https://example.com") as new:
        assert rpc(new, "tools/list", token=tokens["access_token"]).status_code == 401
        assert rpc(new, "tools/list", token="read-test-key").status_code == 401
        assert rpc(new, "tools/list", token="new-read-test-key").status_code == 200
        assert new.post("/token", data=token_form(app, grant_type="refresh_token", refresh_token=tokens["refresh_token"])).status_code == 401


def test_untrusted_redirect_resource_and_metadata_are_handled(client):
    assert client.post("/register", json={"redirect_uris": None}).status_code == 400
    response = client.post("/register", json={"redirect_uris": ["http://evil.example/callback"]})
    assert response.status_code == 400
    app = register(client, client_name='<img src=x onerror="alert(1)">')
    _, page = begin(client, app)
    assert '<img src=x' not in page.text and '&lt;img' in page.text
    bad = client.get("/authorize", params={"client_id": app["client_id"], "response_type": "code", "redirect_uri": REDIRECT,
        "code_challenge": CHALLENGE, "code_challenge_method": "S256", "resource": "https://another.example/mcp"}, follow_redirects=False)
    assert bad.status_code == 302 and "error=invalid_request" in bad.headers["location"]
    bad_redirect = client.get("/authorize", params={"client_id": app["client_id"], "response_type": "code", "redirect_uri": "https://evil.example/callback",
        "code_challenge": CHALLENGE, "code_challenge_method": "S256"}, follow_redirects=False)
    assert bad_redirect.status_code == 400


def test_local_mode_does_not_expose_an_unauthenticated_mcp_server(tmp_path):
    with TestClient(create_app(Settings(db_path=tmp_path / "local.sqlite3"))) as local:
        assert local.get("/mcp/info").json()["enabled"] is False
        assert local.get("/mcp").status_code == 404


def test_official_mcp_client_discovers_oauth_authorizes_and_reads(settings):
    """Exercise the real client transport and OAuth discovery, not just JSON-RPC requests."""
    import anyio
    import httpx
    from mcp import ClientSession
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.auth import OAuthClientMetadata
    from app.models import EntryInput

    class MemoryStorage:
        tokens = None
        client_info = None

        async def get_tokens(self): return self.tokens
        async def set_tokens(self, value): self.tokens = value
        async def get_client_info(self): return self.client_info
        async def set_client_info(self, value): self.client_info = value

    async def run():
        app = create_app(settings)
        storage = MemoryStorage()
        callback = None
        async with app.router.lifespan_context(app):
            saved = app.state.store.create(EntryInput(kind="world", title="霍格華茲", content="七年魔法學校生活", status="published"))
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=settings.public_url) as browser:
                async def redirect_handler(url):
                    nonlocal callback
                    redirect = await browser.get(url, follow_redirects=False)
                    assert redirect.status_code == 302
                    page = await browser.get(redirect.headers["location"])
                    assert page.status_code == 200
                    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
                    flow_id = re.search(r'name="flow_id" value="([^"]+)"', page.text).group(1)
                    response = await browser.post("/oauth/consent", data={"flow_id": flow_id, "csrf": csrf, "decision": "approve", "read_key": settings.read_key}, follow_redirects=False)
                    assert response.status_code == 302
                    params = parse_qs(urlsplit(response.headers["location"]).query)
                    callback = (params["code"][0], params["state"][0])

                async def callback_handler(): return callback

                oauth = OAuthClientProvider(server_url=settings.public_url + "/mcp",
                    client_metadata=OAuthClientMetadata(client_name="SDK client", redirect_uris=[REDIRECT], token_endpoint_auth_method="client_secret_post"),
                    storage=storage, redirect_handler=redirect_handler, callback_handler=callback_handler)
                async with httpx.AsyncClient(transport=transport, auth=oauth) as http:
                    async with streamable_http_client(settings.public_url + "/mcp", http_client=http) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            assert {tool.name for tool in tools.tools} == {"search_entries", "get_entry", "get_roleplay_context"}
                            result = await session.call_tool("get_entry", {"entry_id": saved["id"]})
                            assert result.isError is False
                            assert result.structuredContent["content"] == "七年魔法學校生活"
                            assert storage.tokens and storage.client_info

    anyio.run(run)
