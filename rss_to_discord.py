#!/usr/bin/env python3
"""Post new RSS feed items to a Discord webhook.

Dependency-free on purpose so it can run locally or in GitHub Actions.
"""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import html
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


DEFAULT_FEED_URL = "https://streeteconomics.substack.com/feed"
DEFAULT_STATE_PATH = ".rss_state.json"
DISCORD_LIMIT = 10


@dataclass(frozen=True)
class FeedItem:
    id: str
    title: str
    link: str
    summary: str
    image_url: str
    published_at: float


class FirstImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.image_url = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.image_url or tag.lower() != "img":
            return
        attr_map = {key.lower(): value for key, value in attrs if value}
        self.image_url = attr_map.get("src", "")


def insecure_ssl_enabled() -> bool:
    return os.getenv("ALLOW_INSECURE_SSL", "").lower() in ("1", "true", "yes")


def urlopen_context() -> ssl.SSLContext | None:
    return ssl._create_unverified_context() if insecure_ssl_enabled() else None


def text_at(parent: ET.Element, path: str, namespaces: dict[str, str] | None = None) -> str:
    child = parent.find(path, namespaces or {})
    if child is None or child.text is None:
        return ""
    return " ".join(html.unescape(child.text).split())


def attr_at(parent: ET.Element, path: str, attr: str, namespaces: dict[str, str] | None = None) -> str:
    child = parent.find(path, namespaces or {})
    if child is None:
        return ""
    return child.attrib.get(attr, "")


def strip_html(value: str, max_chars: int = 280) -> str:
    in_tag = False
    out: list[str] = []
    for char in html.unescape(value):
        if char == "<":
            in_tag = True
            continue
        if char == ">":
            in_tag = False
            continue
        if not in_tag:
            out.append(char)
    text = " ".join("".join(out).split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rsplit(" ", 1)[0] + "..."


def first_html_image(value: str) -> str:
    parser = FirstImageParser()
    parser.feed(value)
    return html.unescape(parser.image_url)


def image_from_enclosure(item: ET.Element) -> str:
    for enclosure in item.findall("enclosure"):
        content_type = enclosure.attrib.get("type", "")
        url = enclosure.attrib.get("url", "")
        if url and content_type.startswith("image/"):
            return url
    return ""


def parse_date(value: str) -> float:
    if not value:
        return 0

    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    except (TypeError, ValueError):
        pass

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def stable_id(title: str, link: str) -> str:
    raw = f"{title}\n{link}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def feed_request(feed_url: str) -> urllib.request.Request:
    return urllib.request.Request(
        feed_url,
        headers={
            "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        },
    )


def feed_url_candidates(feed_url: str) -> list[str]:
    parsed_url = urllib.parse.urlparse(feed_url)
    query = urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=True)
    query.append(("rss", "true"))
    fallback = urllib.parse.urlunparse(parsed_url._replace(query=urllib.parse.urlencode(query)))
    return [feed_url] if fallback == feed_url else [feed_url, fallback]


def load_feed(feed_url: str) -> list[FeedItem]:
    parsed_url = urllib.parse.urlparse(feed_url)
    if parsed_url.scheme in ("", "file"):
        xml_bytes = Path(urllib.request.url2pathname(parsed_url.path or feed_url)).read_bytes()
    else:
        last_error: Exception | None = None
        try:
            for candidate_url in feed_url_candidates(feed_url):
                request = feed_request(candidate_url)
                try:
                    with urllib.request.urlopen(request, timeout=30, context=urlopen_context()) as response:
                        xml_bytes = response.read()
                    break
                except urllib.error.HTTPError as error:
                    last_error = error
                    if error.code != 403:
                        raise
            else:
                raise last_error or RuntimeError("Could not fetch feed.")
        except urllib.error.URLError as error:
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                raise RuntimeError(
                    "Could not verify the feed site's SSL certificate. On macOS python.org installs, "
                    "run the bundled 'Install Certificates.command', run this in GitHub Actions, "
                    "or set ALLOW_INSECURE_SSL=1 for a local one-off test."
                ) from error
            raise

    root = ET.fromstring(xml_bytes)
    namespaces = {
        "atom": "http://www.w3.org/2005/Atom",
        "content": "http://purl.org/rss/1.0/modules/content/",
    }

    if root.tag.endswith("feed"):
        raw_items = root.findall("{http://www.w3.org/2005/Atom}entry")
        return [parse_atom_item(item, namespaces) for item in raw_items]

    raw_items = root.findall("./channel/item")
    return [parse_rss_item(item, namespaces) for item in raw_items]


def parse_rss_item(item: ET.Element, namespaces: dict[str, str]) -> FeedItem:
    title = text_at(item, "title")
    link = text_at(item, "link")
    guid = text_at(item, "guid") or stable_id(title, link)
    content = text_at(item, "content:encoded", namespaces)
    summary = text_at(item, "description") or content
    image_url = image_from_enclosure(item) or first_html_image(content)
    published = text_at(item, "pubDate")
    return FeedItem(
        id=guid,
        title=title or "New post",
        link=link,
        summary=strip_html(summary),
        image_url=image_url,
        published_at=parse_date(published),
    )


def parse_atom_item(item: ET.Element, namespaces: dict[str, str]) -> FeedItem:
    title = text_at(item, "atom:title", namespaces)
    link = attr_at(item, "atom:link", "href", namespaces)
    guid = text_at(item, "atom:id", namespaces) or stable_id(title, link)
    content = text_at(item, "atom:content", namespaces)
    summary = text_at(item, "atom:summary", namespaces) or content
    image_url = first_html_image(content)
    published = text_at(item, "atom:published", namespaces) or text_at(item, "atom:updated", namespaces)
    return FeedItem(
        id=guid,
        title=title or "New post",
        link=link,
        summary=strip_html(summary),
        image_url=image_url,
        published_at=parse_date(published),
    )


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"seen_ids": []}
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_state(path: Path, seen_ids: set[str]) -> None:
    path.write_text(
        json.dumps({"seen_ids": sorted(seen_ids)}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def webhook_urls_from(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [url.strip() for url in value if isinstance(url, str) and url.strip()]

    urls: list[str] = []
    for line in value.splitlines():
        urls.extend(part.strip() for part in line.split(",") if part.strip())
    return urls


def post_to_discord(webhook_url: str, item: FeedItem, index: int, dry_run: bool = False) -> None:
    payload = {
        "embeds": [
            {
                "title": item.title[:256],
                "url": item.link,
                "description": item.summary[:4096] if item.summary else None,
                "image": {"url": item.image_url} if item.image_url else None,
                "timestamp": (
                    datetime.fromtimestamp(item.published_at, tz=timezone.utc).isoformat()
                    if item.published_at
                    else None
                ),
            }
        ],
    }

    payload["embeds"][0] = {key: value for key, value in payload["embeds"][0].items() if value}
    body = json.dumps(payload).encode("utf-8")

    if dry_run:
        print(f"Webhook #{index}:")
        print(json.dumps(payload, indent=2))
        return

    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "rss-to-discord/1.0"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30, context=urlopen_context()) as response:
            response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord webhook failed: HTTP {error.code}: {detail}") from error


def read_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def main() -> int:
    parser = argparse.ArgumentParser(description="Post new RSS items to a Discord webhook.")
    parser.add_argument("--config", type=Path, help="Optional JSON config file.")
    parser.add_argument("--feed-url", default=os.getenv("RSS_FEED_URL"))
    parser.add_argument("--webhook-url", default=os.getenv("DISCORD_WEBHOOK_URL"))
    parser.add_argument("--webhook-urls", default=os.getenv("DISCORD_WEBHOOK_URLS"))
    parser.add_argument("--state-path", default=os.getenv("RSS_STATE_PATH", DEFAULT_STATE_PATH))
    parser.add_argument("--max-posts", type=int, default=int(os.getenv("MAX_POSTS", "3")))
    parser.add_argument("--dry-run", action="store_true", help="Print Discord payloads without posting.")
    parser.add_argument(
        "--mark-existing",
        action="store_true",
        help="Mark current feed items as seen without posting them.",
    )
    args = parser.parse_args()

    config = read_config(args.config)
    feed_url = args.feed_url or config.get("feed_url") or DEFAULT_FEED_URL
    webhook_urls = (
        webhook_urls_from(args.webhook_urls)
        or webhook_urls_from(config.get("discord_webhook_urls"))
        or webhook_urls_from(args.webhook_url)
        or webhook_urls_from(config.get("discord_webhook_url"))
    )
    state_path = Path(config.get("state_path") or args.state_path)
    max_posts = int(config.get("max_posts") or args.max_posts)

    if not webhook_urls and not args.dry_run and not args.mark_existing:
        print(
            "Missing Discord webhook. Set DISCORD_WEBHOOK_URL, DISCORD_WEBHOOK_URLS, "
            "discord_webhook_url, or discord_webhook_urls.",
            file=sys.stderr,
        )
        return 2

    items = sorted(load_feed(feed_url), key=lambda item: item.published_at or time.time())
    state = load_state(state_path)
    seen_ids = set(state.get("seen_ids", []))

    if args.mark_existing:
        save_state(state_path, seen_ids | {item.id for item in items})
        print(f"Marked {len(items)} current feed items as seen.")
        return 0

    new_items = [item for item in items if item.id not in seen_ids]
    posts_to_send = new_items[-min(max_posts, DISCORD_LIMIT) :]

    for item in posts_to_send:
        for index, webhook_url in enumerate(webhook_urls or ["dry-run"], start=1):
            post_to_discord(webhook_url, item, index=index, dry_run=args.dry_run)
        verb = "Previewed" if args.dry_run else "Posted"
        print(f"{verb}: {item.title} ({item.link}) to {len(webhook_urls) or 1} webhook(s)")
        seen_ids.add(item.id)

    if new_items and len(posts_to_send) < len(new_items):
        skipped = new_items[: len(new_items) - len(posts_to_send)]
        seen_ids.update(item.id for item in skipped)
        print(f"Skipped {len(skipped)} older item(s) because max_posts={max_posts}.")

    if args.dry_run:
        print("Dry run only. State file was not changed.")
    else:
        save_state(state_path, seen_ids)
    print(f"Done. New posts sent: {len(posts_to_send)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
