"""Who is using the page, and every way the answer cannot be believed (section 4)."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from store.repo import MAX_NAME_LENGTH
from tests.ui_support import (LOOPBACK, PAGES, PROXY, USER_HEADER, app_for,
                              client_for, dev_settings, header_settings, proxy_client)
from ui.identity import MAX_USER_LENGTH, WEBSOCKET_REFUSED, who


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


UNBELIEVABLE = pytest.mark.parametrize("headers", [
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
    [named(b"a" * (MAX_USER_LENGTH + 1))],
], ids=["absent", "other", "empty", "blank", "repeated", "nul", "newline", "escape",
        "delete", "separator", "not-utf8", "too-long"])


@UNBELIEVABLE
def test_a_header_that_cannot_be_believed(headers):
    assert who(scope(headers=headers), header_settings()) is None


def test_a_name_may_be_as_long_as_the_store_allows():
    name = "a" * MAX_USER_LENGTH
    assert who(scope(headers=[named(name.encode())]), header_settings()) == name
    assert MAX_USER_LENGTH == MAX_NAME_LENGTH


# On a unix socket: whoever can open it is the proxy

SOCKET = "/run/biochef-ui/ui.sock"


def on_the_socket(headers=(), path=SOCKET):
    # What uvicorn reports for a unix socket: no peer, and the path as the server.
    return {"client": None, "server": (path, None), "headers": list(headers)}


def socket_settings(**overrides):
    return header_settings(**{"trusted_proxies": (), "socket": SOCKET, **overrides})


def test_on_the_socket_the_proxy_names_the_user():
    assert who(on_the_socket([named(b"j.silva")]), socket_settings()) == "j.silva"


@pytest.mark.parametrize("address", [PROXY, LOOPBACK, "::1"])
def test_with_a_socket_no_address_is_trusted(address):
    # Not even one the address list would have trusted: there is none.
    request = {**scope(address, [named(b"j.silva")]), "server": (SOCKET, None)}
    settings = socket_settings(trusted_proxies=header_settings().trusted_proxies)
    assert who(request, settings) is None


@pytest.mark.parametrize("server", [("/run/other/ui.sock", None), ("127.0.0.1", 8081),
                                    None])
def test_only_a_request_on_that_socket_counts(server):
    request = {"client": None, "server": server, "headers": [named(b"j.silva")]}
    assert who(request, socket_settings()) is None


@UNBELIEVABLE
def test_on_the_socket_the_header_still_has_to_be_believable(headers):
    assert who(on_the_socket(headers), socket_settings()) is None


def test_dev_mode_on_a_socket_is_nobody():
    assert who(on_the_socket([named(b"j.silva")]),
               dev_settings(socket=SOCKET)) is None


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


# Websockets and lifespan


def with_a_websocket(settings):
    """An app with a websocket route, which the shell does not have yet."""
    app = app_for(settings)

    @app.websocket("/ui/_/socket")
    async def socket(websocket: WebSocket):  # unannotated, FastAPI reads a query parameter
        await websocket.accept()
        await websocket.send_text("served")
        await websocket.close()

    return app


@pytest.mark.parametrize("path", ["/ui/_/socket", "/ui/static/app.css", PAGES[0]])
@pytest.mark.parametrize("who_asks", ["dev", "untrusted", "trusted proxy"])
def test_a_websocket_is_closed_for_everyone(path, who_asks):
    settings, address, headers = {
        "dev": (dev_settings(), LOOPBACK, {}),
        "untrusted": (header_settings(), "10.9.9.9", {USER_HEADER: "j.silva"}),
        "trusted proxy": (header_settings(), PROXY, {USER_HEADER: "j.silva"}),
    }[who_asks]
    client = TestClient(with_a_websocket(settings), client=(address, 50000))
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(path, headers=headers):
            pass
    assert closed.value.code == WEBSOCKET_REFUSED


def test_lifespan_still_reaches_the_app():
    with client_for(dev_settings()) as client:  # runs startup and shutdown
        assert client.get(PAGES[0]).status_code == 200
