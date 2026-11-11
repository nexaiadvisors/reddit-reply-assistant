import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import reply_assistant as ra


def listing(*children):
    return {"data": {"children": list(children)}}


def post(name, title, selftext="", **extra):
    return {"kind": "t3", "data": {"name": name, "title": title, "selftext": selftext,
                                   "permalink": f"/r/test/comments/{name}/", "author": "someone", **extra}}


class FakeReddit:
    """Records every request and answers from a table keyed by URL path."""

    def __init__(self, routes=None, status=200, headers=None):
        self.routes, self.status, self.headers, self.calls = routes or {}, status, headers or {}, []

    def __call__(self, method, url, headers, data=None):
        self.calls.append((method, url, headers))
        if url == ra.TOKEN_URL:
            return 200, {}, json.dumps({"access_token": "tok", "expires_in": 3600}).encode()
        path = url[len(ra.API):].split("?")[0]
        return self.status, self.headers, json.dumps(self.routes.get(path, listing())).encode()


def make_client(fake, **cfg_overrides):
    cfg = {**ra.DEFAULTS, "username": "tester", "subreddits": ["test"],
           "keywords": ["automation"], "min_seconds_between_requests": 0, **cfg_overrides}
    clock = {"t": 1_000_000.0}
    client = ra.Client(cfg, {}, {"refresh_token": "r"}, "id", "secret",
                       send=fake, now=lambda: clock["t"], sleep=lambda s: None)
    return client, clock


class ReadOnlyTests(unittest.TestCase):
    def test_write_endpoints_are_refused_before_any_request(self):
        fake = FakeReddit()
        client, _ = make_client(fake)
        for path in ("/api/comment", "/api/submit", "/api/vote", "/api/compose",
                     "/r/test/hot", "/message/sent", "/user/tester/comments"):
            with self.assertRaises(ValueError):
                client.get(path)
        self.assertEqual(fake.calls, [])

    def test_sweep_sends_only_get_requests_to_the_api(self):
        fake = FakeReddit({"/r/test/new": listing(post("t3_a", "Help with automation"))})
        client, _ = make_client(fake)
        ra.sweep(client)
        api_calls = [(m, u) for m, u, _ in fake.calls if u.startswith(ra.API)]
        self.assertTrue(api_calls)
        self.assertTrue(all(m == "GET" for m, _ in api_calls))
        posts = [(m, u) for m, u, _ in fake.calls if m != "GET"]
        self.assertEqual(posts, [("POST", ra.TOKEN_URL)])

    def test_inbox_is_read_without_marking_messages_read(self):
        fake = FakeReddit()
        client, _ = make_client(fake)
        ra.sweep(client)
        inbox_urls = [u for _, u, _ in fake.calls if "/message/inbox" in u]
        self.assertEqual(len(inbox_urls), 1)
        self.assertIn("mark=false", inbox_urls[0])

    def test_user_agent_names_the_app_and_account(self):
        fake = FakeReddit()
        client, _ = make_client(fake)
        client.get("/api/v1/me")
        self.assertTrue(all("reddit-reply-assistant" in h["User-Agent"] and "/u/tester" in h["User-Agent"]
                            for _, _, h in fake.calls))


class LimitTests(unittest.TestCase):
    def test_daily_budget_stops_further_requests(self):
        fake = FakeReddit()
        client, _ = make_client(fake, daily_request_budget=3)
        client.get("/api/v1/me")  # token refresh + this = 2 requests
        client.get("/api/v1/me")  # 3
        with self.assertRaises(ra.Stop):
            client.get("/api/v1/me")
        self.assertEqual(len(fake.calls), 3)

    def test_rate_limit_response_stops_the_run(self):
        fake = FakeReddit(status=429)
        client, _ = make_client(fake)
        with self.assertRaises(ra.Stop):
            ra.sweep(client)
        self.assertEqual(len([c for c in fake.calls if c[1].startswith(ra.API)]), 1)

    def test_low_rate_limit_header_stops_the_next_request(self):
        fake = FakeReddit(headers={"X-Ratelimit-Remaining": "2.0"})
        client, _ = make_client(fake)
        client.get("/api/v1/me")
        with self.assertRaises(ra.Stop):
            client.get("/api/v1/me")


class ContentTests(unittest.TestCase):
    def test_only_keyword_matches_are_kept_and_not_repeated(self):
        fake = FakeReddit({"/r/test/new": listing(
            post("t3_a", "Which automation tool for invoices?"),
            post("t3_b", "Best pizza in town"),
            post("t3_c", "automation question", author="[deleted]"))})
        client, _ = make_client(fake)
        self.assertEqual([i["id"] for i in ra.sweep(client)], ["t3_a"])
        fake.routes["/api/info"] = listing(post("t3_a", "Which automation tool for invoices?"))
        self.assertEqual(ra.sweep(client), [])

    def test_unread_comment_replies_are_listed(self):
        reply = {"kind": "t1", "data": {"name": "t1_r", "was_comment": True, "new": True,
                                        "body": "thanks, which tool?", "link_title": "A thread",
                                        "context": "/r/test/comments/x/_/r/?context=3",
                                        "subreddit": "test", "author": "someone"}}
        private = {"kind": "t4", "data": {"name": "t4_m", "new": True, "body": "hi"}}
        fake = FakeReddit({"/message/inbox": listing(reply, private)})
        client, _ = make_client(fake)
        found = ra.sweep(client)
        self.assertEqual([(i["id"], i["kind"]) for i in found], [("t1_r", "inbox_reply")])
        self.assertNotIn("author", found[0])

    def test_cached_text_is_deleted_after_retention_window(self):
        fake = FakeReddit({"/r/test/new": listing(post("t3_a", "automation help", "body"))})
        client, clock = make_client(fake)
        ra.sweep(client)
        self.assertIn("t3_a", client.state["items"])
        fake.routes = {}
        clock["t"] += 49 * 3600
        ra.sweep(client)
        self.assertEqual(client.state["items"], {})

    def test_content_removed_on_reddit_is_deleted_locally(self):
        fake = FakeReddit({"/r/test/new": listing(post("t3_a", "automation help", "body"))})
        client, _ = make_client(fake)
        ra.sweep(client)
        fake.routes = {"/api/info": listing(post("t3_a", "automation help", "[removed]"))}
        ra.sweep(client)
        self.assertEqual(client.state["items"], {})

    def test_long_text_is_cut_before_caching(self):
        fake = FakeReddit({"/r/test/new": listing(post("t3_a", "automation", "x" * 5000))})
        client, _ = make_client(fake, max_text_chars=100)
        self.assertEqual(len(ra.sweep(client)[0]["text"]), 100)


if __name__ == "__main__":
    unittest.main()
