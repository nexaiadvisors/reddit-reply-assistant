#!/usr/bin/env python3
"""Read-only Reddit reply assistant for one account.

It lists recent questions in a few subreddits and unread comment replies in
the account's own inbox, so a person can answer them by hand. It never posts,
comments, votes, messages or moderates: the only requests it can send to the
Reddit API are the four GET endpoints in ALLOWED_GET.
"""
import argparse
import base64
import json
import os
import re
import secrets
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

VERSION = "0.1.0"
AUTH_URL = "https://www.reddit.com/api/v1/authorize"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"
REDIRECT_URI = "http://localhost:8080"
# Reddit has no read-only inbox scope: reading /message/inbox needs
# "privatemessages". This program has no code path that sends a message.
SCOPES = "identity read privatemessages"

# The complete list of Reddit API calls this program can make. All are GET.
ALLOWED_GET = (
    re.compile(r"^/api/v1/me$"),
    re.compile(r"^/r/[A-Za-z0-9_]{2,21}/new$"),
    re.compile(r"^/message/inbox$"),
    re.compile(r"^/api/info$"),
)

DEFAULTS = {
    "username": "",
    "subreddits": [],
    "keywords": [],
    "posts_per_subreddit": 25,
    "daily_request_budget": 100,
    "min_seconds_between_requests": 2,
    "retention_hours": 48,
    "max_text_chars": 2000,
}


class Stop(Exception):
    """Raised to end the run: budget used up, rate limit, or an API error."""


def load_config(path):
    cfg = dict(DEFAULTS)
    with open(path, "rb") as f:
        cfg.update(tomllib.load(f))
    if not cfg["username"] or not cfg["subreddits"]:
        raise Stop("config needs a username and at least one subreddit")
    return cfg


def state_dir():
    base = os.environ.get("REPLY_ASSISTANT_STATE_DIR")
    return Path(base) if base else Path.home() / ".local/state/reddit-reply-assistant"


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def http_send(method, url, headers, data=None):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class Client:
    def __init__(self, cfg, state, token, client_id, client_secret,
                 send=http_send, now=time.time, sleep=time.sleep):
        self.cfg, self.state, self.token = cfg, state, token
        self.client_id, self.client_secret = client_id, client_secret
        self.send, self.now, self.sleep = send, now, sleep
        self.user_agent = f"macos:reddit-reply-assistant:v{VERSION} (by /u/{cfg['username']})"
        self.rate_limit_low = False

    def _spend(self):
        """Count one request against the daily budget and pace it."""
        if self.rate_limit_low:
            raise Stop("Reddit rate limit nearly used up; stopping")
        day = time.strftime("%Y-%m-%d", time.gmtime(self.now()))
        if self.state.get("day") != day:
            self.state.update(day=day, requests=0)
        if self.state["requests"] >= self.cfg["daily_request_budget"]:
            raise Stop("daily request budget used up")
        wait = self.state.get("last_request", 0) + self.cfg["min_seconds_between_requests"] - self.now()
        if wait > 0:
            self.sleep(wait)
        self.state["requests"] += 1
        self.state["last_request"] = self.now()

    def token_request(self, form):
        """The only POST in this program. It goes to Reddit's token endpoint."""
        self._spend()
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        status, _, body = self.send(
            "POST", TOKEN_URL,
            {"Authorization": f"Basic {basic}", "User-Agent": self.user_agent,
             "Content-Type": "application/x-www-form-urlencoded"},
            urllib.parse.urlencode(form).encode())
        data = json.loads(body) if status == 200 else {}
        if "access_token" not in data:
            raise Stop(f"token request failed (HTTP {status})")
        self.token["access_token"] = data["access_token"]
        self.token["expires_at"] = self.now() + data.get("expires_in", 3600) - 60
        if data.get("refresh_token"):
            self.token["refresh_token"] = data["refresh_token"]

    def _access_token(self):
        if self.token.get("expires_at", 0) <= self.now():
            if not self.token.get("refresh_token"):
                raise Stop("not authorized yet; run the 'auth' command first")
            self.token_request({"grant_type": "refresh_token",
                                "refresh_token": self.token["refresh_token"]})
        return self.token["access_token"]

    def get(self, path, params=None):
        if not any(p.match(path) for p in ALLOWED_GET):
            raise ValueError(f"endpoint not on the read-only allowlist: {path}")
        access = self._access_token()
        self._spend()
        url = f"{API}{path}?{urllib.parse.urlencode({**(params or {}), 'raw_json': 1})}"
        status, headers, body = self.send(
            "GET", url, {"Authorization": f"Bearer {access}", "User-Agent": self.user_agent})
        if status != 200:
            raise Stop(f"Reddit returned HTTP {status} for {path}; stopping")
        remaining = {k.lower(): v for k, v in headers.items()}.get("x-ratelimit-remaining")
        if remaining is not None and float(remaining) < 5:
            self.rate_limit_low = True
        return json.loads(body)


def is_removed(d):
    return (d.get("author") == "[deleted]" or bool(d.get("removed_by_category"))
            or d.get("selftext") in ("[removed]", "[deleted]")
            or d.get("body") in ("[removed]", "[deleted]"))


def purge_expired(state, cfg, now):
    cutoff = now - cfg["retention_hours"] * 3600
    items = state.setdefault("items", {})
    for name in [n for n, it in items.items() if it["fetched_at"] < cutoff]:
        del items[name]


def drop_removed(client):
    """Re-check every cached item and delete any that Reddit no longer shows."""
    items = client.state.setdefault("items", {})
    names = list(items)
    for i in range(0, len(names), 100):
        chunk = names[i:i + 100]
        listing = client.get("/api/info", {"id": ",".join(chunk)})
        live = {c["data"]["name"] for c in listing["data"]["children"]
                if not is_removed(c["data"])}
        for name in chunk:
            if name not in live:
                del items[name]


def matches(text, keywords):
    text = text.lower()
    return not keywords or any(k.lower() in text for k in keywords)


def sweep(client):
    cfg, state = client.cfg, client.state
    purge_expired(state, cfg, client.now())
    drop_removed(client)
    items, found = state["items"], []

    def keep(name, kind, subreddit, title, permalink, text):
        if name in items:
            return
        items[name] = {"kind": kind, "subreddit": subreddit, "title": title,
                       "url": f"https://www.reddit.com{permalink}",
                       "text": text[:cfg["max_text_chars"]], "fetched_at": client.now()}
        found.append({"id": name, **items[name]})

    for sub in cfg["subreddits"]:
        listing = client.get(f"/r/{sub}/new", {"limit": cfg["posts_per_subreddit"]})
        for child in listing["data"]["children"]:
            d = child["data"]
            if is_removed(d) or not matches(f"{d['title']} {d.get('selftext', '')}", cfg["keywords"]):
                continue
            keep(d["name"], "post", sub, d["title"], d["permalink"], d.get("selftext", ""))

    # mark=false leaves the messages unread, so reading changes nothing on Reddit.
    inbox = client.get("/message/inbox", {"limit": 25, "mark": "false"})
    for child in inbox["data"]["children"]:
        d = child["data"]
        if child["kind"] == "t1" and d.get("was_comment") and d.get("new") and not is_removed(d):
            keep(d["name"], "inbox_reply", d.get("subreddit", ""), d.get("link_title", ""),
                 d.get("context", ""), d.get("body", ""))
    return found


def authorize(client):
    """One-time browser consent. Stores a refresh token; no password is used."""
    expected = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode({
        "client_id": client.client_id, "response_type": "code", "state": expected,
        "redirect_uri": REDIRECT_URI, "duration": "permanent", "scope": SCOPES})
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == expected and "code" in q:
                result["code"] = q["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab.")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 8080), Handler)
    print("Approve access in the browser window that opens.")
    webbrowser.open(f"{AUTH_URL}?{query}")
    server.handle_request()
    server.server_close()
    if "code" not in result:
        raise Stop("authorization was not completed")
    client.token_request({"grant_type": "authorization_code", "code": result["code"],
                          "redirect_uri": REDIRECT_URI})
    name = client.get("/api/v1/me")["name"]
    if name.lower() != client.cfg["username"].lower():
        client.token.clear()
        raise Stop(f"signed in as u/{name}, but the config says u/{client.cfg['username']}")
    print(f"Authorized as u/{name}.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["auth", "sweep", "purge", "status"])
    parser.add_argument("--config", default="config.toml")
    args = parser.parse_args(argv)

    state_path, token_path = state_dir() / "state.json", state_dir() / "token.json"
    state, token = load_json(state_path, {}), load_json(token_path, {})
    try:
        cfg = load_config(args.config)
        if args.command == "status":
            print(json.dumps({"day": state.get("day"), "requests_today": state.get("requests", 0),
                              "daily_request_budget": cfg["daily_request_budget"],
                              "cached_items": len(state.get("items", {}))}, indent=1))
            return 0
        if args.command == "purge":
            state["items"] = {}
            print("Deleted all cached Reddit content.")
            return 0
        client = Client(cfg, state, token, os.environ.get("REDDIT_CLIENT_ID", ""),
                        os.environ.get("REDDIT_CLIENT_SECRET", ""))
        if not client.client_id:
            raise Stop("set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET in the environment")
        if args.command == "auth":
            authorize(client)
        else:
            print(json.dumps(sweep(client), indent=1))
        return 0
    except Stop as e:
        print(f"stopped: {e}", file=sys.stderr)
        return 1
    finally:
        save_json(state_path, state)
        save_json(token_path, token)


if __name__ == "__main__":
    sys.exit(main())
