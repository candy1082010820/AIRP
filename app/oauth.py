"""Persistent, consent-based OAuth for the read-only MCP connector.

The MCP SDK owns discovery, registration, PKCE validation and OAuth endpoints.
This provider stores hashed tokens and encrypted client metadata in SQLite.
"""
import base64
import hashlib
import html
import json
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    RefreshToken, RegistrationError, TokenError, construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.responses import HTMLResponse, RedirectResponse

from app.config import Settings
from app.db import Store

READ_SCOPE = "airp:read"
FLOW_SECONDS = 600
ACCESS_SECONDS = 3600
REFRESH_SECONDS = 30 * 86400
COOKIE = "airp_oauth_csrf"


def digest(value: str):
    return hashlib.sha256(value.encode()).hexdigest()


class ReadOnlyOAuthProvider:
    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings
        self.resource = settings.public_url + "/mcp"
        self.fingerprint = digest(json.dumps([settings.read_key, settings.public_url]))
        key = hashlib.sha256(b"AIRP OAuth client encryption\0" + settings.read_key.encode()).digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(key))

    def initialize(self):
        with self.store.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS airp_oauth_clients (
                    id TEXT PRIMARY KEY, encrypted_metadata TEXT NOT NULL, fingerprint TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS airp_oauth_flows (
                    id TEXT PRIMARY KEY, client_id TEXT NOT NULL, payload TEXT NOT NULL,
                    expires_at REAL NOT NULL, csrf_hash TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    fingerprint TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS airp_oauth_tokens (
                    token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL,
                    payload TEXT NOT NULL, expires_at REAL NOT NULL,
                    family_id TEXT NOT NULL, fingerprint TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS airp_oauth_token_family ON airp_oauth_tokens(family_id);
            """)

    def cleanup(self, conn):
        stamp = time.time()
        conn.execute("DELETE FROM airp_oauth_flows WHERE expires_at<? OR fingerprint!=?", (stamp, self.fingerprint))
        conn.execute("DELETE FROM airp_oauth_tokens WHERE expires_at<? OR fingerprint!=?", (stamp, self.fingerprint))
        conn.execute("DELETE FROM airp_oauth_clients WHERE fingerprint!=?", (self.fingerprint,))

    async def get_client(self, client_id):
        with self.store.connect() as conn:
            row = conn.execute("SELECT encrypted_metadata FROM airp_oauth_clients WHERE id=? AND fingerprint=?", (client_id, self.fingerprint)).fetchone()
        if not row:
            return None
        try:
            return OAuthClientInformationFull.model_validate_json(self.cipher.decrypt(row[0].encode()))
        except (InvalidToken, ValueError):
            return None

    async def register_client(self, client_info):
        if not client_info.redirect_uris:
            raise RegistrationError("invalid_client_metadata", "At least one redirect URI is required")
        if client_info.token_endpoint_auth_method not in {"none", "client_secret_post", "client_secret_basic"}:
            raise RegistrationError("invalid_client_metadata", "Unsupported authentication method")
        for redirect in client_info.redirect_uris or []:
            url = urlsplit(str(redirect))
            loopback = url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
            if (url.scheme != "https" and not loopback) or not url.hostname or url.username or url.password or url.fragment:
                raise RegistrationError("invalid_redirect_uri", "Redirects require HTTPS, or HTTP loopback for native clients")
        encrypted = self.cipher.encrypt(client_info.model_dump_json().encode()).decode()
        with self.store.connect(write=True) as conn:
            self.cleanup(conn)
            conn.execute("INSERT INTO airp_oauth_clients VALUES (?,?,?)", (client_info.client_id, encrypted, self.fingerprint))

    async def authorize(self, client, params: AuthorizationParams):
        scopes = params.scopes or [READ_SCOPE]
        if set(scopes) != {READ_SCOPE}:
            raise AuthorizeError("invalid_scope", "Only airp:read is supported")
        if params.resource not in (None, self.resource):
            raise AuthorizeError("invalid_request", "The resource must match this AIRP MCP server")
        if len(params.code_challenge) != 43:
            raise AuthorizeError("invalid_request", "A SHA-256 PKCE challenge is required")
        params = params.model_copy(update={"scopes": [READ_SCOPE], "resource": self.resource})
        flow_id = secrets.token_urlsafe(32)
        with self.store.connect(write=True) as conn:
            self.cleanup(conn)
            count = conn.execute("SELECT count(*) FROM airp_oauth_flows WHERE client_id=?", (client.client_id,)).fetchone()[0]
            if count >= 10:
                raise AuthorizeError("temporarily_unavailable", "Too many pending authorizations; retry after ten minutes")
            conn.execute("INSERT INTO airp_oauth_flows (id,client_id,payload,expires_at,fingerprint) VALUES (?,?,?,?,?)", (flow_id, client.client_id, params.model_dump_json(), time.time() + FLOW_SECONDS, self.fingerprint))
        return self.settings.public_url + "/oauth/consent?" + urlencode({"flow_id": flow_id})

    def flow(self, conn, flow_id):
        row = conn.execute("SELECT * FROM airp_oauth_flows WHERE id=? AND fingerprint=? AND expires_at>?", (flow_id, self.fingerprint, time.time())).fetchone()
        if not row or row["attempts"] >= 5:
            raise HTTPException(400, "授權已過期或嘗試次數過多，請返回外掛程式重新連線。")
        return row

    async def consent_page(self, flow_id):
        csrf = secrets.token_urlsafe(32)
        with self.store.connect(write=True) as conn:
            row = self.flow(conn, flow_id)
            conn.execute("UPDATE airp_oauth_flows SET csrf_hash=? WHERE id=?", (digest(csrf), flow_id))
        client = await self.get_client(row["client_id"])
        if not client:
            raise HTTPException(400, "應用程式授權資訊已失效，請重新連線。")
        params = AuthorizationParams.model_validate_json(row["payload"])
        response = self.render_consent(flow_id, csrf, client, params)
        response.set_cookie(COOKIE, csrf, max_age=FLOW_SECONDS, httponly=True, secure=True, samesite="lax", path="/oauth/consent")
        return response

    def render_consent(self, flow_id, csrf, client, params, error="", status_code=200):
        escape = html.escape
        name = escape(client.client_name or "MCP 應用程式")
        target = escape(urlsplit(str(params.redirect_uri)).netloc)
        error_html = f'<p class="error" role="alert">{escape(error)}</p>' if error else ""
        body = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1"><title>AIRP · 外掛程式授權</title>
        <link rel="stylesheet" href="/static/oauth.css"></head><body><main>
        <div class="brand">AIRP</div><h1>連接你的角色扮演資料庫</h1>
        <p><strong>{name}</strong> 想讀取你的 AIRP 資料。</p>
        <section><h2>本次授權範圍</h2><p>搜尋與讀取最新已發布的世界觀、角色、背景、情境與扮演規則。</p>
        <p>草稿、版本歷史與備份不會提供；外掛程式無法新增、修改或刪除資料。</p>
        <p class="destination">授權完成後返回：{target}</p></section>
        <form method="post" action="/oauth/consent">
        <input type="hidden" name="flow_id" value="{escape(flow_id)}">
        <input type="hidden" name="csrf" value="{escape(csrf)}">
        <label for="read-key">AIRP_READ_KEY 的值</label>
        <input id="read-key" name="read_key" type="password" required maxlength="512" autocomplete="off">
        <p class="hint">填入你在 Railway 保存的讀取金鑰。金鑰只送至 AIRP 核對，不會交給外掛程式；請勿輸入管理金鑰。</p>
        {error_html}<div class="actions"><button name="decision" value="deny" class="secondary" formnovalidate>取消</button>
        <button name="decision" value="approve">授權讀取已發布資料</button></div></form>
        </main></body></html>"""
        return HTMLResponse(body, status_code=status_code, headers={
            "Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
        })

    async def consent_submit(self, request: Request):
        expected_origin = urlsplit(self.settings.public_url)
        origin = expected_origin.scheme + "://" + expected_origin.netloc
        if request.headers.get("origin") not in (None, origin):
            raise HTTPException(403, "請從 AIRP 授權頁面提交。")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                raise HTTPException(413, "授權表單過大。")
        try:
            fields = parse_qs(body.decode(), keep_blank_values=True, max_num_fields=5)
            if any(len(value) != 1 for value in fields.values()):
                raise ValueError()
            values = {key: value[0] for key, value in fields.items()}
        except (ValueError, UnicodeError):
            raise HTTPException(400, "授權表單格式不正確。") from None
        flow_id, csrf = values.get("flow_id", ""), values.get("csrf", "")
        cookie = request.cookies.get(COOKIE, "")
        params, client_id, wrong_key = None, None, False
        with self.store.connect(write=True) as conn:
            row = self.flow(conn, flow_id)
            if not csrf or not cookie or not row["csrf_hash"] or not secrets.compare_digest(csrf.encode(), cookie.encode()) or not secrets.compare_digest(digest(csrf), row["csrf_hash"]):
                raise HTTPException(403, "授權頁面已失效，請重新開啟連線流程。")
            params = AuthorizationParams.model_validate_json(row["payload"])
            client_id = row["client_id"]
            decision = values.get("decision")
            if decision == "deny":
                conn.execute("DELETE FROM airp_oauth_flows WHERE id=?", (flow_id,))
                redirect = construct_redirect_uri(str(params.redirect_uri), error="access_denied", **({"state": params.state} if params.state is not None else {}))
            elif decision == "approve":
                key = values.get("read_key", "").strip()
                if not key or not secrets.compare_digest(key.encode(), self.settings.read_key.encode()):
                    conn.execute("UPDATE airp_oauth_flows SET attempts=attempts+1 WHERE id=?", (flow_id,))
                    wrong_key = True
                else:
                    code = secrets.token_urlsafe(32)
                    payload = AuthorizationCode(code=code, scopes=[READ_SCOPE], expires_at=time.time() + 180,
                        client_id=client_id, code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
                        redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                        resource=self.resource, subject="airp-owner")
                    self.save_token(conn, "code", code, payload, payload.expires_at, secrets.token_urlsafe(24))
                    conn.execute("DELETE FROM airp_oauth_flows WHERE id=?", (flow_id,))
                    redirect = construct_redirect_uri(str(params.redirect_uri), code=code, **({"state": params.state} if params.state is not None else {}))
            else:
                raise HTTPException(400, "請選擇授權或取消。")
        if wrong_key:
            client = await self.get_client(client_id)
            return self.render_consent(flow_id, csrf, client, params, "讀取金鑰不正確。請填 AIRP_READ_KEY 的值，並確認沒有使用管理金鑰。", 401)
        response = RedirectResponse(redirect, status_code=302, headers={"Cache-Control": "no-store"})
        response.delete_cookie(COOKIE, path="/oauth/consent", secure=True, httponly=True, samesite="lax")
        return response

    def save_token(self, conn, kind, token, model, expires_at, family):
        payload = model.model_dump_json(exclude={"token", "code"})
        conn.execute("INSERT INTO airp_oauth_tokens VALUES (?,?,?,?,?,?,?)", (digest(token), kind, model.client_id, payload, expires_at, family, self.fingerprint))

    def token_row(self, conn, kind, token, client_id=None):
        row = conn.execute("SELECT * FROM airp_oauth_tokens WHERE token_hash=? AND kind=? AND fingerprint=? AND expires_at>?", (digest(token), kind, self.fingerprint, time.time())).fetchone()
        if row and (client_id is None or row["client_id"] == client_id):
            return row
        return None

    async def load_authorization_code(self, client, authorization_code):
        with self.store.connect() as conn:
            row = self.token_row(conn, "code", authorization_code, client.client_id)
        return AuthorizationCode(code=authorization_code, **json.loads(row["payload"])) if row else None

    def issue_tokens(self, conn, client_id, scopes, family):
        access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        stamp = int(time.time())
        access_model = AccessToken(token=access, client_id=client_id, scopes=scopes, expires_at=stamp + ACCESS_SECONDS, resource=self.resource, subject="airp-owner")
        refresh_model = RefreshToken(token=refresh, client_id=client_id, scopes=scopes, expires_at=stamp + REFRESH_SECONDS, resource=self.resource, subject="airp-owner")
        self.save_token(conn, "access", access, access_model, access_model.expires_at, family)
        self.save_token(conn, "refresh", refresh, refresh_model, refresh_model.expires_at, family)
        return OAuthToken(access_token=access, refresh_token=refresh, token_type="Bearer", expires_in=ACCESS_SECONDS, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        with self.store.connect(write=True) as conn:
            row = self.token_row(conn, "code", authorization_code.code, client.client_id)
            if not row:
                raise TokenError("invalid_grant", "Authorization code expired or already used")
            conn.execute("DELETE FROM airp_oauth_tokens WHERE token_hash=?", (digest(authorization_code.code),))
            return self.issue_tokens(conn, client.client_id, authorization_code.scopes, row["family_id"])

    async def load_refresh_token(self, client, refresh_token):
        with self.store.connect() as conn:
            row = self.token_row(conn, "refresh", refresh_token, client.client_id)
        return RefreshToken(token=refresh_token, **json.loads(row["payload"])) if row else None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        if set(scopes) != {READ_SCOPE}:
            raise TokenError("invalid_scope", "Only airp:read is supported")
        with self.store.connect(write=True) as conn:
            row = self.token_row(conn, "refresh", refresh_token.token, client.client_id)
            if not row:
                raise TokenError("invalid_grant", "Refresh token expired or already used")
            conn.execute("DELETE FROM airp_oauth_tokens WHERE family_id=?", (row["family_id"],))
            return self.issue_tokens(conn, client.client_id, scopes, row["family_id"])

    async def load_access_token(self, token):
        if secrets.compare_digest(token.encode(), self.settings.read_key.encode()):
            return AccessToken(token=token, client_id="static-read-key", scopes=[READ_SCOPE], resource=self.resource)
        with self.store.connect() as conn:
            row = self.token_row(conn, "access", token)
        return AccessToken(token=token, **json.loads(row["payload"])) if row else None

    async def revoke_token(self, token):
        with self.store.connect(write=True) as conn:
            row = conn.execute("SELECT family_id FROM airp_oauth_tokens WHERE token_hash=?", (digest(token.token),)).fetchone()
            if row:
                conn.execute("DELETE FROM airp_oauth_tokens WHERE family_id=?", (row["family_id"],))
