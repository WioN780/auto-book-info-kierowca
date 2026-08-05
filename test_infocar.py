#!/usr/bin/env python3
"""Self-check for the session-cookie handling that keeps the bot logged in. Run: python test_infocar.py"""

import base64
import json
import time

import infocar_bot
from infocar_bot import InfoCarClient


def _md_cookie(expires: int) -> str:
    md = json.dumps({"maxAge": 900, "expires": expires, "issuedAt": expires - 900})
    return base64.urlsafe_b64encode(md.encode()).decode().rstrip("=")


def test_cookies_go_in_the_jar_not_a_static_header():
    c = InfoCarClient(f'CookieScriptConsent={{"a":"b"}}; __Secure-PUDOJT=abc; __Secure-PUDOJTMD={_md_cookie(0)}')
    assert "Cookie" not in c.session.headers, "static Cookie header blocks server-side token rotation"
    assert c.session.cookies.get("__Secure-PUDOJT") == "abc"
    assert "__Secure-PUDOJT=abc" in c.cookie_string()

    # A rotated cookie from a Set-Cookie header must replace, not duplicate.
    c.session.cookies.set("__Secure-PUDOJT", "xyz", domain=InfoCarClient.DOMAIN, path="/")
    assert c.session.cookies.get("__Secure-PUDOJT") == "xyz"
    assert c.cookie_string().count("__Secure-PUDOJT=") == 1


def test_jwt_expiry_is_read_from_metadata_cookie():
    soon = int(time.time()) + 900
    c = InfoCarClient(f"__Secure-PUDOJT=abc; __Secure-PUDOJTMD={_md_cookie(soon)}")
    assert int(c.jwt_expires_at().timestamp()) == soon

    assert InfoCarClient("__Secure-PUDOJT=abc").jwt_expires_at() is None
    assert InfoCarClient("__Secure-PUDOJT=abc; __Secure-PUDOJTMD=garbage").jwt_expires_at() is None


def test_org_ids_accept_a_single_id_or_a_list_under_either_key():
    parse = infocar_bot.parse_org_ids
    assert parse("43,42, 73", {}) == [43, 42, 73]
    assert parse("43", {"organization_ids": [1, 2]}) == [43], "CLI must win over config"
    assert parse(None, {"organization_ids": [43, 42]}) == [43, 42]
    assert parse(None, {"organization_id": 43}) == [43]
    # A list under the singular key is the obvious thing to try, and it used to crash.
    assert parse(None, {"organization_id": [43, 42, 73]}) == [43, 42, 73]
    assert parse(None, {"org_id": [43, 42]}) == [43, 42]
    assert parse(None, {}) == [43]


def _client_with_responses(codes):
    """Client whose refresh call returns `codes` in order (None = network failure)."""
    c = InfoCarClient(f"__Secure-PUDOJT=abc; __Secure-PUDOJTMD={_md_cookie(int(time.time()) + 900)}")
    calls, sleeps = [], []

    def fake_get(url, **kwargs):
        calls.append(url)
        code = codes[min(len(calls) - 1, len(codes) - 1)]
        if code is None:
            raise OSError("Remote end closed connection without response")
        return type("Res", (), {"status_code": code})()

    c.session.get = fake_get
    infocar_bot.time.sleep = lambda s: sleeps.append(s)
    return c, calls, sleeps


def test_refresh_retries_server_errors_but_not_rejections():
    # A 500 is the 1h session cap or a gateway blip - indistinguishable here, so retry.
    c, calls, sleeps = _client_with_responses([500])
    assert c.refresh_jwt(config_path="no-such-config.json") is False
    assert len(calls) == 3 and sleeps == [2, 4], (calls, sleeps)

    # 401/404 is the gateway's definitive verdict: retrying cannot help.
    c, calls, sleeps = _client_with_responses([404])
    assert c.refresh_jwt(config_path="no-such-config.json") is False
    assert len(calls) == 1 and sleeps == [], (calls, sleeps)


def test_unreachable_server_is_not_a_logged_out_session():
    # The bot gives up only on `is False`. If an outage outlasted the refresh retries and
    # None counted as logged out, any WiFi drop would kill a session with hours left.
    c, _, _ = _client_with_responses([None])
    assert c.check_logged_user() is None

    c, _, _ = _client_with_responses([404])
    assert c.check_logged_user() is False


def test_refresh_survives_a_dropped_connection():
    # One TCP reset used to kill a session with half an hour of headroom left.
    c, calls, _ = _client_with_responses([None, 204])
    assert c.refresh_jwt(config_path="no-such-config.json") is True
    assert len(calls) == 2


if __name__ == "__main__":
    test_cookies_go_in_the_jar_not_a_static_header()
    test_jwt_expiry_is_read_from_metadata_cookie()
    test_org_ids_accept_a_single_id_or_a_list_under_either_key()
    test_refresh_retries_server_errors_but_not_rejections()
    test_unreachable_server_is_not_a_logged_out_session()
    test_refresh_survives_a_dropped_connection()
    print("OK")
