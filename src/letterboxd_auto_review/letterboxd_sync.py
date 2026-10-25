"""Merge a public Letterboxd RSS feed into the local export-based history.

RSS is a recent-activity window. Keep older records, and never infer deletions
from entries disappearing from that window. This module never edits drafts.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from letterboxd_auto_review.movie_files import title_key, validate_watched_date

MAX_FEED_BYTES = 2_000_000
LB = "{https://letterboxd.com}"


def write_json(path: Path, value) -> None:
    """Write complete JSON before replacing the previous local copy."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_username(username: str) -> str:
    """Accept a profile name, never an arbitrary URL or filesystem path."""

    if not isinstance(username, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", username):
        raise ValueError("Enter your Letterboxd username, without a URL or @ sign.")
    return username


def entry_url(value: str, username: str) -> str:
    """Use the profile's film-entry URL as identity, even if its RSS GUID changes."""

    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc.lower() not in ("letterboxd.com", "www.letterboxd.com"):
        raise ValueError("The feed contains an invalid film-entry link.")
    path = parsed.path.strip("/").split("/")
    if len(path) < 3 or path[0].casefold() != username.casefold() or path[1] != "film":
        raise ValueError("The feed contains a film entry from another profile.")
    return "https://letterboxd.com/" + "/".join([path[0].lower(), *path[1:]]) + "/"


class ReviewText(HTMLParser):
    """Keep paragraphs and simple emphasis, dropping the feed's poster HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden:
            if tag in {"p", "div", "blockquote", "li"}:
                self.parts.append("\n\n")
            elif tag == "br":
                self.parts.append("\n")
            elif tag in {"i", "em"}:
                self.parts.append("*")
            elif tag in {"b", "strong"}:
                self.parts.append("**")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden:
            if tag in {"p", "div", "blockquote", "li"}:
                self.parts.append("\n\n")
            elif tag in {"i", "em"}:
                self.parts.append("*")
            elif tag in {"b", "strong"}:
                self.parts.append("**")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def text(self) -> str:
        # Whitespace between HTML paragraphs is layout, not another paragraph.
        text = re.sub(r"[ \t\r\f\v]+", " ", "".join(self.parts))
        text = re.sub(r" *\n *", "\n", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_feed(content: bytes, username: str) -> tuple[list[dict], int]:
    """Read film activity, skipping lists and rejecting a wrong or malformed feed."""

    validate_username(username)
    if len(content) > MAX_FEED_BYTES or re.search(br"<!\s*(?:DOCTYPE|ENTITY)", content, re.I):
        raise ValueError("Letterboxd returned an unsupported feed.")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as error:
        raise ValueError("Letterboxd did not return a readable RSS feed. Try again later.") from error
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise ValueError("Letterboxd did not return an RSS feed. Try again later.")
    profile = urlsplit(channel.findtext("link", ""))
    if profile.netloc.lower() not in {"letterboxd.com", "www.letterboxd.com"} or profile.path.strip("/").casefold() != username.casefold():
        raise ValueError("The feed does not match the requested Letterboxd profile.")
    entries, skipped = [], 0
    seen = set()
    for item in channel.findall("item"):
        movie = item.findtext(LB + "filmTitle", "").strip()
        if not movie:
            continue  # Lists and other non-film activity are not watched entries.
        try:
            link = entry_url(item.findtext("link", ""), username)
            watched = item.findtext(LB + "watchedDate", "").strip()
            watched = validate_watched_date(watched) if watched else None
            year = item.findtext(LB + "filmYear", "").strip()
            year = int(year) if year else None
            rating = item.findtext(LB + "memberRating", "").strip()
            rating = float(rating) if rating else None
            if rating is not None and (not 0 <= rating <= 5 or rating * 2 != int(rating * 2)):
                raise ValueError("Invalid rating")
            published = item.findtext("pubDate", "").strip()
            logged = parsedate_to_datetime(published).date().isoformat() if published else ""
            if len(movie) > 200 or "\n" in movie or "\r" in movie:
                raise ValueError("Invalid title")
            guid = item.findtext("guid", "")
            review = ""
            if guid.startswith("letterboxd-review-"):
                parser = ReviewText()
                parser.feed(item.findtext("description", ""))
                review = parser.text()
            if link in seen:
                continue
            seen.add(link)
            entries.append({"movie": movie, "year": year, "letterboxd_uri": link,
                            "rss_entry_url": link, "rss_guid": guid,
                            "watched_date": watched, "logged_date": logged,
                            "rating": rating, "rewatch": item.findtext(LB + "rewatch", "").casefold() == "yes",
                            "review": review, "tags": []})
        except (ValueError, TypeError, OverflowError):
            skipped += 1
    if skipped and not entries:
        raise ValueError("The feed's film entries could not be read. Your saved history was left unchanged.")
    return entries, skipped


class LetterboxdRedirects(HTTPRedirectHandler):
    """Follow only HTTPS redirects that stay on Letterboxd."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.netloc.lower() not in {"letterboxd.com", "www.letterboxd.com"}:
            raise ValueError("Letterboxd redirected the feed to an unexpected address.")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def fetch_feed(username: str) -> bytes:
    """Fetch one public feed directly from the laptop, without a proxy or login."""

    username = validate_username(username)
    request = Request(f"https://letterboxd.com/{username}/rss/", headers={
        "User-Agent": "AfterTheCredits/0.1 (personal diary sync)",
        "Accept": "application/rss+xml, application/xml",
    })
    try:
        with build_opener(LetterboxdRedirects()).open(request, timeout=20) as response:
            content = response.read(MAX_FEED_BYTES + 1)
    except HTTPError as error:
        if error.code == 404:
            raise ValueError("That Letterboxd profile was not found. Check the username.") from error
        raise ValueError("Letterboxd is temporarily refusing the feed request. Your saved history is safe; try again later.") from error
    except (URLError, TimeoutError, OSError) as error:
        raise ValueError("Could not reach Letterboxd. Your saved history is available offline; try syncing again later.") from error
    if len(content) > MAX_FEED_BYTES:
        raise ValueError("The Letterboxd feed was too large to read safely.")
    return content


def load_diary(root: Path) -> list[dict]:
    """Use the merged diary, or seed it from the existing untouched CSV export."""

    merged = root / "data/diary.json"
    if merged.exists():
        records = json.loads(merged.read_text(encoding="utf-8"))
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            raise ValueError("The saved diary is not a list of entries.")
        return records
    export = root / "data/raw/letterboxd_export/diary.csv"
    if not export.exists():
        return []
    records = []
    with export.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            records.append({"movie": (row.get("Name") or "").strip(),
                            "year": int(row["Year"]) if row.get("Year") else None,
                            "watched_date": (row.get("Watched Date") or "").strip() or None,
                            "logged_date": (row.get("Date") or "").strip(),
                            "letterboxd_uri": (row.get("Letterboxd URI") or "").strip(),
                            "rating": float(row["Rating"]) if row.get("Rating") else None,
                            "rewatch": (row.get("Rewatch") or "").strip().casefold() == "yes",
                            "tags": [tag.strip() for tag in (row.get("Tags") or "").split(",") if tag.strip()]})
    return records


def viewing_key(row: dict) -> tuple:
    """Match an export's short boxd.it link to a feed entry without fetching each link.

    Only dated entries may use this fallback. Ambiguous same-day entries are left
    alone, rather than guessing which viewing an old export row refers to.
    """

    return title_key(row.get("movie", "")), row.get("year"), row.get("watched_date")


def merge_entries(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], dict]:
    """Upsert by stable entry URL, with a unique dated export match as fallback."""

    result = [dict(row) for row in existing]
    counts = {"added": 0, "updated": 0, "ambiguous": 0}
    for entry in incoming:
        url = entry["rss_entry_url"]
        exact = [i for i, row in enumerate(result) if row.get("rss_entry_url", row.get("letterboxd_uri")) == url]
        candidates = exact
        if not candidates and entry.get("watched_date"):
            candidates = [i for i, row in enumerate(result) if not row.get("rss_entry_url") and viewing_key(row) == viewing_key(entry)]
        if len(candidates) > 1:
            counts["ambiguous"] += 1
            continue
        if candidates:
            index = candidates[0]
            # RSS does not supply tags. Preserve the export's richer metadata.
            updated = {**result[index], **entry, "tags": result[index].get("tags", [])}
            if updated != result[index]:
                result[index] = updated
                counts["updated"] += 1
        else:
            result.append(dict(entry))
            counts["added"] += 1
    return result, counts


class LetterboxdSync:
    """One background sync at a time, with cached history available while offline."""

    def __init__(self, root: Path, lock=None):
        self.root = root
        self.lock = lock or threading.RLock()
        self.state_path = root / "data/letterboxd_sync.json"
        self.saved = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}
        self.status = {**self.saved, "state": "idle", "message": "Ready to sync." if self.saved.get("username") else "Connect your Letterboxd profile."}

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.status)

    def start(self, username: str | None = None) -> dict:
        """Remember the username and return immediately while a worker fetches RSS."""

        with self.lock:
            if self.status["state"] == "running":
                return dict(self.status)
            username = validate_username(username or self.saved.get("username", ""))
            previous = self.saved.get("username")
            if previous and previous.casefold() != username.casefold():
                raise ValueError("This library is already connected to another profile. Use a separate project folder for another account.")
            self.saved["username"] = username
            write_json(self.state_path, self.saved)
            self.status = {**self.saved, "state": "running", "message": f"Checking @{username} for new film activity…"}
            threading.Thread(target=self._run, args=(username,), daemon=True).start()
            return dict(self.status)

    def _run(self, username: str) -> None:
        try:
            entries, skipped = parse_feed(fetch_feed(username), username)
            with self.lock:
                diary, diary_counts = merge_entries(load_diary(self.root), [row for row in entries if row.get("watched_date")])
                review_path = self.root / "data/reviews.json"
                reviews = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else []
                if not isinstance(reviews, list) or any(not isinstance(row, dict) for row in reviews):
                    raise ValueError("The saved reviews could not be read. Restore the review data before syncing.")
                reviews, review_counts = merge_entries(reviews, [row for row in entries if row["review"]])
                # Keep the pre-sync review examples as a local recovery copy.
                backup = self.root / "data/backups/before-letterboxd-sync-reviews.json"
                if review_path.exists() and not backup.exists():
                    write_json(backup, json.loads(review_path.read_text(encoding="utf-8")))
                write_json(self.root / "data/diary.json", diary)
                write_json(review_path, reviews)
                success = datetime.now(timezone.utc).isoformat()
                counts = {"viewings_added": diary_counts["added"], "viewings_updated": diary_counts["updated"],
                          "reviews_added": review_counts["added"], "reviews_updated": review_counts["updated"],
                          "skipped": skipped + diary_counts["ambiguous"] + review_counts["ambiguous"]}
                message = f"Synced @{username}: {counts['viewings_added']} new viewings, {counts['reviews_added']} new review examples."
                if counts["skipped"]:
                    message += f" {counts['skipped']} unreadable or ambiguous records were left unchanged."
                saved = {"username": username, "last_success": success, "counts": counts}
                write_json(self.state_path, saved)
                self.saved = saved
                self.status = {**saved, "state": "done", "message": message}
        except ValueError as error:
            with self.lock:
                self.status = {**self.saved, "state": "error", "message": str(error)}
        except Exception:
            with self.lock:
                self.status = {**self.saved, "state": "error", "message": "Sync could not finish. Your notes and drafts are unchanged. Check your connection and local history files, then try again."}

    def watched(self) -> list[dict]:
        """Expose dated viewings for the UI, without returning stored review bodies."""

        items = []
        for index, row in enumerate(load_diary(self.root)):
            if not row.get("watched_date") or not row.get("movie"):
                continue
            identity = row.get("rss_entry_url") or row.get("letterboxd_uri") or f"{viewing_key(row)}:{index}"
            items.append({"entry_id": hashlib.sha256(identity.encode()).hexdigest()[:24],
                          "title": row["movie"], "year": row.get("year"), "watched": row["watched_date"],
                          "rating": row.get("rating"), "rewatch": row.get("rewatch", False)})
        return sorted(items, key=lambda item: (item["watched"], item["title"]), reverse=True)
