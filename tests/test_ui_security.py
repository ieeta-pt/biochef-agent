"""The headers, and that nothing the page loads leaves the agent (section 9)."""

import hashlib
import json
import re

import pytest

from tests.ui_support import (PAGES, PROXY, REPO_ROOT, UI, client_for, dev_settings,
                              header_settings, proxy_client, with_routes)
from ui.security import CONTENT_SECURITY_POLICY

POLICY = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
          "font-src 'self'; connect-src 'self'; form-action 'self'; "
          "frame-ancestors 'none'; base-uri 'none'")

STATIC = UI / "static"
TEMPLATES = sorted((UI / "templates").rglob("*.html"))


def assert_security_headers(response):
    assert response.headers["content-security-policy"] == POLICY
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_the_policy_is_the_one_in_the_spec():
    assert CONTENT_SECURITY_POLICY == POLICY


@pytest.mark.parametrize("path", PAGES)
def test_every_page_has_the_headers(path):
    response = proxy_client().get(path)
    assert_security_headers(response)
    assert response.headers["cache-control"] == "no-store"


def test_a_redirect_has_them():
    assert_security_headers(proxy_client().get("/"))


def test_a_refusal_has_them():
    response = client_for(header_settings(), PROXY).get(PAGES[0])
    assert response.status_code == 401
    assert_security_headers(response)


def test_a_csrf_refusal_has_them():
    response = with_routes(dev_settings()).post("/ui/_/echo")
    assert response.status_code == 403
    assert_security_headers(response)


def test_a_missing_page_has_them():
    response = proxy_client().get("/ui/nonsense")
    assert response.status_code == 404
    assert_security_headers(response)


def test_a_crash_has_them_and_says_nothing():
    client = with_routes(dev_settings(), raise_server_exceptions=False)
    response = client.get("/ui/_/crash")
    assert response.status_code == 500
    assert_security_headers(response)
    assert "secret detail" not in response.text


def test_a_crash_still_reaches_the_log():
    client = with_routes(dev_settings())
    with pytest.raises(RuntimeError):
        client.get("/ui/_/crash")


def test_static_files_have_the_headers_and_may_be_cached():
    response = proxy_client().get("/ui/static/app.css")
    assert_security_headers(response)
    assert "cache-control" not in response.headers


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_no_documentation_pages(path):
    assert proxy_client().get(path).status_code == 404


def test_static_cannot_escape_its_directory():
    client = proxy_client()
    assert client.get("/ui/static/../settings.py").status_code == 404
    assert client.get("/ui/static/%2e%2e/settings.py").status_code == 404


# No inline code, nothing external


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.name)
def test_no_inline_style_or_script_in_templates(template):
    text = template.read_text()
    assert not re.search(r"\bstyle\s*=", text)
    assert "<style" not in text
    assert not re.search(r"<script(?![^>]*\bsrc=)", text)
    assert not re.search(r"\bon[a-z]+\s*=", text)


def test_the_templates_were_found():
    assert len(TEMPLATES) == 6


def references(text):
    """Everything an HTML page or stylesheet would load."""
    found = re.findall(r"""(?:src|href)\s*=\s*["']([^"']+)""", text)
    found += re.findall(r"""url\(\s*["']?([^"')]+)""", text)
    found += re.findall(r"""@import\s+["']([^"']+)""", text)
    return found


def loads_from_the_agent(reference):
    return reference.startswith("/ui/") and not reference.startswith("//")


@pytest.mark.parametrize("path", PAGES)
def test_a_rendered_page_loads_only_from_the_agent(path):
    client = proxy_client()
    text = client.get(path).text
    loaded = references(text)
    assert loaded
    assert all(loads_from_the_agent(item) for item in loaded), loaded
    assert "http://" not in text and "https://" not in text


def test_everything_a_page_loads_is_there():
    client = proxy_client()
    page = client.get(PAGES[0]).text
    css = client.get("/ui/static/app.css")
    for item in {*references(page), *references(css.text)}:
        response = client.get(item)
        assert response.status_code == 200, item


def test_the_stylesheet_is_the_right_type():
    response = proxy_client().get("/ui/static/app.css")
    assert response.headers["content-type"].startswith("text/css")


def test_the_stylesheet_loads_nothing_external():
    css = (STATIC / "app.css").read_text()
    assert all(loads_from_the_agent(item) for item in references(css))
    assert "@import" not in css


def test_our_script_loads_nothing_external():
    script = (STATIC / "app.js").read_text()
    assert not re.search(r"https?://", script)
    assert not re.search(r"\beval\(|new Function", script)


def test_htmx_is_the_pinned_one_and_names_no_host():
    text = (STATIC / "vendor" / "htmx-2.0.11.min.js").read_text()
    assert 'version:"2.0.11"' in text
    assert not re.search(r"https?://", text)


def test_the_logo_loads_nothing_external():
    svg = (STATIC / "img" / "biochef-white.svg").read_text()
    assert "<script" not in svg and "<image" not in svg
    assert all(item.startswith("#") for item in re.findall(r"url\(([^)]+)\)", svg))
    assert "href" not in svg


def test_htmx_is_configured_with_eval_off():
    text = proxy_client().get(PAGES[0]).text
    content = re.search(r"""<meta name="htmx-config" content='([^']+)'""", text).group(1)
    config = json.loads(content)
    assert config["allowEval"] is False
    assert config["includeIndicatorStyles"] is False
    assert config["historyCacheSize"] == 0


def test_app_js_sends_the_token_the_middleware_reads():
    script = (STATIC / "app.js").read_text()
    assert '"X-CSRF-Token"' in script and 'meta[name="csrf-token"]' in script


# Vendored files


def sums():
    lines = (UI / "static.sha256").read_text().splitlines()
    return {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in lines}


def vendored():
    for directory in ("vendor", "fonts", "img"):
        for path in (STATIC / directory).rglob("*"):
            if path.is_file() and not path.name.startswith("LICENSE-"):
                yield path.relative_to(STATIC).as_posix()


def test_every_vendored_file_is_listed_and_every_listed_file_exists():
    assert set(vendored()) == set(sums())


@pytest.mark.parametrize("name,expected", sorted(sums().items()))
def test_a_vendored_file_is_the_one_that_was_checked(name, expected):
    assert hashlib.sha256((STATIC / name).read_bytes()).hexdigest() == expected


def test_the_ten_font_files_of_the_spec():
    fonts = sorted(path.name for path in (STATIC / "fonts").glob("*.woff2"))
    assert len(fonts) == 10
    weights = {("roboto-mono" if "mono" in name else "roboto",
                re.search(r"-(\d{3})-", name).group(1)) for name in fonts}
    assert weights == {("roboto", "400"), ("roboto", "500"), ("roboto", "700"),
                       ("roboto-mono", "400"), ("roboto-mono", "500")}


def test_fonts_are_woff2():
    for path in (STATIC / "fonts").glob("*.woff2"):
        assert path.read_bytes()[:4] == b"wOF2"


def test_the_licences_are_there():
    assert "Zero-Clause BSD" in (STATIC / "vendor" / "LICENSE-htmx").read_text()
    fonts = (STATIC / "fonts" / "LICENSE-fonts").read_text()
    assert fonts.count("SIL Open Font License") >= 2
    assert "Roboto Project Authors" in fonts and "Roboto Mono Project Authors" in fonts


def test_the_tailwind_cli_is_pinned_for_both_platforms():
    script = (UI / "tailwind" / "build.sh").read_text()
    assert "VERSION=4.3.3\n" in script
    pinned = (UI / "tailwind" / "SHA256SUMS").read_text().splitlines()
    assert {line.split("  ")[1] for line in pinned} == {
        "tailwindcss-linux-x64", "tailwindcss-macos-arm64"}
    assert all(re.fullmatch(r"[0-9a-f]{64}", line.split("  ")[0]) for line in pinned)


def test_the_built_stylesheet_is_committed_with_the_tokens():
    css = (STATIC / "app.css").read_text()
    assert "#00574b" in css.lower() and "#00796b" in css.lower()


def test_only_the_palette_of_the_spec_compiles():
    source = (UI / "tailwind" / "input.css").read_text()
    assert "--color-*: initial;" in source


def test_the_api_does_not_serve_the_page():
    source = (REPO_ROOT / "main.py").read_text()
    assert not re.search(r"^\s*(from|import)\s+ui\b", source, re.M)
