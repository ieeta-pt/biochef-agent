"""Who is using the page, and every way the answer cannot be believed (section 4)."""

import pytest

from tests.ui_support import (LOOPBACK, PAGES, PROXY, USER_HEADER, client_for,
                              dev_settings, header_settings, proxy_client)
from ui.identity import who


def scope(address=PROXY, headers=()):
    client = None if address is None else (address, 50000)
    return {"client": client, "headers": list(headers)}


def named(value):
    return (USER_HEADER.lower().encode(), value)


# Header mode


def test_a_trusted_proxy_names_the_user():
    assert who(scope(headers=[named(b"j.silva")]), header_settings()) == "j.silva"


def test_the_header_name_is_not_case_sensitive():
    headers = [(b"x-forwarded-user", b"j.silva")]
    assert who(scope(headers=headers),
               header_settings(identity_header="X-FORWARDED-USER")) == "j.silva"


def test_a_name_may_be_more_than_ascii():
    value = "joão".encode()
    assert who(scope(headers=[named(value)]), header_settings()) == "joão"


@pytest.mark.parametrize("address", ["10.0.1.5", "192.168.0.1", "127.0.0.1",
                                     "::1", "::ffff:10.0.1.5"])
def test_an_untrusted_address_cannot_name_anyone(address):
    assert who(scope(address, [named(b"j.silva")]), header_settings()) is None


def test_an_ipv4_proxy_seen_through_a_dual_stack_socket_is_still_that_proxy():
    assert who(scope("::ffff:10.0.0.5", [named(b"j.silva")]),
               header_settings()) == "j.silva"


@pytest.mark.parametrize("address", [None, "testclient", "unix:/tmp/socket"])
def test_no_usable_address_is_no_trust(address):
    assert who(scope(address, [named(b"j.silva")]), header_settings()) is None


@pytest.mark.parametrize("headers", [
    [],
    [(b"x-other-user", b"j.silva")],
    [named(b"")],
    [named(b"   ")],
    [named(b"a.costa"), named(b"j.silva")],
    [named(b"j.silva\x00")],
    [named(b"j.silva\nevent")],
    [named(b"j\x1bsilva")],
    [named(b"j\x7fsilva")],
    [named("j silva".encode())],
    [named(b"\xff\xfe")],
], ids=["absent", "other", "empty", "blank", "repeated", "nul", "newline", "escape",
        "delete", "separator", "not-utf8"])
def test_a_header_that_cannot_be_believed(headers):
    assert who(scope(headers=headers), header_settings()) is None


# Dev mode


@pytest.mark.parametrize("address", ["127.0.0.1", "127.0.0.2", "::1",
                                     "::ffff:127.0.0.1"])
def test_dev_mode_serves_loopback(address):
    assert who(scope(address), dev_settings()) == "dev.user"


@pytest.mark.parametrize("address", ["10.0.0.5", "192.168.1.9", "fd00::1",
                                     "::ffff:10.0.0.5", None, "testclient"])
def test_dev_mode_refuses_anything_else_whatever_the_host_setting_said(address):
    assert who(scope(address), dev_settings()) is None


def test_dev_mode_ignores_the_header():
    assert who(scope(LOOPBACK, [named(b"a.costa")]), dev_settings()) == "dev.user"


# Over HTTP


EVERYTHING = (*PAGES, "/", "/ui/static/app.css", "/ui/static/app.js",
              "/ui/static/vendor/htmx-2.0.11.min.js", "/docs", "/redoc",
              "/openapi.json", "/nonsense")


@pytest.mark.parametrize("path", EVERYTHING)
def test_no_identity_no_anything(path):
    response = client_for(header_settings(), PROXY).get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "TRE-Proxy"
    assert "<html" not in response.text


@pytest.mark.parametrize("path", EVERYTHING)
def test_an_untrusted_address_gets_nothing_even_with_the_header(path):
    client = client_for(header_settings(), "10.9.9.9")
    assert client.get(path, headers={USER_HEADER: "j.silva"}).status_code == 401


@pytest.mark.parametrize("path", PAGES)
def test_dev_identity_from_elsewhere_gets_nothing(path):
    assert client_for(dev_settings(), "10.0.0.5").get(path).status_code == 401


def test_a_refused_request_leaves_no_cookie():
    assert "set-cookie" not in client_for(header_settings(), PROXY).get(PAGES[0]).headers


def test_the_refusal_says_nothing_about_who_is_trusted():
    text = client_for(header_settings(), PROXY).get(PAGES[0]).text
    assert PROXY not in text and USER_HEADER not in text


def test_a_trusted_request_is_served():
    assert proxy_client().get(PAGES[0]).status_code == 200
