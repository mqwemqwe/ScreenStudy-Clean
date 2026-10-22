"""User-authorized ChatGPT plan access via public OAuth and Responses endpoints.

No browser cookies, API-key fallback, hosted tools, or ChatGPT backend routes.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import re
import secrets
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import __version__

ISSUER = "https://auth.openai.com"
API = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
USAGE_URL = "https://chatgpt.com/settings/usage"
ALGORITHMS = ("RS256", "ES256")
TERMINAL_REFRESH = {"invalid_grant", "invalid_refresh_token", "token_expired",
                    "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused"}


class ChatGPTError(ValueError):
    def __init__(self, message, code="", status=None):
        super().__init__(message)
        self.code, self.status = code, status


def provider_error(data, status=None, request_id="", secrets_to_hide=()):
    if not isinstance(data, dict):
        data = {}
    error = data.get("error", data)
    if isinstance(error, str):
        error = {"code": error, "message": data.get("error_description", "")}
    if not isinstance(error, dict):
        error = {}
    code = str(error.get("code") or error.get("type") or "")
    message = error.get("message") or data.get("detail") or "OpenAI не выполнила запрос."
    hints = {
        "subscription_sharing_user_not_eligible": "Подписка недоступна для этого аккаунта, пространства или политики OpenAI.",
        "subscription_sharing_usage_limit_exceeded": "Достигнут лимит подписки или приложения. Откройте «Использование ChatGPT».",
        "subscription_sharing_usage_unavailable": "OpenAI временно не может проверить доступный объём подписки. Повторите позже.",
        "subscription_sharing_unsupported_capability": "Модель или параметр не поддерживается через подписку. Выберите другую доступную модель.",
        "subscription_sharing_route_not_supported": "OpenAI не разрешила этот способ подключения для аккаунта.",
        "subscription_sharing_invalid_user": "Войдите в ChatGPT заново: сеанс не подтверждён.",
    }
    if code in TERMINAL_REFRESH:
        message = "Сеанс ChatGPT завершён. Войдите заново."
    else:
        message = hints.get(code, message)
    detail = f"ChatGPT{f' · HTTP {status}' if status else ''}: {message}"
    if code:
        detail += f" [{code}]"
    if request_id:
        detail += f" · запрос {request_id}"
    for secret in secrets_to_hide:
        if isinstance(secret, str) and secret:
            detail = detail.replace(secret, "[скрыто]")
    detail = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [скрыто]", detail)
    detail = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[скрыто]", detail)
    detail = re.sub(r"\b(?:gsk_|sk-)[A-Za-z0-9_-]+", "[скрыто]", detail)
    return ChatGPTError(" ".join(detail.split())[:900], code, status)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise ChatGPTError("OpenAI вернула перенаправление. Запрос остановлен; повторите позже.")


def trusted_url(url, origin):
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or p.netloc != urllib.parse.urlsplit(origin).netloc or p.fragment:
        raise ChatGPTError("Недопустимый адрес сервиса OpenAI.")
    return url


def open_request(request, timeout=45, secrets_to_hide=()):
    import certifi
    context = ssl.create_default_context(cafile=certifi.where())
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(16_384)
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                data = {}
            request_id = (exc.headers.get("x-request-id") or exc.headers.get("openai-request-id", "")) if exc.headers else ""
            raise provider_error(data, exc.code, request_id, secrets_to_hide) from None
        finally:
            exc.close()
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ChatGPTError("Нет соединения с OpenAI. Проверьте интернет и повторите позже.") from None


def json_request(url, *, form=None, token=None):
    origin = API if token else ISSUER
    trusted_url(url, origin)
    headers = {"Accept": "application/json", "User-Agent": f"ScreenStudy/{__version__}"}
    data = None
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form).encode()
    if token:
        headers["Authorization"] = "Bearer " + token
    hidden = [token] + list((form or {}).values())
    with open_request(urllib.request.Request(url, data, headers), secrets_to_hide=hidden) as response:
        raw = response.read(2_000_001)
    if not raw:  # Successful revocation returns an empty body.
        return {}
    if len(raw) > 2_000_000:
        raise ChatGPTError("Ответ OpenAI слишком большой.")
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeDecodeError):
        raise ChatGPTError("OpenAI вернула ответ неподходящего формата.") from None


def atomic_json(path, value):
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_identity(encoded, client_id, nonce, config, jwks):
    import jwt
    try:
        header = jwt.get_unverified_header(encoded)
        # The allowed algorithms are fixed locally, never taken from a token.
        allowed = [a for a in ALGORITHMS if a in config.get("id_token_signing_alg_values_supported", ALGORITHMS)]
        if header.get("alg") not in allowed or not header.get("kid"):
            raise ValueError()
        keys = [k for k in jwks.get("keys", []) if k.get("kid") == header["kid"]
                and k.get("use", "sig") == "sig" and k.get("kty") in ("RSA", "EC")]
        if len(keys) != 1:
            raise ValueError()
        key = jwt.PyJWK.from_dict(keys[0])
        if key.algorithm_name not in allowed or key.algorithm_name != header["alg"]:
            raise ValueError()
        claims = jwt.decode(encoded, key.key, algorithms=allowed,
                            audience=client_id, issuer=ISSUER, leeway=5,
                            options={"require": ["sub", "iss", "aud", "exp", "iat", "nonce"]})
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise ValueError()
        if not secrets.compare_digest(str(claims.get("nonce", "")).encode(), nonce.encode()):
            raise ValueError()
        if "azp" in claims and claims["azp"] != client_id:
            raise ValueError()
        if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != client_id:
            raise ValueError()
        return claims
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        raise ChatGPTError("Не удалось подтвердить подлинность входа ChatGPT. Повторите вход.") from None


class LoginAttempt:
    """One-use, loopback-only callback. All callback data stays out of logs."""
    def __init__(self, host_id, config, profile=None):
        self.profile = copy.deepcopy(profile or {})
        self.state, self.nonce, self.verifier = (secrets.token_urlsafe(32) for _ in range(3))
        self.result = None
        attempt = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(3)

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                expected_host = f"127.0.0.1:{self.server.server_port}"
                accepted = False
                if (len(self.path) < 16_384 and parsed.path == "/auth/callback"
                        and self.headers.get("Host") == expected_host and attempt.result is None):
                    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                    if all(len(v) == 1 for v in params.values()):
                        values = {k: v[0] for k, v in params.items()}
                        if secrets.compare_digest(values.get("state", "").encode(), attempt.state.encode()):
                            attempt.result, accepted = values, True
                body = ("Вернитесь в ScreenStudy: приложение проверяет вход."
                        if accepted else "Этот запрос входа недействителен. Вернитесь в ScreenStudy.").encode()
                self.send_response(200 if accepted else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'none'")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.server.timeout = .3
        self.redirect_uri = f"http://127.0.0.1:{self.server.server_port}/auth/callback"
        params = {"client_id": self.profile.get("client_id") or "dynamic_agent_client",
                  "ext_agent_host_id": host_id, "response_type": "code",
                  "redirect_uri": self.redirect_uri, "scope": SCOPES, "resource": API,
                  "state": self.state, "nonce": self.nonce, "code_challenge_method": "S256",
                  "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip("=")}
        if self.profile:
            if self.profile.get("id_token"):
                params["id_token_hint"] = self.profile["id_token"]
            if self.profile.get("email"):
                params["login_hint"] = self.profile["email"]
            if not permitted(self.profile):
                params["prompt"] = "consent"
        else:
            params["agent_name_hint"] = "ScreenStudy"
        self.url = trusted_url(config["authorization_endpoint"], ISSUER) + "?" + urllib.parse.urlencode(params)

    def wait(self, cancel, timeout=180):
        deadline = time.monotonic() + timeout
        try:
            while self.result is None and not cancel.is_set() and time.monotonic() < deadline:
                self.server.handle_request()
            if cancel.is_set():
                raise ChatGPTError("Вход отменён.")
            if self.result is None:
                raise ChatGPTError("Время входа истекло. Нажмите «Continue with ChatGPT» ещё раз.")
            if self.result.get("error"):
                raise ChatGPTError("Вход или разрешение отменены в браузере. Можно повторить вход.")
            client_id = self.result.get("client_id") or self.profile.get("client_id")
            if (not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{1,256}", client_id)
                    or client_id == "dynamic_agent_client"
                    or not self.result.get("code")):
                raise ChatGPTError("OpenAI не завершила регистрацию приложения. Повторите вход.")
            if self.profile and client_id != self.profile["client_id"]:
                raise ChatGPTError("Аккаунт входа не соответствует выбранному подключению.")
            return client_id, self.result["code"]
        finally:
            self.server.server_close()


def permitted(profile):
    return PLAN_SCOPE in profile.get("scopes", []) and "resource.invoke" in profile.get("scopes", [])


class Manager:
    def __init__(self, root=None):
        self.root = Path(root) if root else Path.home()/"Library/Application Support/ScreenStudy"
        self.path = self.root/"chatgpt-accounts.json"
        self._mutex = threading.RLock()

    @contextmanager
    def locked(self):
        import fcntl  # macOS and Linux; serializes rotating refreshes across processes.
        with self._mutex:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root.chmod(0o700)
            fd = os.open(self.root/"chatgpt.lock", os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(fd, "a") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self):
        if not self.path.exists():
            return {"profiles": {}}
        try:
            result = json.loads(self.path.read_text("utf-8"))
            if not isinstance(result.get("profiles"), dict):
                raise ValueError()
            return result
        except (ValueError, AttributeError):
            raise ChatGPTError("Файл подключений ChatGPT повреждён. Существующий файл не изменён.") from None

    def host_id(self):
        with self.locked():
            path = self.root/"chatgpt-host.json"
            if path.exists():
                try:
                    host = json.loads(path.read_text("utf-8"))["host_id"]
                    if not host.startswith("urn:uuid:"):
                        raise ValueError()
                    uuid.UUID(host[9:])
                    return host
                except (ValueError, KeyError, TypeError, AttributeError):
                    raise ChatGPTError("Не удалось прочитать идентификатор установки ChatGPT.") from None
            host = "urn:uuid:" + str(uuid.uuid4())
            atomic_json(path, {"host_id": host})
            return host

    def profiles(self):
        # Atomic file replacement makes read-only snapshots safe without waiting
        # for a network refresh in another thread/process (which would freeze UI).
        return [{"id": key, "email": p.get("email", ""), "connected": bool(p.get("access_token")),
                 "permitted": permitted(p)} for key, p in self._read()["profiles"].items()]

    def ready(self, account):
        return any(p["id"] == account and p["connected"] and p["permitted"] for p in self.profiles())

    def discovery(self):
        config = json_request(ISSUER + "/.well-known/openid-configuration")
        if config.get("issuer") != ISSUER:
            raise ChatGPTError("Неожиданный издатель авторизации OpenAI.")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            trusted_url(config.get(key, ""), ISSUER)
        return config

    def connect(self, account="", cancel=None, browser_open=None):
        cancel = cancel or threading.Event()
        browser_open = browser_open or webbrowser.open
        with self.locked():
            old = copy.deepcopy(self._read()["profiles"].get(account)) if account else None
        if account and not old:
            raise ChatGPTError("Выбранное подключение больше не существует.")
        config = self.discovery()
        attempt = LoginAttempt(self.host_id(), config, old)
        try:
            if cancel.is_set():
                raise ChatGPTError("Вход отменён.")
            if not browser_open(attempt.url):
                raise ChatGPTError("Не удалось открыть браузер. Откройте браузер по умолчанию и повторите вход.")
            client_id, code = attempt.wait(cancel)
        finally:
            attempt.server.server_close()
        tokens = json_request(config["token_endpoint"], form={"grant_type": "authorization_code",
            "client_id": client_id, "code": code, "code_verifier": attempt.verifier,
            "redirect_uri": attempt.redirect_uri, "resource": API})
        claims = validate_identity(tokens.get("id_token", ""), client_id, attempt.nonce,
                                   config, json_request(config["jwks_uri"]))
        if old and claims["sub"] != old["subject"]:
            raise ChatGPTError("Вход выполнен в другой аккаунт. Добавьте его отдельным подключением.")
        record = self.token_record(tokens)
        record.update(client_id=client_id, subject=claims["sub"], email=str(claims.get("email") or ""),
                      welcomed=bool(old and old.get("welcomed")))
        if cancel.is_set():
            # User closed the dialog during the network exchange. Do not activate it.
            try:
                self.revoke(record, config)
            except ChatGPTError:
                pass
            raise ChatGPTError("Вход отменён. При необходимости отключите ScreenStudy в настройках ChatGPT.")
        with self.locked():
            data = self._read()
            existing = data["profiles"].get(client_id)
            if existing and existing["subject"] != claims["sub"]:
                raise ChatGPTError("Подключение принадлежит другому аккаунту.")
            data["profiles"][client_id] = record
            atomic_json(self.path, data)
        return {"id": client_id, "permitted": permitted(record), "welcome": permitted(record) and not record["welcomed"]}

    @staticmethod
    def token_record(tokens, previous=None):
        previous = previous or {}
        access = tokens.get("access_token", "")
        scopes = tokens.get("scope", " ".join(previous.get("scopes", [])))
        expiry = tokens.get("expires_in", 3600)
        if (not isinstance(scopes, str) or not isinstance(access, str)
                or tokens.get("token_type", "Bearer").lower() != "bearer"
                or not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry <= 0):
            raise ChatGPTError("OpenAI вернула неподходящий сеанс авторизации.")
        if PLAN_SCOPE in scopes.split() and not access:
            raise ChatGPTError("OpenAI не выдала доступ для использования подписки.")
        result = copy.deepcopy(previous)
        result.update(access_token=access, refresh_token=tokens.get("refresh_token", previous.get("refresh_token", "")),
                      id_token=tokens.get("id_token", previous.get("id_token", "")),
                      scopes=scopes.split(), expires_at=time.time() + expiry)
        return result

    def acknowledge(self, account):
        with self.locked():
            data = self._read()
            if account in data["profiles"]:
                data["profiles"][account]["welcomed"] = True
                atomic_json(self.path, data)

    def access_token(self, account):
        with self.locked():
            data = self._read()
            p = data["profiles"].get(account, {})
            if not p.get("access_token") or not permitted(p):
                raise ChatGPTError("Войдите в ChatGPT и разрешите использование подписки в настройках приложения.")
            if p.get("expires_at", 0) > time.time() + 30:
                return p["access_token"]
            if not p.get("refresh_token"):
                raise ChatGPTError("Сеанс ChatGPT истёк. Войдите заново.")
            try:
                tokens = json_request(ISSUER + "/api/accounts/oauth/token", form={"grant_type": "refresh_token",
                    "client_id": p["client_id"], "refresh_token": p["refresh_token"], "resource": API})
            except ChatGPTError as exc:
                if exc.code in TERMINAL_REFRESH:
                    self.clear_tokens(p)
                    atomic_json(self.path, data)
                raise
            p = self.token_record(tokens, p)
            data["profiles"][account] = p
            atomic_json(self.path, data)
            if not permitted(p):
                raise ChatGPTError("Разрешение использовать подписку отозвано. Войдите заново.")
            return p["access_token"]

    @staticmethod
    def clear_tokens(p):
        for key in ("access_token", "refresh_token", "id_token", "expires_at", "scopes"):
            p.pop(key, None)

    def revoke(self, profile, config=None):
        if not profile.get("refresh_token"):
            return
        config = config or self.discovery()
        endpoint = trusted_url(config.get("revocation_endpoint", ""), ISSUER)
        json_request(endpoint, form={"token": profile["refresh_token"], "token_type_hint": "refresh_token",
                                     "client_id": profile["client_id"]})

    def sign_out(self, account):
        with self.locked():
            data = self._read()
            p = data["profiles"].get(account)
            if not p:
                return "Выход выполнен."
            warning = ""
            try:
                self.revoke(p)
            except ChatGPTError:
                warning = " Отзыв сеанса на сервере не подтверждён. Отключите ScreenStudy в настройках ChatGPT."
            self.clear_tokens(p)
            atomic_json(self.path, data)
            return "Выход выполнен; локальные данные сеанса удалены." + warning

    def models(self, account):
        data = json_request(API + "/models", token=self.access_token(account))
        entries = data.get("models")
        if not isinstance(entries, list):
            raise ChatGPTError("OpenAI не вернула каталог моделей подписки.")
        result = []
        for item in entries:
            if (isinstance(item, dict) and item.get("visibility") == "list"
                    and isinstance(item.get("slug"), str) and item["slug"]):
                result.append({"slug": item["slug"], "display_name": str(item.get("display_name") or item["slug"])})
        if not result:
            raise ChatGPTError("Для этого подключения нет доступных моделей. Проверьте аккаунт и разрешения ChatGPT.")
        return result

    def respond(self, account, model, prompt, jpeg, instructions):
        token = self.access_token(account)
        body = response_body(model, prompt, jpeg, instructions)
        request = urllib.request.Request(API + "/responses", json.dumps(body, ensure_ascii=False).encode(),
            {"Content-Type": "application/json", "Accept": "text/event-stream",
             "User-Agent": f"ScreenStudy/{__version__}", "Authorization": "Bearer " + token})
        try:
            with open_request(request, timeout=90, secrets_to_hide=[token]) as response:
                return read_stream(response, token)
        except (TimeoutError, OSError):
            raise ChatGPTError("Ответ ChatGPT прервался. Нажмите «Ответить сейчас», чтобы повторить.") from None


def response_body(model, prompt, jpeg, instructions):
    content = [{"type": "input_text", "text": prompt}]
    if jpeg is not None:
        content.append({"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")})
    return {"model": model, "instructions": instructions, "input": [{"role": "user", "content": content}],
            "store": False, "stream": True}


def read_stream(response, token=""):
    chunks, event_lines = [], []
    size, started = 0, time.monotonic()
    while True:
        line = response.readline(2_000_001)
        size += len(line)
        if size > 4_000_000 or len(line) > 2_000_000:
            raise ChatGPTError("Ответ ChatGPT слишком большой.")
        if time.monotonic() - started > 180:
            raise ChatGPTError("ChatGPT отвечает слишком долго. Повторите запрос позже.")
        if line.strip():
            if line.startswith(b"data:"):
                event_lines.append(line[5:].strip())
            continue
        if event_lines:
            raw = b"\n".join(event_lines)
            event_lines.clear()
            if raw != b"[DONE]":
                try:
                    event = json.loads(raw)
                    if not isinstance(event, dict):
                        raise ValueError()
                except (ValueError, UnicodeDecodeError):
                    raise ChatGPTError("Повреждён поток ответа ChatGPT.") from None
                kind = event.get("type")
                if kind == "response.output_text.delta" and isinstance(event.get("delta"), str):
                    chunks.append(event["delta"])
                elif kind in ("response.failed", "error"):
                    raise provider_error(event.get("response", event), secrets_to_hide=[token])
                elif kind == "response.incomplete":
                    raise ChatGPTError("ChatGPT не завершила ответ. Попробуйте один вопрос или другую модель.")
                elif kind == "response.completed":
                    result = event.get("response", {})
                    if result.get("status", "completed") != "completed":
                        raise ChatGPTError("ChatGPT не подтвердила завершение ответа.")
                    final = []
                    for item in result.get("output", []):
                        if item.get("type") == "message":
                            for part in item.get("content", []):
                                if part.get("type") == "output_text":
                                    final.append(part.get("text", ""))
                                elif part.get("type") == "refusal":
                                    raise ChatGPTError("ChatGPT отказалась отвечать на этот запрос.")
                    answer = "".join(final or chunks).strip()
                    if not answer:
                        raise ChatGPTError("ChatGPT завершила запрос без текстового ответа.")
                    return answer
        if not line:
            raise ChatGPTError("Связь прервалась до завершения ответа ChatGPT. Неполный ответ не показан.")


_managers = {}
_manager_lock = threading.Lock()


def manager(root=None):
    key = str(root or "default")
    with _manager_lock:
        if key not in _managers:
            _managers[key] = Manager(root)
        return _managers[key]
