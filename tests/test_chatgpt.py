"""Offline contract tests; all credentials are generated test fixtures."""
import base64
import copy
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from screenstudy import ai, chatgpt as cg
from screenstudy.core import Settings

CONFIG = {"issuer": cg.ISSUER, "authorization_endpoint": cg.ISSUER + "/api/accounts/authorize",
          "token_endpoint": cg.ISSUER + "/api/accounts/oauth/token", "jwks_uri": cg.ISSUER + "/.well-known/jwks.json",
          "revocation_endpoint": cg.ISSUER + "/oauth/revoke", "id_token_signing_alg_values_supported": ["RS256"]}
CLIENT = "oaiapp_test_client"


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.private.public_key()))
        cls.jwk.update(kid="test", use="sig", alg="RS256")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.auth = cg.Manager(Path(self.temp.name)/"app")
        self.captured = {}
        self.requests = []

    def tearDown(self):
        self.temp.cleanup()

    def identity(self, **changes):
        claims = {"sub": "test-subject", "iss": cg.ISSUER, "aud": CLIENT, "exp": time.time() + 300,
                  "iat": int(time.time()), "nonce": "nonce", "email": "demo@example.test"}
        claims.update(changes)
        return jwt.encode(claims, self.private, algorithm="RS256", headers={"kid": "test"})

    def fake_json(self, url, *, form=None, token=None):
        self.requests.append((url, copy.deepcopy(form), token))
        if url.endswith("openid-configuration"):
            return copy.deepcopy(CONFIG)
        if url == CONFIG["jwks_uri"]:
            return {"keys": [self.jwk]}
        if url == CONFIG["revocation_endpoint"]:
            return {}
        if form and form.get("grant_type") == "authorization_code":
            self.assertEqual(form["client_id"], CLIENT)
            self.assertEqual(form["redirect_uri"], self.captured["redirect_uri"])
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).decode().rstrip("=")
            self.assertEqual(challenge, self.captured["code_challenge"])
            return {"access_token": "test-access", "refresh_token": "test-refresh", "token_type": "Bearer",
                    "scope": cg.SCOPES, "expires_in": 3600, "id_token": self.identity(nonce=self.captured["nonce"])}
        raise AssertionError("Unexpected test route")

    def fake_browser(self, url):
        self.captured = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        callback = self.captured["redirect_uri"] + "?" + urllib.parse.urlencode(
            {"code": "test-code", "state": self.captured["state"], "client_id": CLIENT})
        def deliver():
            with urllib.request.urlopen(callback, timeout=3) as r:
                r.read()
        t = threading.Thread(target=deliver)
        t.start()
        self.addCleanup(t.join, 4)
        return True

    def seed(self, account=CLIENT, **changes):
        record = {"client_id": account, "subject": "test-subject", "email": "demo@example.test",
                  "access_token": "old-access", "refresh_token": "old-refresh", "id_token": "test-id",
                  "scopes": cg.SCOPES.split(), "expires_at": time.time() + 3600}
        record.update(changes)
        with self.auth.locked():
            data = self.auth._read()
            data["profiles"][account] = record
            cg.atomic_json(self.auth.path, data)

    def test_full_signin_pkce_callback_identity_and_private_persistence(self):
        with patch.object(cg, "json_request", side_effect=self.fake_json):
            result = self.auth.connect(browser_open=self.fake_browser)
        self.assertTrue(result["permitted"])
        self.assertEqual(self.captured["client_id"], "dynamic_agent_client")
        self.assertEqual(self.captured["agent_name_hint"], "ScreenStudy")
        self.assertEqual(self.captured["resource"], cg.API)
        self.assertTrue(self.captured["redirect_uri"].startswith("http://127.0.0.1:"))
        self.assertTrue(cg.Manager(self.auth.root).ready(CLIENT))
        self.assertEqual(os.stat(self.auth.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.auth.root).st_mode & 0o777, 0o700)
        self.assertNotIn("access_token", self.auth.profiles()[0])
        self.assertEqual(self.auth.host_id(), cg.Manager(self.auth.root).host_id())

    def test_returning_account_reuses_client_and_checks_identity(self):
        self.seed()
        with patch.object(cg, "json_request", side_effect=self.fake_json):
            self.auth.connect(CLIENT, browser_open=self.fake_browser)
        self.assertEqual(self.captured["client_id"], CLIENT)
        self.assertNotIn("agent_name_hint", self.captured)
        self.seed(subject="another-subject")
        with patch.object(cg, "json_request", side_effect=self.fake_json):
            with self.assertRaisesRegex(cg.ChatGPTError, "другой аккаунт"):
                self.auth.connect(CLIENT, browser_open=self.fake_browser)
        self.assertEqual(self.auth.access_token(CLIENT), "old-access")

    def test_id_token_signature_issuer_audience_expiry_nonce(self):
        for change in ({"iss":"https://attacker.invalid"}, {"aud":"different-client"},
                       {"exp":time.time()-100}, {"nonce":"other"}, {"nonce":"подделка"}):
            with self.subTest(change=change), self.assertRaises(cg.ChatGPTError):
                cg.validate_identity(self.identity(**change), CLIENT, "nonce", CONFIG, {"keys":[self.jwk]})
        forged = jwt.encode({"sub":"test", "nonce":"nonce", "aud":CLIENT, "iss":cg.ISSUER,
                             "exp":time.time()+60, "iat":int(time.time())}, "wrong-secret"*4, algorithm="HS256", headers={"kid":"test"})
        with self.assertRaises(cg.ChatGPTError):
            cg.validate_identity(forged, CLIENT, "nonce", CONFIG, {"keys":[self.jwk]})
        damaged = self.identity()[:-10] + "aaaaaaaaaa"
        with self.assertRaises(cg.ChatGPTError):
            cg.validate_identity(damaged, CLIENT, "nonce", CONFIG, {"keys":[self.jwk]})

    def test_wrong_state_ignored_and_cancel_closes_listener(self):
        attempt = cg.LoginAttempt(self.auth.host_id(), CONFIG)
        cancel = threading.Event()
        failures = []
        def wait():
            try:
                attempt.wait(cancel, timeout=3)
            except cg.ChatGPTError as e:
                failures.append(str(e))
        thread = threading.Thread(target=wait)
        thread.start()
        for state in ("wrong", "подделка"):
            u = attempt.redirect_uri + "?" + urllib.parse.urlencode({"state":state, "code":"test", "client_id":CLIENT})
            with self.assertRaises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(u, timeout=2)
            self.assertEqual(exc.exception.code, 400)
            exc.exception.close()
        self.assertIsNone(attempt.result)
        cancel.set()
        thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertIn("отменён", failures[0])
        self.assertEqual(attempt.server.fileno(), -1)

    def test_callback_requires_issued_id_and_denied_consent_is_not_exchanged(self):
        for result in ({"state":"s", "code":"test"}, {"error":"access_denied"}):
            attempt = cg.LoginAttempt(self.auth.host_id(), CONFIG)
            attempt.result = result
            with self.assertRaises(cg.ChatGPTError):
                attempt.wait(threading.Event())

    def test_identity_without_plan_scope_is_saved_but_not_used(self):
        def limited(*args, **kwargs):
            result = self.fake_json(*args, **kwargs)
            if "access_token" in result:
                result["scope"] = "openid email profile"
            return result
        with patch.object(cg, "json_request", side_effect=limited):
            result = self.auth.connect(browser_open=self.fake_browser)
        self.assertFalse(result["permitted"])
        self.assertFalse(self.auth.ready(CLIENT))
        with self.assertRaises(cg.ChatGPTError):
            self.auth.access_token(CLIENT)

    def test_concurrent_refresh_rotates_once_and_survives_restart(self):
        self.seed(expires_at=0)
        seen = []
        def refresh(url, **kwargs):
            seen.append(kwargs["form"])
            time.sleep(.03)
            return {"access_token":"new-access", "refresh_token":"new-refresh", "expires_in":3600,
                    "scope":cg.SCOPES, "token_type":"Bearer"}
        results = []
        other = cg.Manager(self.auth.root)
        with patch.object(cg, "json_request", side_effect=refresh):
            threads = [threading.Thread(target=lambda a=a: results.append(a.access_token(CLIENT))) for a in (self.auth,other)]
            for t in threads: t.start()
            for t in threads: t.join(2)
        self.assertEqual(results, ["new-access", "new-access"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["client_id"], CLIENT)
        self.assertEqual(seen[0]["refresh_token"], "old-refresh")
        with self.auth.locked():
            self.assertEqual(self.auth._read()["profiles"][CLIENT]["refresh_token"], "new-refresh")

    def test_refresh_network_failure_preserves_session_terminal_failure_clears(self):
        self.seed(expires_at=0)
        with patch.object(cg, "json_request", side_effect=cg.ChatGPTError("offline")):
            with self.assertRaises(cg.ChatGPTError): self.auth.access_token(CLIENT)
        self.assertTrue(self.auth.ready(CLIENT))
        with patch.object(cg, "json_request", side_effect=cg.ChatGPTError("expired", "invalid_grant")):
            with self.assertRaises(cg.ChatGPTError): self.auth.access_token(CLIENT)
        self.assertFalse(self.auth.ready(CLIENT))
        self.assertIn(CLIENT, self.auth._read()["profiles"])

    def test_signout_revokes_and_preserves_other_account(self):
        self.seed()
        self.seed("oaiapp_other")
        with patch.object(cg, "json_request", side_effect=self.fake_json):
            self.auth.sign_out(CLIENT)
        self.assertFalse(self.auth.ready(CLIENT))
        self.assertTrue(self.auth.ready("oaiapp_other"))
        self.assertTrue(any(form and form.get("token_type_hint") == "refresh_token" for _,form,_ in self.requests))
        self.seed()
        with patch.object(cg, "json_request", side_effect=cg.ChatGPTError("offline")):
            result = self.auth.sign_out(CLIENT)
        self.assertIn("не подтверждён", result)
        self.assertFalse(self.auth.ready(CLIENT))

    def test_catalog_preserves_order_and_filters_hidden_models(self):
        self.seed()
        entries = [{"slug":"model-b","display_name":"B","visibility":"list"},
                   {"slug":"hidden","visibility":"hidden"},
                   {"slug":"model-a","display_name":"A","visibility":"list"}]
        with patch.object(cg, "json_request", return_value={"models":entries}) as request:
            self.assertEqual([m["slug"] for m in self.auth.models(CLIENT)], ["model-b","model-a"])
            self.assertEqual(request.call_args.kwargs["token"], "old-access")

    def test_manager_uses_selected_session_and_public_responses_endpoint(self):
        self.seed()
        self.seed("oaiapp_other", access_token="other-access")
        result = stream({"type":"response.output_text.delta","delta":"ready"}, {"type":"response.completed"})
        with patch.object(cg, "open_request", return_value=result) as transport:
            self.assertEqual(self.auth.respond("oaiapp_other", "model-a", "Q", None, "study"), "ready")
        req = transport.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(req.get_header("Authorization"), "Bearer other-access")
        body = json.loads(req.data)
        self.assertEqual(body["model"], "model-a")
        self.assertEqual(body["instructions"], "study")
        self.assertNotIn("max_output_tokens", body)
        self.assertNotIn("previous_response_id", body)

    def test_discovery_redirect_and_error_redaction(self):
        for bad in ("http://auth.openai.com/token", "https://auth.openai.com.attacker.invalid/token", "https://evil.invalid"):
            with self.assertRaises(cg.ChatGPTError): cg.trusted_url(bad, cg.ISSUER)
        with self.assertRaises(cg.ChatGPTError):
            cg.NoRedirect().redirect_request(None, io.BytesIO(), 302, "", {}, "https://evil.invalid")
        error = cg.provider_error({"detail":"failed Bearer test-secret eyJabc.abc.xyz"}, 403, "req_test", ["test-secret"])
        self.assertNotIn("test-secret", str(error))
        self.assertNotIn("eyJabc", str(error))
        self.assertIn("403", str(error))
        self.assertIn("req_test", str(error))


def stream(*events):
    return io.BytesIO(b"".join(b"data: " + json.dumps(e, ensure_ascii=False).encode() + b"\n\n" for e in events))


class ResponseTests(unittest.TestCase):
    def test_completed_text_and_image_request_contract(self):
        body = cg.response_body("model", "вопрос", b"image", "instruction")
        self.assertEqual(set(body), {"model","instructions","input","store","stream"})
        self.assertFalse(body["store"])
        self.assertTrue(body["stream"])
        self.assertEqual(body["input"][0]["content"][1]["type"], "input_image")
        output = cg.read_stream(stream({"type":"response.output_text.delta","delta":"Ответ"},
                                       {"type":"response.completed","response":{"status":"completed"}}))
        self.assertEqual(output, "Ответ")

    def test_terminal_response_text_does_not_duplicate_delta(self):
        output = cg.read_stream(stream({"type":"response.output_text.delta","delta":"A"},
            {"type":"response.completed","response":{"output":[{"type":"message","content":[{"type":"output_text","text":"AB"}]}]}}))
        self.assertEqual(output, "AB")

    def test_truncated_incomplete_or_late_limit_never_returns_partial_answer(self):
        delta = {"type":"response.output_text.delta","delta":"partial"}
        for events in ((delta,), (delta,{"type":"response.incomplete"}),
            (delta,{"type":"response.failed","response":{"error":{"code":"subscription_sharing_usage_limit_exceeded"}}})):
            with self.assertRaises(cg.ChatGPTError): cg.read_stream(stream(*events))
        with self.assertRaisesRegex(cg.ChatGPTError, "лимит"):
            cg.read_stream(stream({"type":"error","code":"subscription_sharing_usage_limit_exceeded"}))

    def test_ai_routes_chatgpt_without_api_key_or_fallback(self):
        cfg = Settings(provider="chatgpt",base_url=cg.API,model="test-model",chatgpt_account=CLIENT,use_images=False)
        class Fake:
            def respond(self, account, model, prompt, jpeg, instructions):
                assert account == CLIENT and jpeg is None
                return '{"questions":[]}'
        with patch.object(ai, "make_request", side_effect=AssertionError("must not use API-key route")):
            self.assertEqual(ai.analyze(cfg, "unrelated-groq-key", {"lines":[],"jpeg":b"image"}, auth=Fake()), [])
        cfg.base_url = "https://evil.invalid"
        with self.assertRaises(ValueError): ai.call(cfg, "", "Q", auth=Fake())


if __name__ == "__main__":
    unittest.main()
