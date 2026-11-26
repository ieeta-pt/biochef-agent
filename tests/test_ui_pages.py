"""The empty pages and what every one of them shows (part 3)."""

import re

import pytest

from tests.ui_support import PAGES, USER_HEADER, client_for, dev_client, header_settings, proxy_client, PROXY

TITLES = {"/ui/inbox": "Inbox", "/ui/studies": "Studies",
          "/ui/requests": "Latest requests", "/ui/datasets": "Datasets",
          "/ui/node": "Node"}


@pytest.mark.parametrize("path", PAGES)
def test_every_page_opens_for_a_trusted_user(path):
    response = proxy_client().get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert f">{TITLES[path]}</h1>" in response.text


def test_the_root_goes_to_the_inbox():
    response = proxy_client().get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/inbox"


def test_a_host_header_does_not_shape_the_redirect():
    response = proxy_client().get("/", headers={"host": "evil.test"})
    assert "evil.test" not in response.headers["location"]


@pytest.mark.parametrize("path", PAGES)
def test_the_sidebar_marks_only_the_current_page(path):
    text = proxy_client().get(path).text
    assert text.count('aria-current="page"') == 1
    current = re.search(r'<a href="([^"]+)"[^>]*aria-current="page"', text, re.S)
    assert current.group(1) == path


def test_the_sidebar_has_the_five_items_in_order():
    text = proxy_client().get(PAGES[0]).text
    assert re.findall(r'<a href="(/ui/[a-z]+)"', text) == list(PAGES)


def test_the_active_item_uses_the_darker_colours_of_section_5():
    text = proxy_client().get(PAGES[0]).text
    item = re.search(r'<a href="/ui/inbox"[^>]*>', text, re.S).group(0)
    assert "bg-brand-deep" in item and "border-white" in item
    other = re.search(r'<a href="/ui/studies"[^>]*>', text, re.S).group(0)
    assert "hover:bg-brand-hover" in other


def test_the_counts_stay_empty():
    assert "text-brand-strong" not in proxy_client().get(PAGES[0]).text


def test_the_sidebar_says_who_and_which_node():
    text = proxy_client().get(PAGES[0]).text
    assert "Signed in through the TRE · j.silva" in text
    assert "Agent · Example TRE (test)" in text


def test_the_sidebar_does_not_invent_a_coordinator():
    assert "Coordinator not connected yet" in proxy_client().get(PAGES[0]).text


def test_the_policy_engine_link_appears_only_when_configured():
    assert "Policy Engine" not in proxy_client().get(PAGES[0]).text
    url = "https://policy.tre.test/"
    text = dev_client(policy_engine_url=url).get(PAGES[0]).text
    assert f'href="{url}"' in text and 'rel="noopener noreferrer"' in text


@pytest.mark.parametrize("path", PAGES)
def test_dev_identity_is_red_on_every_page(path):
    text = dev_client().get(path).text
    bar = re.search(r'<p role="status" class="([^"]+)">Development identity', text)
    assert bar and "bg-bad" in bar.group(1)


@pytest.mark.parametrize("path", PAGES)
def test_the_red_bar_is_only_for_dev(path):
    assert "Development identity" not in proxy_client().get(path).text


def test_a_hostile_user_name_is_text():
    client = client_for(header_settings(), PROXY)
    response = client.get(PAGES[0], headers={USER_HEADER: "<script>x</script>"})
    assert "<script>x" not in response.text
    assert "&lt;script&gt;x&lt;/script&gt;" in response.text


def test_a_hostile_node_name_is_text():
    text = dev_client(node_name='"><img src=x>').get(PAGES[0]).text
    assert "<img src=x>" not in text


def test_every_control_has_a_room_of_44px():
    for link in re.findall(r'<a href="/ui/[^>]*>', proxy_client().get(PAGES[0]).text, re.S):
        assert "min-h-11" in link
