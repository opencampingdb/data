"""Website and social page parsing for the Overture importer.

Reads the Facebook, Instagram, TikTok and X page a link gives, in canonical
form, and the host of a website. See pipeline/README.md "Overture import",
"Gap fills".
"""

from __future__ import annotations

import re
from typing import Iterable

# The social page facts, and the hosts (a subdomain counts too) each is read from.
SOCIAL_HOSTS = {
    "facebook": ("facebook.com", "fb.com"),
    "instagram": ("instagram.com",),
    "tiktok": ("tiktok.com",),
    "x": ("x.com", "twitter.com"),
}


SOCIAL_FACTS = tuple(SOCIAL_HOSTS)
SOCIAL_HOST_LIST = tuple(host for hosts in SOCIAL_HOSTS.values() for host in hosts)


def host_of(url: str) -> str:
    """The host of a URL, lower case, without a leading www."""
    match = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#:]+)", url.strip().lower())
    host = match.group(1) if match else url.strip().lower()
    return host[4:] if host.startswith("www.") else host


def on_host(host: str, hosts: Iterable[str]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


# First path pieces that are a post, photo, event, share link or a site page,
# never a social page, for each platform.
NOT_A_PAGE = {
    "facebook": {
        "photo", "photo.php", "photos", "posts", "permalink.php", "story.php", "events", "event", "share",
        "sharer", "sharer.php", "watch", "video", "videos", "reel", "reels", "groups", "hashtag", "media",
        "login", "dialog", "l.php", "plugins", "public", "search", "marketplace", "help",
    },
    "instagram": {"p", "reel", "reels", "tv", "stories", "explore", "accounts", "share", "direct", "about"},
    "tiktok": set(),
    "x": {"i", "intent", "share", "home", "search", "hashtag", "status", "explore", "settings", "login"},
}


FACEBOOK_NAME = re.compile(r"^[A-Za-z0-9.\-]{2,100}$")
INSTAGRAM_NAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")
TIKTOK_NAME = re.compile(r"^@[A-Za-z0-9._]{2,24}$")
X_NAME = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def facebook_page(host: str, parts: list[str], query: str) -> str | None:
    """A Facebook page URL from a link's host, path pieces and query, or None."""
    if host not in ("facebook.com", "m.facebook.com", "web.facebook.com", "business.facebook.com", "fb.com"):
        # Country and language subdomains (en-gb.facebook.com) are the same pages.
        if not re.match(r"^[a-z]{2}(-[a-z]{2})?\.facebook\.com$", host):
            return None
    first = parts[0]
    if first.lower() == "profile.php":
        ids = re.search(r"[?&]id=(\d+)", query)
        return f"https://www.facebook.com/profile.php?id={ids.group(1)}" if ids and len(parts) == 1 else None
    if first.lower() == "pages" and len(parts) >= 3 and parts[2].isdigit():
        return f"https://www.facebook.com/{parts[2]}"
    # A page with no user name: people/<name>/<id> or p/<name>-<id>.
    if first.lower() == "people" and len(parts) == 3 and parts[2].isdigit():
        return f"https://www.facebook.com/profile.php?id={parts[2]}"
    page_id = re.search(r"-(\d{6,})$", parts[1]) if first.lower() == "p" and len(parts) == 2 else None
    if page_id:
        return f"https://www.facebook.com/profile.php?id={page_id.group(1)}"
    if first.lower() == "pg" and len(parts) >= 2:
        first, parts = parts[1], parts[1:]
    if len(parts) > 1 or not FACEBOOK_NAME.match(first):
        return None
    return f"https://www.facebook.com/{first}"


def named_page(hosts: tuple[str, ...] | None, name: re.Pattern, template: str):
    """A page reader for a platform whose page is one path piece, a user name.

    hosts: the hosts a page is read from, or None for any of the platform's.
    """

    def page(host: str, parts: list[str], query: str) -> str | None:
        if (hosts is not None and host not in hosts) or len(parts) > 1 or not name.match(parts[0]):
            return None
        return template.format(parts[0])

    return page


# How each platform's page URL is read from a link on one of its hosts: the
# host, the path pieces and the query in, the canonical page URL or None out.
PAGE_READERS = {
    "facebook": facebook_page,
    "instagram": named_page(None, INSTAGRAM_NAME, "https://www.instagram.com/{}/"),
    "tiktok": named_page(("tiktok.com",), TIKTOK_NAME, "https://www.tiktok.com/{}"),
    "x": named_page(("x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com"), X_NAME, "https://x.com/{}"),
}


def social_page(url: str) -> tuple[str, str] | None:
    """The social fact and canonical https page URL a link gives, or None.

    Only a page (a profile or business page) counts. A post, photo, event,
    video or share link, a short link, or a link on any other host gives None.
    The query and fragment are dropped, except Facebook's profile.php id.
    """
    url = (url or "").strip()
    match = re.match(r"^https?://([^/?#]+)(/[^?#]*)?(\?[^#]*)?", url, re.IGNORECASE)
    if not match:
        return None
    host = match.group(1).lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    key = next((k for k, hosts in SOCIAL_HOSTS.items() if on_host(host, hosts)), None)
    if key is None:
        return None
    parts = [part for part in (match.group(2) or "").split("/") if part]
    if not parts or parts[0].lower() in NOT_A_PAGE[key]:
        return None
    page = PAGE_READERS[key](host, parts, match.group(3) or "")
    return (key, page) if page else None


def page_key(url: str) -> str:
    """A website or social page as compared for SHARED_PAGE_LIMIT: lower case, no trailing slash."""
    return url.strip().lower().rstrip("/")


def all_social_pages(row: dict) -> list[tuple[str, str]]:
    """Every social page an Overture place gives, as (fact, page), socials first, then websites."""
    pages = [social_page(url) if isinstance(url, str) else None for url in [*(row.get("socials") or []), *(row.get("websites") or [])]]
    return [page for page in pages if page and len(page[1]) <= 500]


def social_pages(row: dict, shared: frozenset[str] = frozenset()) -> dict[str, str]:
    """Each social fact's page: the first from Overture's socials, then from its websites, leaving out shared pages."""
    out = {}
    for key, page in all_social_pages(row):
        if key not in out and page_key(page) not in shared:
            out[key] = page
    return out
