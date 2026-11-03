"""Plaud Cloud sign-in: only a reply that names a real account counts as signed in.

Plaud answers HTTP 200 for most failures and puts the verdict in a JSON
``status`` field (0 = OK). The bug: a sign-in code pasted into the token field
got ``{"status": -3900, "msg": "invalid auth header"}`` from /user/me, which was
taken as success, so the app showed "Plaud User" and saved a dead token.
All HTTP here is faked; no real tokens.
"""

import base64
import json
import urllib.parse

import pytest
import requests

from app.state import cloud
from app.utils.constants import PLAUD_API_DOMAINS

EU = PLAUD_API_DOMAINS["eu"]
GLOBAL = PLAUD_API_DOMAINS["global"]
CN = PLAUD_API_DOMAINS["cn"]

FAKE_TOKEN = "eyJfake.test.token"
GOOD_USER_REPLY = {
    "status": 0,
    "msg": "success",
    "data_user": {"id": "u123", "nickname": "Test Person", "email": "test@example.com", "phone": "", "extra": 1},
}
JUNK_TOKEN_REPLY = {"status": -3900, "msg": "invalid auth header"}
EXPIRED_TOKEN_REPLY = {"detail": "token invalid or expired"}  # with HTTP 401
BAD_CODE_REPLY = {"status": -1, "msg": "授权码无效或已过期", "access_token": ""}


def region_code(region="aws:eu-central-1", head="testcode123"):
    return f"{head}::{base64.b64encode(region.encode()).decode()}"


class FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    @property
    def content(self):
        """Raw bytes like requests.Response: str/bytes bodies verbatim, else JSON."""
        if isinstance(self._body, bytes):
            return self._body
        if isinstance(self._body, str):
            return self._body.encode()
        return json.dumps(self._body).encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


class FakePlaud:
    """Stands in for http_requests in app.state.cloud; routes by (method, url)."""

    def __init__(self):
        self.routes = {}
        self.calls = []
        self.HTTPError = requests.HTTPError

    def on(self, method, url, body, status_code=200):
        self.routes[(method, url)] = (
            body if callable(body) or isinstance(body, FakeResponse) else FakeResponse(body, status_code)
        )

    def _send(self, method, url, **kw):
        self.calls.append((method, url, kw))
        route = self.routes.get((method, url))
        if route is None:
            raise requests.ConnectionError(f"no route for {method} {url}")
        return route(**kw) if callable(route) else route

    def get(self, url, **kw):
        return self._send("GET", url, **kw)

    def post(self, url, **kw):
        return self._send("POST", url, **kw)

    def urls(self):
        return [(m, u) for m, u, _ in self.calls]


@pytest.fixture
def plaud(monkeypatch, tmp_path):
    fake = FakePlaud()
    monkeypatch.setattr(cloud, "http_requests", fake)
    monkeypatch.setattr(cloud, "PLAUD_CONFIG_PATH", tmp_path / ".plaud_config.json")
    monkeypatch.setattr(
        cloud,
        "_cloud_state",
        {"logged_in": False, "token": None, "api_domain": EU, "user": None, "desktop_uuid": "uuid-1"},
    )
    return fake


def saved_config():
    path = cloud.PLAUD_CONFIG_PATH
    return json.loads(path.read_text()) if path.exists() else None


def assert_signed_out():
    assert cloud._cloud_state["logged_in"] is False
    assert cloud._cloud_state["token"] is None
    assert saved_config() is None


# ---------------------------------------------------------------------------
# user_from_reply: the one place that decides what a /user/me reply means
# ---------------------------------------------------------------------------


class TestUserFromReply:
    def test_real_user_is_reduced_to_the_fields_the_app_shows(self):
        assert cloud.user_from_reply(GOOD_USER_REPLY) == {
            "id": "u123",
            "nickname": "Test Person",
            "email": "test@example.com",
        }

    @pytest.mark.parametrize(
        "body, reason",
        [
            (JUNK_TOKEN_REPLY, "invalid auth header"),
            (EXPIRED_TOKEN_REPLY, "token invalid or expired"),
            ({"status": 0}, "no account in the reply"),
            ({"status": 0, "data_user": {"nickname": "x"}}, "no account"),  # no id
            ({"status": 0, "data_user": "u123"}, "no account"),
            ({"status": 1, "data_user": {"id": "u1"}}, "no account"),
            (None, "no account"),
            ([], "no account"),
        ],
    )
    def test_anything_else_is_rejected_with_plauds_reason(self, body, reason):
        with pytest.raises(cloud.PlaudAuthError, match=reason) as exc:
            cloud.user_from_reply(body)
        assert str(exc.value).startswith("Plaud rejected the token (")


# ---------------------------------------------------------------------------
# Pasted bearer token
# ---------------------------------------------------------------------------


class TestLoginWithToken:
    def test_junk_token_with_http_200_is_rejected(self, plaud):
        plaud.on("GET", f"{EU}/user/me", JUNK_TOKEN_REPLY)
        result = cloud.login_with_token("not-a-real-token")
        assert result == {"error": "Plaud rejected the token (invalid auth header)"}
        assert_signed_out()

    def test_expired_token_401_is_rejected(self, plaud):
        plaud.on("GET", f"{EU}/user/me", EXPIRED_TOKEN_REPLY, status_code=401)
        result = cloud.login_with_token(FAKE_TOKEN)
        assert result == {"error": "Plaud rejected the token (token invalid or expired)"}
        assert_signed_out()

    def test_good_token_signs_in_and_saves(self, plaud):
        plaud.on("GET", f"{EU}/user/me", GOOD_USER_REPLY)
        result = cloud.login_with_token(f"Bearer {FAKE_TOKEN}")
        assert result["logged_in"] is True
        assert result["user"]["nickname"] == "Test Person"
        assert cloud._cloud_state["logged_in"] is True
        assert cloud._cloud_state["token"] == FAKE_TOKEN
        assert cloud.get_state()["user"] == {"id": "u123", "nickname": "Test Person", "email": "test@example.com"}
        assert saved_config() == {"token": FAKE_TOKEN, "api_domain": EU}
        _, _, kw = plaud.calls[0]
        assert kw["headers"]["Authorization"] == f"bearer {FAKE_TOKEN}"

    def test_rejected_token_leaves_the_current_login_alone(self, plaud):
        cloud._cloud_state.update(logged_in=True, token="eyJcurrent", user={"id": "u0"})
        plaud.on("GET", f"{EU}/user/me", JUNK_TOKEN_REPLY)
        assert "error" in cloud.login_with_token("junk")
        assert cloud._cloud_state["logged_in"] is True
        assert cloud._cloud_state["token"] == "eyJcurrent"

    def test_network_error_raises_and_saves_nothing(self, plaud):
        with pytest.raises(requests.ConnectionError):
            cloud.login_with_token(FAKE_TOKEN)
        assert_signed_out()

    def test_region_mismatch_follows_plauds_redirect(self, plaud):
        plaud.on(
            "GET",
            f"{EU}/user/me",
            {"status": -302, "msg": "user region mismatch", "data": {"domains": {"api": GLOBAL}}},
        )
        plaud.on("GET", f"{GLOBAL}/user/me", GOOD_USER_REPLY)
        assert cloud.login_with_token(FAKE_TOKEN)["logged_in"] is True
        assert cloud._cloud_state["api_domain"] == GLOBAL
        assert saved_config() == {"token": FAKE_TOKEN, "api_domain": GLOBAL}

    def test_sign_in_code_pasted_as_token_is_exchanged(self, plaud):
        plaud.on("POST", f"{EU}/auth/access-token-auth-code", {"status": 0, "access_token": FAKE_TOKEN})
        plaud.on("GET", f"{EU}/user/me", GOOD_USER_REPLY)
        code = region_code()
        result = cloud.login_with_token(code)
        assert result["logged_in"] is True
        assert plaud.urls() == [("POST", f"{EU}/auth/access-token-auth-code"), ("GET", f"{EU}/user/me")]
        assert plaud.calls[0][2]["json"] == {"client_id": "web", "auth_code": code, "desktop_uuid": "uuid-1"}
        assert saved_config()["token"] == FAKE_TOKEN

    def test_url_encoded_code_pasted_as_token_is_exchanged(self, plaud):
        plaud.on("POST", f"{EU}/auth/access-token-auth-code", BAD_CODE_REPLY)
        code = region_code()
        result = cloud.login_with_token(urllib.parse.quote(code, safe=""))
        assert result == {"error": cloud.AUTH_CODE_EXPIRED_MESSAGE}
        assert plaud.calls[0][2]["json"]["auth_code"] == code
        assert_signed_out()

    @pytest.mark.parametrize(
        "text, is_code",
        [
            ("abc::ZXU=", True),
            ("abc%3A%3AZXU%3D", True),
            ("abc%3a%3aZXU%3d", True),
            ("eyJhbGciOi.payload.sig", False),
            ("eyJweird::thing", False),
            ("plain-token", False),
        ],
    )
    def test_looks_like_auth_code(self, text, is_code):
        assert cloud.looks_like_auth_code(text) is is_code


# ---------------------------------------------------------------------------
# Sign-in code from the browser flow
# ---------------------------------------------------------------------------


class TestExchangeToken:
    def test_bad_or_expired_code_is_explained(self, plaud):
        plaud.on("POST", f"{EU}/auth/access-token-auth-code", BAD_CODE_REPLY)
        result = cloud.exchange_token(region_code())
        assert result == {"error": "The sign-in code is invalid or has expired. Sign in again and use the new code."}
        assert ("GET", f"{EU}/user/me") not in plaud.urls()
        assert_signed_out()

    def test_unknown_plaud_message_is_passed_on(self, plaud):
        plaud.on("POST", f"{EU}/auth/access-token-auth-code", {"status": -5, "msg": "something else"})
        assert cloud.exchange_token("plaincode") == {"error": "Plaud didn't accept the sign-in code (something else)."}

    def test_token_with_non_zero_status_is_not_trusted(self, plaud):
        plaud.on("POST", f"{EU}/auth/access-token-auth-code", {"status": -1, "msg": "nope", "access_token": "x"})
        assert "nope" in cloud.exchange_token("plaincode")["error"]
        assert_signed_out()

    def test_issued_token_that_user_me_rejects_is_not_saved(self, plaud):
        plaud.on("POST", f"{EU}/auth/access-token-auth-code", {"status": 0, "access_token": FAKE_TOKEN})
        plaud.on("GET", f"{EU}/user/me", JUNK_TOKEN_REPLY)
        assert cloud.exchange_token(region_code()) == {"error": "Plaud rejected the token (invalid auth header)"}
        assert_signed_out()

    def test_good_code_signs_in(self, plaud):
        plaud.on("POST", f"{EU}/auth/access-token-auth-code", {"status": 0, "access_token": FAKE_TOKEN})
        plaud.on("GET", f"{EU}/user/me", GOOD_USER_REPLY)
        result = cloud.exchange_token(region_code(), "uuid-2")
        assert result == {
            "logged_in": True,
            "user": {"id": "u123", "nickname": "Test Person", "email": "test@example.com"},
        }
        assert plaud.calls[0][2]["json"]["desktop_uuid"] == "uuid-2"
        assert saved_config() == {"token": FAKE_TOKEN, "api_domain": EU}

    def test_url_encoded_code_is_decoded(self, plaud):
        plaud.on("POST", f"{EU}/auth/access-token-auth-code", {"status": 0, "access_token": FAKE_TOKEN})
        plaud.on("GET", f"{EU}/user/me", GOOD_USER_REPLY)
        code = region_code()
        assert cloud.exchange_token(urllib.parse.quote(code, safe=""))["logged_in"] is True
        assert plaud.calls[0][2]["json"]["auth_code"] == code

    @pytest.mark.parametrize(
        "region, domain",
        [("aws:eu-central-1", EU), ("aws:us-west-2", GLOBAL), ("aliyun:cn-hangzhou", CN)],
    )
    def test_region_suffix_picks_the_api_domain(self, plaud, region, domain):
        cloud._cloud_state["api_domain"] = "https://api.example.invalid"
        plaud.on("POST", f"{domain}/auth/access-token-auth-code", BAD_CODE_REPLY)
        cloud.exchange_token(region_code(region))
        assert plaud.urls() == [("POST", f"{domain}/auth/access-token-auth-code")]

    @pytest.mark.parametrize(
        "code",
        [
            region_code("aws:ap-southeast-1"),  # region we have no domain for
            "testcode::!!!not-base64!!!",
            "testcode::",
            "testcode-without-suffix",
        ],
    )
    def test_unknown_region_keeps_the_current_domain(self, plaud, code):
        cloud._cloud_state["api_domain"] = GLOBAL
        plaud.on("POST", f"{GLOBAL}/auth/access-token-auth-code", BAD_CODE_REPLY)
        cloud.exchange_token(code)
        assert plaud.urls() == [("POST", f"{GLOBAL}/auth/access-token-auth-code")]

    def test_server_error_raises(self, plaud):
        plaud.on("POST", f"{EU}/auth/access-token-auth-code", {}, status_code=502)
        with pytest.raises(requests.HTTPError):
            cloud.exchange_token("plaincode")
        assert_signed_out()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class TestRoutes:
    @pytest.fixture
    def client(self, flask_client, plaud):
        return flask_client

    def test_login_token_route_rejects_junk_token(self, client, plaud):
        plaud.on("GET", f"{EU}/user/me", JUNK_TOKEN_REPLY)
        r = client.post("/cloud/login-token", json={"token": "junk"})
        assert r.status_code == 401
        assert r.get_json() == {"error": "Plaud rejected the token (invalid auth header)"}
        assert client.get("/cloud/status").get_json()["logged_in"] is False

    def test_login_token_route_accepts_good_token(self, client, plaud):
        plaud.on("GET", f"{EU}/user/me", GOOD_USER_REPLY)
        r = client.post("/cloud/login-token", json={"token": FAKE_TOKEN})
        assert r.status_code == 200
        assert r.get_json()["user"]["nickname"] == "Test Person"
        status = client.get("/cloud/status").get_json()
        assert status["logged_in"] is True and status["user"]["email"] == "test@example.com"

    def test_login_token_route_network_error(self, client, plaud):
        r = client.post("/cloud/login-token", json={"token": FAKE_TOKEN})
        assert r.status_code == 502
        assert "Couldn't check the token" in r.get_json()["error"]

    def test_exchange_token_route_explains_expired_code(self, client, plaud):
        plaud.on("POST", f"{EU}/auth/access-token-auth-code", BAD_CODE_REPLY)
        r = client.post("/cloud/exchange-token", json={"auth_code": region_code()})
        assert r.status_code == 400
        assert r.get_json() == {"error": cloud.AUTH_CODE_EXPIRED_MESSAGE}


# ---------------------------------------------------------------------------
# Token restored at startup
# ---------------------------------------------------------------------------


class TestStartupValidation:
    def restore(self, token=FAKE_TOKEN, domain=EU):
        cloud.PLAUD_CONFIG_PATH.write_text(json.dumps({"token": token, "api_domain": domain}))
        cloud._cloud_state.update(token=token, api_domain=domain, logged_in=True)

    def test_rejected_saved_token_signs_out_and_is_forgotten(self, plaud):
        self.restore()
        plaud.on("GET", f"{EU}/user/me", JUNK_TOKEN_REPLY)
        cloud._validate_token_async(FAKE_TOKEN, EU)
        assert_signed_out()

    def test_expired_saved_token_401_signs_out(self, plaud):
        self.restore()
        plaud.on("GET", f"{EU}/user/me", EXPIRED_TOKEN_REPLY, status_code=401)
        cloud._validate_token_async(FAKE_TOKEN, EU)
        assert_signed_out()

    def test_offline_start_keeps_the_login(self, plaud):
        self.restore()
        cloud._validate_token_async(FAKE_TOKEN, EU)  # no route: ConnectionError
        assert cloud._cloud_state["logged_in"] is True
        assert saved_config()["token"] == FAKE_TOKEN

    def test_server_error_keeps_the_login(self, plaud):
        self.restore()
        plaud.on("GET", f"{EU}/user/me", {}, status_code=503)
        cloud._validate_token_async(FAKE_TOKEN, EU)
        assert cloud._cloud_state["logged_in"] is True

    def test_good_saved_token_fills_in_the_user(self, plaud):
        self.restore()
        plaud.on("GET", f"{EU}/user/me", GOOD_USER_REPLY)
        cloud._validate_token_async(FAKE_TOKEN, EU)
        assert cloud._cloud_state["logged_in"] is True
        assert cloud._cloud_state["user"]["nickname"] == "Test Person"

    def test_rejection_of_an_old_token_leaves_a_newer_login_alone(self, plaud):
        self.restore()
        cloud._cloud_state.update(token="eyJnewer")
        cloud.PLAUD_CONFIG_PATH.write_text(json.dumps({"token": "eyJnewer", "api_domain": EU}))
        plaud.on("GET", f"{EU}/user/me", JUNK_TOKEN_REPLY)
        cloud._validate_token_async(FAKE_TOKEN, EU)
        assert cloud._cloud_state["logged_in"] is True
        assert saved_config()["token"] == "eyJnewer"

    def test_desktop_app_token_is_saved_only_when_valid(self, plaud):
        cloud._cloud_state.update(token=FAKE_TOKEN, logged_in=True)
        plaud.on("GET", f"{EU}/user/me", GOOD_USER_REPLY)
        cloud._validate_token_async(FAKE_TOKEN, EU, save_on_success=True)
        assert saved_config() == {"token": FAKE_TOKEN, "api_domain": EU}

    def test_desktop_app_token_rejected_is_not_saved(self, plaud):
        cloud._cloud_state.update(token=FAKE_TOKEN, logged_in=True)
        plaud.on("GET", f"{EU}/user/me", JUNK_TOKEN_REPLY)
        cloud._validate_token_async(FAKE_TOKEN, EU, save_on_success=True)
        assert_signed_out()


# ---------------------------------------------------------------------------
# Device credentials
# ---------------------------------------------------------------------------


class TestDeviceCredentials:
    @pytest.fixture
    def signed_in(self, plaud, tmp_config):
        cloud._cloud_state.update(logged_in=True, token=FAKE_TOKEN)
        return plaud

    def test_bind_non_zero_status_surfaces_plauds_message(self, signed_in):
        signed_in.on("POST", f"{EU}/device/bind", {"status": -3900, "msg": "invalid auth header"})
        result = cloud.bind_device("SN_ABC")
        assert result["error"] == "Plaud wouldn't register this NotePin (invalid auth header)"
        assert result["status"] == -3900
        assert cloud.get_device_bind_token("SN_ABC") is None

    def test_bind_status_zero_without_resolvable_user_id_is_an_error(self, signed_in):
        # FAKE_TOKEN isn't a JWT, so the id comes from /user/me, which has none here.
        signed_in.on("POST", f"{EU}/device/bind", {"status": 0, "data": None})
        signed_in.on("GET", f"{EU}/user/me", {"status": 0, "data_user": {}})
        assert "account id couldn't be read" in cloud.bind_device("SN_ABC")["error"]
        assert cloud.get_device_bind_token("SN_ABC") is None

    def test_bind_falls_back_to_user_me_for_the_user_id(self, signed_in):
        signed_in.on("POST", f"{EU}/device/bind", {"status": 0, "data": None})
        signed_in.on("GET", f"{EU}/user/me", {"status": 0, "data_user": {"id": "a1b2c3d4" * 4}})
        assert cloud.bind_device("SN_ABC")["token"] == "a1b2c3d4" * 4
        assert cloud.get_device_bind_token("SN_ABC") == "a1b2c3d4" * 4

    def test_sn_sign_non_zero_status_surfaces_plauds_message(self, signed_in):
        signed_in.on("POST", f"{EU}/device/sn-sign", {"status": -7, "msg": "device not bound"})
        result = cloud.fetch_sn_signature("SN_ABC")
        assert result["error"] == "Plaud wouldn't sign this NotePin's serial (device not bound)"
        assert cloud.get_device_sn_signature("SN_ABC") is None

    def test_sn_sign_status_zero_without_signature(self, signed_in):
        signed_in.on("POST", f"{EU}/device/sn-sign", {"status": 0, "data": {"signature": {}}})
        assert cloud.fetch_sn_signature("SN_ABC") == {"error": "Plaud returned no signature for this NotePin"}

    def test_expired_sign_in_401_says_sign_in_again(self, signed_in):
        signed_in.on("POST", f"{EU}/device/sn-sign", EXPIRED_TOKEN_REPLY, status_code=401)
        error = cloud.fetch_sn_signature("SN_ABC")["error"]
        assert "token invalid or expired" in error and "Sign in again" in error

    def test_network_error_is_returned_not_raised(self, signed_in):
        assert "no route" in cloud.bind_device("SN_ABC")["error"]


def test_exchange_uses_saved_login_id_after_restart(monkeypatch):
    """The plaud:// link can arrive after a restart wiped the in-memory id."""
    import app.state.cloud as cloud_mod

    saved = {}
    monkeypatch.setattr(cloud_mod, "update_config", lambda fn: fn(saved))
    monkeypatch.setattr(cloud_mod, "load_config", lambda: dict(saved))
    _url, uuid = cloud_mod.get_login_url("eu")
    monkeypatch.setitem(cloud_mod._cloud_state, "desktop_uuid", None)  # "restart"
    sent = {}

    class R:
        status_code = 200

        def json(self):
            return {"status": -1, "msg": "x", "access_token": ""}

    def fake_post(url, json=None, **kw):
        sent.update(json)
        return R()

    monkeypatch.setattr(cloud_mod.http_requests, "post", fake_post)
    cloud_mod.exchange_token("abc::YXdzOmV1LWNlbnRyYWwtMQ")
    assert sent["desktop_uuid"] == uuid


# ---------------------------------------------------------------------------
# pld_ut / pld_urt capture, Android-header sn-sign with Fernet, token refresh
# ---------------------------------------------------------------------------

from cryptography.fernet import Fernet  # noqa: E402


class CookieResponse(FakeResponse):
    """FakeResponse whose raw.headers carries repeated Set-Cookie lines."""

    def __init__(self, body, set_cookies=(), status_code=200):
        super().__init__(body, status_code)

        class _Raw:
            headers = type("H", (), {"getlist": lambda self, name: list(set_cookies)})()

        self.raw = _Raw()


UT_CLEARER = "pld_ut=; Max-Age=0; Path=/"
NEW_UT = "new.ut.token"


class TestCookieCapture:
    def test_helper_takes_last_non_empty_value(self):
        r = CookieResponse({}, [UT_CLEARER, "pld_ut=real.ut; Path=/; HttpOnly", "PLD_URT=real.urt; Max-Age=99"])
        assert cloud._cookies_from_response(r) == {"ut": "real.ut", "urt": "real.urt"}

    def test_helper_falls_back_to_response_cookies(self):
        r = FakeResponse({})
        r.cookies = {"pld_ut": "c.ut"}
        assert cloud._cookies_from_response(r) == {"ut": "c.ut", "urt": None}

    def test_exchange_saves_ut_and_urt(self, plaud):
        plaud.on(
            "POST",
            f"{EU}/auth/access-token-auth-code",
            CookieResponse(
                {"status": 0, "access_token": FAKE_TOKEN}, [UT_CLEARER, "pld_ut=real.ut", "pld_urt=real.urt"]
            ),
        )
        plaud.on("GET", f"{EU}/user/me", GOOD_USER_REPLY)
        assert cloud.exchange_token(region_code())["logged_in"] is True
        assert cloud._cloud_state["ut"] == "real.ut" and cloud._cloud_state["urt"] == "real.urt"
        assert saved_config() == {"token": FAKE_TOKEN, "api_domain": EU, "ut": "real.ut", "urt": "real.urt"}

    def test_exchange_without_cookies_keeps_old_file_shape(self, plaud):
        plaud.on("POST", f"{EU}/auth/access-token-auth-code", {"status": 0, "access_token": FAKE_TOKEN})
        plaud.on("GET", f"{EU}/user/me", GOOD_USER_REPLY)
        cloud.exchange_token(region_code())
        assert saved_config() == {"token": FAKE_TOKEN, "api_domain": EU}

    def test_load_reads_ut_and_urt_and_old_files_still_load(self, plaud, monkeypatch):
        monkeypatch.setattr(cloud, "_validate_token_async", lambda *a, **k: None)
        cloud.PLAUD_CONFIG_PATH.write_text(json.dumps({"token": "t", "api_domain": EU, "ut": "u", "urt": "r"}))
        assert cloud.load_cloud_token() is True
        assert (cloud._cloud_state["ut"], cloud._cloud_state["urt"]) == ("u", "r")
        cloud.PLAUD_CONFIG_PATH.write_text(json.dumps({"token": "t2", "api_domain": EU}))
        cloud._cloud_state.update(ut=None, urt=None)
        assert cloud.load_cloud_token() is True
        assert cloud._cloud_state["token"] == "t2" and cloud._cloud_state["ut"] is None

    def test_logout_clears_ut_and_urt(self, plaud):
        cloud._cloud_state.update(ut="u", urt="r")
        cloud.logout()
        assert cloud._cloud_state["ut"] is None and cloud._cloud_state["urt"] is None


class TestSnSignAndroid:
    SN = "SN_ABC"

    @pytest.fixture
    def signed_in(self, plaud, tmp_config):
        cloud._cloud_state.update(logged_in=True, token=FAKE_TOKEN, ut="the.ut", urt="the.urt")
        return plaud

    def test_sends_android_headers_and_prefers_ut(self, signed_in):
        signed_in.on("POST", f"{EU}/device/sn-sign", {"status": 0, "data": {"signature": {self.SN: "SIG"}}})
        assert cloud.fetch_sn_signature(self.SN) == {"signature": "SIG", "sn": self.SN}
        _m, _u, kw = signed_in.calls[0]
        h = kw["headers"]
        assert h["Authorization"] == "Bearer the.ut"
        assert h["app-platform"] == "android" and h["edit-from"] == "android"
        assert h["app-version"] == "4.2.0 (1297)"
        assert h["X-Encrypt-Response"] == "1"
        assert h["User-Agent"] == "PLAUD/4.2.0(build:1297;Android 14)"
        assert kw["json"] == {"sns": [self.SN]}

    def test_falls_back_to_rest_token_without_ut(self, signed_in):
        cloud._cloud_state.update(ut=None)
        signed_in.on("POST", f"{EU}/device/sn-sign", {"status": 0, "data": {"signature": {self.SN: "SIG"}}})
        cloud.fetch_sn_signature(self.SN)
        assert signed_in.calls[0][2]["headers"]["Authorization"] == f"Bearer {FAKE_TOKEN}"

    def test_fernet_body_is_decrypted(self, signed_in):
        payload = {"status": 0, "data": {"signature": {self.SN: "DECRYPTED_SIG"}}}
        blob = Fernet(cloud.PLAUD_FERNET_KEY).encrypt(json.dumps(payload).encode())
        assert blob.startswith(b"gAAAAAB")
        signed_in.on("POST", f"{EU}/device/sn-sign", blob)
        assert cloud.fetch_sn_signature(self.SN)["signature"] == "DECRYPTED_SIG"
        assert cloud.get_device_sn_signature(self.SN) == "DECRYPTED_SIG"

    def test_decode_body_garbage_is_empty(self):
        assert cloud._decode_body(b"gAAAAABnot-a-token") == {}
        assert cloud._decode_body(b"<html>") == {}

    def test_stale_ut_refreshes_once_then_succeeds(self, signed_in):
        replies = iter(
            [
                FakeResponse({"status": -3900, "msg": "invalid auth header"}, 401),
                FakeResponse({"status": 0, "data": {"signature": {self.SN: "SIG2"}}}),
            ]
        )
        signed_in.on("POST", f"{EU}/device/sn-sign", lambda **kw: next(replies))
        signed_in.on("POST", f"{EU}/auth/refresh-user-token", CookieResponse({}, [UT_CLEARER, f"pld_ut={NEW_UT}"]))
        signed_in.on("GET", f"{EU}/user/me", GOOD_USER_REPLY)
        assert cloud.fetch_sn_signature(self.SN)["signature"] == "SIG2"
        urls = [u for _m, u in signed_in.urls()]
        assert urls.count(f"{EU}/device/sn-sign") == 2
        assert urls.count(f"{EU}/auth/refresh-user-token") == 1
        sign_calls = [c for c in signed_in.calls if c[1].endswith("/device/sn-sign")]
        assert sign_calls[1][2]["headers"]["Authorization"] == f"Bearer {NEW_UT}"

    def test_refresh_is_attempted_only_once(self, signed_in):
        signed_in.on("POST", f"{EU}/device/sn-sign", {"status": -3900, "msg": "invalid auth header"}, status_code=401)
        signed_in.on("POST", f"{EU}/auth/refresh-user-token", CookieResponse({}, [f"pld_ut={NEW_UT}"]))
        signed_in.on("GET", f"{EU}/user/me", GOOD_USER_REPLY)
        assert "error" in cloud.fetch_sn_signature(self.SN)
        urls = [u for _m, u in signed_in.urls()]
        assert urls.count(f"{EU}/auth/refresh-user-token") == 1
        assert urls.count(f"{EU}/device/sn-sign") == 2


class TestRefreshUserToken:
    @pytest.fixture
    def with_urt(self, plaud):
        cloud._cloud_state.update(logged_in=True, token="old.token", ut="old.ut", urt="the.urt")
        return plaud

    def test_no_refresh_token(self, plaud):
        assert cloud.refresh_user_token() == {"refreshed": False, "error": "no refresh token"}

    def test_refresh_updates_state_and_persists(self, with_urt):
        with_urt.on("POST", f"{EU}/auth/refresh-user-token", CookieResponse({}, [UT_CLEARER, f"pld_ut={NEW_UT}"]))
        with_urt.on("GET", f"{EU}/user/me", GOOD_USER_REPLY)
        result = cloud.refresh_user_token()
        assert result["refreshed"] is True and result["user"]["id"] == "u123"
        assert cloud._cloud_state["token"] == NEW_UT and cloud._cloud_state["ut"] == NEW_UT
        assert saved_config() == {"token": NEW_UT, "api_domain": EU, "ut": NEW_UT, "urt": "the.urt"}
        kw = with_urt.calls[0][2]
        assert kw["cookies"] == {"pld_ut": "old.ut", "pld_urt": "the.urt"} and kw["json"] == {}

    def test_no_new_cookie_leaves_state_alone(self, with_urt):
        with_urt.on(
            "POST", f"{EU}/auth/refresh-user-token", CookieResponse({"status": -1, "msg": "nope"}, [UT_CLEARER])
        )
        result = cloud.refresh_user_token()
        assert result["refreshed"] is False and "nope" in result["error"]
        assert cloud._cloud_state["token"] == "old.token"

    def test_new_token_rejected_by_user_me(self, with_urt):
        with_urt.on("POST", f"{EU}/auth/refresh-user-token", CookieResponse({}, [f"pld_ut={NEW_UT}"]))
        with_urt.on("GET", f"{EU}/user/me", JUNK_TOKEN_REPLY)
        assert cloud.refresh_user_token()["refreshed"] is False
        assert cloud._cloud_state["token"] == "old.token"

    def test_cloud_api_401_refreshes_once_and_retries(self, with_urt):
        replies = iter([FakeResponse(EXPIRED_TOKEN_REPLY, 401), FakeResponse({"status": 0, "data_devices": []})])
        with_urt.on("GET", f"{EU}/device/list", lambda **kw: next(replies))
        with_urt.on("POST", f"{EU}/auth/refresh-user-token", CookieResponse({}, [f"pld_ut={NEW_UT}"]))
        with_urt.on("GET", f"{EU}/user/me", GOOD_USER_REPLY)
        assert cloud.cloud_api("/device/list")["status"] == 0
        assert [u for _m, u in with_urt.urls()].count(f"{EU}/auth/refresh-user-token") == 1
        last = [c for c in with_urt.calls if c[1].endswith("/device/list")][-1]
        assert last[2]["headers"]["Authorization"] == f"bearer {NEW_UT}"
