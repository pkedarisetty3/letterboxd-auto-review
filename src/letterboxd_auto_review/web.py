"""A laptop-hosted review desk, using the same files and model workflow as the CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import socket
import threading
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from letterboxd_auto_review import draft_review as drafting
from letterboxd_auto_review.build_copy_preview import parse_markdown_review, parse_draft_title
from letterboxd_auto_review.feedback import learn_from_draft
from letterboxd_auto_review.letterboxd_sync import LetterboxdSync
from letterboxd_auto_review.movie_files import movie_file_stem, parse_watched_date, validate_watched_date

STATIC = Path(__file__).with_name("static")
NOTES_TEMPLATE = """Expectation / reason I watched:

Immediate reaction:

What I liked:

What did not work:

Scenes or moments to mention:

Performances:

Themes / bigger thoughts:

Ending thought:
"""


class Conflict(ValueError):
    """The user needs to reload or explicitly allow replacing an existing draft."""


def atomic_write(path: Path, text: str) -> None:
    """Replace a file only after all its new content has been written."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class ReviewDesk:
    """Read and edit the local movie library without exposing arbitrary files.

    A revision fingerprint prevents an old phone tab from replacing newer edits.
    Model generation runs in one background thread, keeping the library available.
    """

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.lock = threading.RLock()
        self.letterboxd = LetterboxdSync(self.root, self.lock)
        self.job = {"state": "idle"}
        self.pairing_code = f"{secrets.randbelow(1000000):06d}"
        self.session = secrets.token_urlsafe(32)
        self.pair_attempts: list[float] = []

    def path(self, folder: str, name: str) -> Path:
        """Constrain every requested movie file to its expected local folder."""

        path = self.root / folder / name
        if path.is_symlink() or path.resolve().parent != (self.root / folder).resolve():
            raise ValueError("Invalid movie file.")
        return path

    def paths(self, movie_id: str) -> tuple[Path, Path, Path]:
        if not isinstance(movie_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,220}", movie_id):
            raise ValueError("Invalid movie identifier.")
        return (self.path("notes", f"{movie_id}-notes.md"),
                self.path("drafts", f"{movie_id}-review.md"),
                self.path("drafts", f"{movie_id}-letterboxd-copy.html"))

    def read(self, movie_id: str) -> dict:
        """Return editable text, metadata, and a fingerprint for one viewing."""

        notes_path, draft_path, _ = self.paths(movie_id)
        notes = notes_path.read_text(encoding="utf-8") if notes_path.exists() else ""
        draft = draft_path.read_text(encoding="utf-8") if draft_path.exists() else ""
        if not notes and not draft:
            raise FileNotFoundError("Movie not found.")
        title = drafting.parse_movie_from_notes(notes) if notes else parse_draft_title(draft, draft_path)
        watched = parse_watched_date(notes) or (movie_id[:10] if re.match(r"\d{4}-\d{2}-\d{2}-", movie_id) else "")
        rating = drafting.parse_rating_from_notes(notes or draft.replace("**Rating:**", "Rating:"))
        # Metadata is edited through the form fields; the textarea holds thoughts.
        body = re.sub(r"(?m)^# [^\n]*\n?", "", notes, count=1)
        body = re.sub(r"(?im)^(?:Watched|Rating):[^\n]*\n?", "", body).strip()
        try:
            review = parse_markdown_review(draft) if draft else ""
        except ValueError:
            review = ""
        revision = hashlib.sha256((notes + "\0" + draft).encode()).hexdigest()
        return {"id": movie_id, "title": title, "watched": watched, "rating": rating,
                "notes": body, "review": review, "has_draft": bool(draft), "revision": revision,
                "copy_text": drafting.format_letterboxd_review_text(review),
                "copy_html": drafting.markdown_review_to_clipboard_html(review)}

    def library(self) -> list[dict]:
        """List viewings newest first, keeping large note/review bodies out of the list."""

        identifiers = set()
        for folder, suffix in [("notes", "-notes.md"), ("drafts", "-review.md")]:
            for path in (self.root / folder).glob(f"*{suffix}"):
                if not path.is_symlink():
                    identifiers.add(path.name.removesuffix(suffix))
        items = []
        for movie_id in identifiers:
            try:
                item = self.read(movie_id)
                items.append({key: item[key] for key in ("id", "title", "watched", "rating", "has_draft")})
            except (ValueError, OSError):
                continue
        return sorted(items, key=lambda item: (item["watched"], item["title"]), reverse=True)

    def check_edit(self, data: dict) -> dict:
        """Reject edits from stale tabs or while a generation is using these files."""

        if self.job["state"] == "running":
            raise Conflict("A draft is generating. Wait for it to finish before saving.")
        current = self.read(data.get("id", ""))
        if current["revision"] != data.get("revision"):
            raise Conflict("This movie changed in another tab or on disk. Reload it before saving.")
        return current

    def save_notes(self, data: dict) -> dict:
        """Save a new viewing or the thoughts for an existing one without overwrites."""

        with self.lock:
            if self.job["state"] == "running":
                raise Conflict("A draft is generating. Wait for it to finish before saving.")
            title = data.get("title", "").strip()
            if not title or len(title) > 200 or "\n" in title or "\r" in title:
                raise ValueError("Enter a movie title of up to 200 characters.")
            watched = validate_watched_date(data.get("watched", ""))
            rating = data.get("rating")
            if rating in (None, ""):
                rating = None
            else:
                rating = float(rating)
                if not 0 <= rating <= 5 or rating * 2 != int(rating * 2):
                    raise ValueError("Choose a rating between 0 and 5 in half-star steps.")
            movie_id = data.get("id") or movie_file_stem(title, watched)
            notes_path, draft_path, _ = self.paths(movie_id)
            if data.get("id"):
                current = self.check_edit(data)
                if title != current["title"] or watched != current["watched"]:
                    raise ValueError("For another title or watch date, create a new viewing.")
            elif notes_path.exists() or draft_path.exists():
                raise Conflict("This viewing already exists. Open it from your library.")
            body = data.get("notes", "").strip()
            if not isinstance(body, str):
                raise ValueError("Notes must be text.")
            text = f"# {title}\n\nWatched: {watched}\nRating: {rating if rating is not None else ''}\n\n{body}\n"
            atomic_write(notes_path, text)
            # Reflect rating edits in an existing draft without changing review text.
            if draft_path.exists():
                draft = draft_path.read_text(encoding="utf-8")
                draft = re.sub(r"(?m)^\*\*Rating:\*\*[^\n]*", f"**Rating:** {drafting.format_reference_rating(rating)}", draft)
                atomic_write(draft_path, draft)
                self.refresh_preview(movie_id)
            return self.read(movie_id)

    def refresh_preview(self, movie_id: str) -> None:
        """Keep the HTML paste companion synchronized with the saved draft."""

        item = self.read(movie_id)
        _, _, preview = self.paths(movie_id)
        atomic_write(preview, drafting.format_copy_preview_html(item["title"], item["rating"], item["review"]))

    def save_review(self, data: dict) -> dict:
        with self.lock:
            current = self.check_edit(data)
            if not current["has_draft"]:
                raise ValueError("Generate a draft first.")
            review = data.get("review", "").strip()
            if not review or re.search(r"(?m)^##\s", review):
                raise ValueError("Enter review text without Markdown section headings.")
            _, path, _ = self.paths(current["id"])
            draft = path.read_text(encoding="utf-8")
            # Only replace the editable section. Keep the generated baseline for feedback.
            pattern = r"(?ms)(^## Markdown Review\s*\n).*?(?=^##\s+|\Z)"
            updated, count = re.subn(pattern, lambda match: match[1] + "\n" + review + "\n\n", draft, count=1)
            if count != 1:
                raise ValueError("The draft has no editable review section.")
            atomic_write(path, updated)
            self.refresh_preview(current["id"])
            return self.read(current["id"])

    def feedback(self, data: dict) -> dict:
        with self.lock:
            current = self.check_edit(data)
            _, path, _ = self.paths(current["id"])
            learn_from_draft(path, self.root / "feedback/review_edits.jsonl")
            return {"message": "Your edits were saved as feedback for future drafts."}

    def generate(self, data: dict) -> dict:
        """Start generation only after the notes have been saved and validated."""

        with self.lock:
            current = self.check_edit(data)
            if current["has_draft"] and data.get("replace") is not True:
                raise Conflict("This movie already has a draft. Confirm replacement first.")
            if not drafting.extract_required_note_points(current["notes"]):
                raise ValueError("Add your thoughts before generating a review.")
            reviews = drafting.load_reviews(self.root / "data/reviews.json")
            self.job = {"state": "running", "id": current["id"], "message": "Drafting your review, then checking it against your notes…"}
            threading.Thread(target=self._generate, args=(current, reviews), daemon=True).start()
            return dict(self.job)

    def _generate(self, current: dict, reviews: list[dict]) -> None:
        """Call the existing model workflow; never return provider errors or secrets."""

        try:
            notes_path, draft_path, preview_path = self.paths(current["id"])
            notes = notes_path.read_text(encoding="utf-8")
            review, examples = drafting.generate_review(
                current["title"], current["rating"], notes, reviews,
                self.root / "feedback/review_edits.jsonl",
            )
            markdown = drafting.format_draft_markdown(
                current["title"], current["rating"], notes, review, review, examples,
            )
            with self.lock:
                if self.read(current["id"])["revision"] != current["revision"]:
                    raise Conflict("Files changed on disk while generating. Your edits were preserved; generate again when ready.")
                # Preserve the previous draft when the user explicitly regenerates.
                if draft_path.exists():
                    archive = self.root / "drafts/history" / f"{current['id']}-{time.time_ns()}-review.md"
                    atomic_write(archive, draft_path.read_text(encoding="utf-8"))
                atomic_write(draft_path, markdown + "\n")
                atomic_write(preview_path, drafting.format_copy_preview_html(current["title"], current["rating"], review))
                self.job = {"state": "done", "id": current["id"], "message": "Your draft is ready."}
        except Conflict as error:
            with self.lock:
                self.job = {"state": "error", "id": current["id"], "message": str(error)}
        except Exception:
            with self.lock:
                self.job = {"state": "error", "id": current["id"], "message": "Generation failed. Your saved notes and previous draft are safe. Check your API configuration, connection, or provider balance and try again."}


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], desk: ReviewDesk):
        self.desk = desk
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    """Serve only app assets and scoped movie operations, never the project tree."""

    def log_message(self, *args):
        # Request logs could contain pairing details; the launcher needs only URLs.
        pass

    @property
    def desk(self) -> ReviewDesk:
        return self.server.desk

    def local(self) -> bool:
        host = self.headers.get("Host", "").split(":")[0]
        return self.client_address[0] in ("127.0.0.1", "::1") and host in ("localhost", "127.0.0.1", "[::1]")

    def authorized(self) -> bool:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            session = cookie.get("review_session")
            return self.local() or bool(session and secrets.compare_digest(session.value, self.desk.session))
        except Exception:
            return False

    def send(self, status: int, payload, content_type="application/json; charset=utf-8", cookie=None):
        content = json.dumps(payload).encode() if content_type.startswith("application/json") else payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        route = urlsplit(self.path)
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if route.path in assets:
            name, content_type = assets[route.path]
            return self.send(200, (STATIC / name).read_bytes(), content_type)
        if not route.path.startswith("/api/"):
            return self.send(404, {"error": "Not found."})
        if not self.authorized():
            return self.send(401, {"error": "Enter the pairing code shown on your laptop."})
        try:
            with self.desk.lock:
                if route.path == "/api/library":
                    movies = self.desk.library()
                    watched = self.desk.letterboxd.watched()
                    # Link existing writing without creating or modifying any files.
                    local = {(item["title"].casefold(), item["watched"]): item["id"] for item in movies}
                    for item in watched:
                        item["local_id"] = local.get((item["title"].casefold(), item["watched"]))
                    result = {"movies": movies, "template": NOTES_TEMPLATE, "watched": watched,
                              "sync": self.desk.letterboxd.snapshot()}
                elif route.path == "/api/movie":
                    result = self.desk.read(parse_qs(route.query).get("id", [""])[0])
                elif route.path == "/api/sync-status":
                    result = self.desk.letterboxd.snapshot()
                elif route.path == "/api/job":
                    result = dict(self.desk.job)
                elif route.path == "/api/phone":
                    if not self.local():
                        return self.send(403, {"error": "Open phone setup on your laptop."})
                    result = {"code": self.desk.pairing_code, "urls": phone_urls(self.server.server_port),
                              "lan": self.server.server_address[0] == "0.0.0.0"}
                else:
                    return self.send(404, {"error": "Not found."})
            self.send(200, result)
        except (ValueError, FileNotFoundError) as error:
            self.send(400, {"error": str(error)})
        except Exception:
            self.send(500, {"error": "Could not read the movie files."})

    def do_POST(self):
        # Browsers on other websites cannot issue these custom-header requests.
        # Compare Origin as well to protect local access from cross-site writes.
        origin = self.headers.get("Origin")
        if self.headers.get("X-Review-Desk") != "1" or (origin and origin != f"http://{self.headers.get('Host')}"):
            return self.send(403, {"error": "Open the review desk directly to make changes."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_000_000:
                return self.send(413, {"error": "Request is too large or empty."})
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Expected a form object.")
            route = urlsplit(self.path).path
            if route == "/api/pair":
                with self.desk.lock:
                    now = time.monotonic()
                    self.desk.pair_attempts = [value for value in self.desk.pair_attempts if now - value < 60]
                    if len(self.desk.pair_attempts) >= 10:
                        return self.send(429, {"error": "Too many attempts. Try again in a minute."})
                    self.desk.pair_attempts.append(now)
                    if not secrets.compare_digest(str(data.get("code", "")), self.desk.pairing_code):
                        return self.send(401, {"error": "That code did not match. Check your laptop."})
                return self.send(200, {"message": "Phone connected."}, cookie=f"review_session={self.desk.session}; HttpOnly; SameSite=Strict; Path=/")
            if not self.authorized():
                return self.send(401, {"error": "Pair this browser with your laptop first."})
            actions = {"/api/save-notes": self.desk.save_notes, "/api/save-review": self.desk.save_review,
                       "/api/generate": self.desk.generate, "/api/feedback": self.desk.feedback,
                       "/api/sync": lambda data: self.desk.letterboxd.start(data.get("username"))}
            if route not in actions:
                return self.send(404, {"error": "Not found."})
            self.send(200, actions[route](data))
        except Conflict as error:
            self.send(409, {"error": str(error)})
        except (ValueError, TypeError, FileNotFoundError) as error:
            self.send(400, {"error": str(error)})
        except Exception:
            self.send(500, {"error": "Could not complete that action. Reload the movie and try again."})


def phone_urls(port: int) -> list[str]:
    """Find laptop network addresses without contacting an external service."""

    addresses = set()
    try:
        addresses.update(info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    try:
        # UDP connect chooses the local network interface without sending packets.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect(("192.0.2.1", 9))
            addresses.add(connection.getsockname()[0])
    except OSError:
        pass
    return [f"http://{address}:{port}" for address in sorted(addresses) if not address.startswith("127.")]


def main():
    parser = argparse.ArgumentParser(description="Open your laptop-hosted movie review desk.")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "0.0.0.0"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the laptop browser automatically.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    try:
        server = ReviewServer((args.host, args.port), ReviewDesk(root))
    except OSError as error:
        print(f"Could not start the review desk on port {args.port}: {error.strerror}. If another copy is running, close it and try again.")
        return
    # The feed fetch happens in a worker so startup and offline editing stay fast.
    if server.desk.letterboxd.saved.get("username"):
        server.desk.letterboxd.start()
    url = f"http://localhost:{args.port}"
    print(f"\nReview desk: {url}\nKeep this window open. Press Control-C to stop.\n", flush=True)
    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nReview desk stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
