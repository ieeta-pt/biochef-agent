"""Every state change proves it could read the page (section 9)."""

import asyncio
import re

import pytest

from tests.ui_support import PAGES, PROXY, dev_settings, header_settings, proxy_client, with_routes, USER_HEADER
from ui import csrf

DEV_COOKIE = csrf.cookie_name(False)
ECHO = "/ui/_/echo"


@pytest.fixture
def client():
    return with_routes(dev_settings())


def token_of(page):
    return re.search(r'<meta name="csrf-token" content="([^"]+)">', page.text).group(1)


def test_a_page_sets_the_cookie_and_repeats_it(client):
    page = client.get(PAGES[0])
    assert client.cookies[DEV_COOKIE] == token_of(page)


def test_the_cookie_has_the_attributes_of_the_spec_in_dev(client):
    cookie = client.get(PAGES[0]).headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert "Path=/" in cookie and "Secure" not in cookie


def test_behind_the_proxy_it_is_also_secure_and_host_only():
    cookie = proxy_client().get(PAGES[0]).headers["set-cookie"]
    assert cookie.startswith("__Host-biochef-csrf=")
    assert "; Secure" in cookie and "Domain" not in cookie


def test_the_token_stays_for_the_session(client):
    first = token_of(client.get(PAGES[0]))
    second = client.get(PAGES[1])
    assert token_of(second) == first
    assert "set-cookie" not in second.headers


def test_two_sessions_have_two_tokens():
    assert (token_of(with_routes(dev_settings()).get(PAGES[0]))
            != token_of(with_routes(dev_settings()).get(PAGES[0])))


def test_a_token_the_server_would_not_issue_is_replaced(client):
    client.cookies.set(DEV_COOKIE, "short")
    assert len(token_of(client.get(PAGES[0]))) == 43


def test_static_files_do_not_start_a_session(client):
    assert "set-cookie" not in client.get("/ui/static/app.css").headers


def test_a_header_token_passes(client):
    token = token_of(client.get(PAGES[0]))
    response = client.post(ECHO, headers={"X-CSRF-Token": token}, content="body")
    assert (response.status_code, response.text) == (200, "body")


def test_a_form_token_passes_and_the_route_still_reads_the_form(client):
    token = token_of(client.get(PAGES[0]))
    response = client.post(ECHO, data={"csrf_token": token, "comment": "ok"})
    assert response.status_code == 200
    assert "comment=ok" in response.text


def test_no_token_is_refused(client):
    client.get(PAGES[0])
    assert client.post(ECHO).status_code == 403


def test_no_cookie_is_refused(client):
    token = token_of(client.get(PAGES[0]))
    client.cookies.clear()
    assert client.post(ECHO, headers={"X-CSRF-Token": token}).status_code == 403


def test_a_wrong_header_is_refused(client):
    client.get(PAGES[0])
    assert client.post(ECHO, headers={"X-CSRF-Token": "x" * 43}).status_code == 403


def test_a_wrong_form_field_is_refused(client):
    client.get(PAGES[0])
    assert client.post(ECHO, data={"csrf_token": "x" * 43}).status_code == 403


def test_a_wrong_header_is_not_saved_by_a_right_field(client):
    token = token_of(client.get(PAGES[0]))
    response = client.post(ECHO, data={"csrf_token": token},
                           headers={"X-CSRF-Token": "x" * 43})
    assert response.status_code == 403


@pytest.mark.parametrize("order", ["right first", "right last"])
def test_a_repeated_form_field_is_refused(client, order):
    token = token_of(client.get(PAGES[0]))
    fields = [f"csrf_token={token}", "csrf_token=other"]
    body = "&".join(fields if order == "right first" else fields[::-1])
    response = client.post(ECHO, content=body, headers={
        "content-type": "application/x-www-form-urlencoded"})
    assert response.status_code == 403


def test_an_old_token_does_not_work_for_a_new_session(client):
    old = token_of(client.get(PAGES[0]))
    client.cookies.clear()
    client.get(PAGES[0])
    assert client.post(ECHO, headers={"X-CSRF-Token": old}).status_code == 403


def test_a_non_ascii_token_is_a_refusal_not_a_crash(client):
    client.get(PAGES[0])
    response = client.post(ECHO, data={"csrf_token": "é" * 43})
    assert response.status_code == 403


def test_a_malformed_cookie_header_is_a_refusal_not_a_crash(client):
    response = client.post(ECHO, headers={"cookie": 'a="\\'})
    assert response.status_code == 403


@pytest.mark.parametrize("other", ['prefs={"a":1}', "x=hello world", "x=a\\b", "a/b=1"])
@pytest.mark.parametrize("secure", [False, True], ids=["dev", "header"])
def test_another_apps_cookie_does_not_hide_ours(other, secure):
    """Every app on the host shares its cookies; on 127.0.0.1, every port does."""
    client = with_routes(header_settings(), PROXY) if secure else with_routes(dev_settings())
    client.headers[USER_HEADER] = "j.silva"
    token = token_of(client.get(PAGES[0]))
    client.cookies.clear()
    cookies = f"{other}; {csrf.cookie_name(secure)}={token}"
    response = client.post(ECHO, content="body",
                           headers={"cookie": cookies, "X-CSRF-Token": token})
    assert response.status_code == 200


@pytest.mark.parametrize("kind", ["application/json", "multipart/form-data",
                                  "text/plain"])
def test_only_a_form_can_carry_the_field(client, kind):
    token = token_of(client.get(PAGES[0]))
    response = client.post(ECHO, content=f"csrf_token={token}",
                           headers={"content-type": kind})
    assert response.status_code == 403


def test_a_body_over_the_limit_is_not_read_to_the_end(client):
    token = token_of(client.get(PAGES[0]))
    body = f"csrf_token={token}&comment=" + "x" * csrf.MAX_FORM_BYTES
    response = client.post(ECHO, content=body, headers={
        "content-type": "application/x-www-form-urlencoded"})
    assert response.status_code == 413


def test_the_form_check_stops_reading_at_the_limit():
    """Driven directly, so the cap in front of the route cannot stand in for it."""
    token = "t" * 43
    chunk, chunks = b"x" * 65536, 4 * csrf.MAX_FORM_BYTES // 65536
    read = 0

    async def receive():
        nonlocal read
        read += 1
        body = f"csrf_token={token}&comment=".encode() if read == 1 else chunk
        return {"type": "http.request", "body": body, "more_body": read < chunks}

    statuses = []

    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    async def route(scope, receive, send):
        raise AssertionError("the route was reached")

    scope = {"type": "http", "method": "POST", "path": ECHO, "headers": [
        (b"cookie", f"{DEV_COOKIE}={token}".encode()),
        (b"content-type", b"application/x-www-form-urlencoded")]}
    asyncio.run(csrf.CsrfMiddleware(route, dev_settings())(scope, receive, send))
    assert statuses == [413]
    assert read <= csrf.MAX_FORM_BYTES // len(chunk) + 2


def test_the_header_path_has_the_same_cap(client):
    token = token_of(client.get(PAGES[0]))
    big = client.post(ECHO, content="x" * (2 * csrf.MAX_FORM_BYTES),
                      headers={"X-CSRF-Token": token})
    assert big.status_code == 413
    assert big.headers["x-content-type-options"] == "nosniff"
    small = client.post(ECHO, content="x" * (csrf.MAX_FORM_BYTES // 2),
                        headers={"X-CSRF-Token": token})
    assert small.status_code == 200


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_every_unsafe_method_is_covered(client, method):
    client.get(PAGES[0])
    assert getattr(client, method)(ECHO).status_code == 403


def test_only_reading_static_files_skips_the_check(client):
    token = token_of(client.get(PAGES[0]))
    assert client.post("/ui/static/app.css").status_code == 403
    assert client.post("/ui/static/app.css",
                       headers={"X-CSRF-Token": token}).status_code == 405


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_writing_to_static_files_is_checked_whatever_the_method(client, method):
    client.get(PAGES[0])
    assert getattr(client, method)("/ui/static/app.css").status_code == 403


def test_the_check_comes_before_the_body_cap(client):
    # A request that cannot prove it read the page is refused as that, whatever
    # its size, and the cap never reads its body.
    client.get(PAGES[0])
    big = client.post(ECHO, content="x" * (2 * csrf.MAX_FORM_BYTES),
                      headers={"Content-Type": "application/octet-stream"})
    assert big.status_code == 403


def test_get_carries_no_requirement(client):
    assert client.get(PAGES[0]).status_code == 200


def test_identity_is_checked_before_the_token():
    client = with_routes(header_settings(), PROXY)
    assert client.post(ECHO).status_code == 401
    client.headers[USER_HEADER] = "j.silva"
    assert client.post(ECHO).status_code == 403
