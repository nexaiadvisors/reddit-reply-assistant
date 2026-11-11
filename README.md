# reddit-reply-assistant

A small, read-only helper for one Reddit account. It lists recent questions in a
few subreddits and unread comment replies in the account's own inbox, so the
account owner can answer them by hand.

It is a single Python file with no third-party dependencies, so it is quick to
audit: [`reply_assistant.py`](reply_assistant.py).

## What it does

Each run ("sweep"):

1. Reads the newest posts in the subreddits named in the config and keeps the
   ones whose title or text contains a configured keyword.
2. Reads the account's own inbox and keeps unread replies to its comments.
3. Prints the new items as JSON: subreddit, title, link and text.

The owner reads that list, sometimes with a local assistant that drafts a reply
for review, and then writes and posts any answer personally in the browser.

## What it never does

- Post, comment, edit, vote, save, follow, report or moderate.
- Send or reply to private messages.
- Mark inbox messages as read (`mark=false` is sent on every inbox read).
- Crawl, bulk-collect or archive Reddit content.
- Use Reddit content to train or fine-tune a model.

These are enforced in code, not by convention. The program can call exactly four
Reddit API endpoints, all `GET`, listed in `ALLOWED_GET`:

| Endpoint | Why |
|---|---|
| `GET /api/v1/me` | Confirm the signed-in account matches the config |
| `GET /r/{subreddit}/new` | Newest posts in a configured subreddit |
| `GET /message/inbox` | Unread replies to the account's own comments |
| `GET /api/info` | Re-check cached items so removed content is deleted |

Any other path raises an error before a request is sent. The only `POST` goes to
Reddit's OAuth token endpoint to obtain or refresh the access token.

## OAuth scopes

`identity read privatemessages`

Reddit has no read-only inbox scope, so reading `/message/inbox` requires
`privatemessages`. The program has no code path that sends a message.

Authorization uses the standard browser consent flow and a refresh token. The
account password is never entered into or stored by this program.

## Limits

| Limit | Default |
|---|---|
| API requests per UTC day | 100 (hard cap, counted across runs) |
| Concurrency | One request at a time |
| Minimum gap between requests | 2 seconds |
| Posts fetched per subreddit | 25 |
| On HTTP 429 or any non-200 response | Stop the run |
| When `X-Ratelimit-Remaining` drops below 5 | Stop the run |

Every request carries a descriptive User-Agent:
`macos:reddit-reply-assistant:v<version> (by /u/<username>)`.

## Data handling

- Items are cached locally in one file with owner-only permissions, so the same
  item is not listed twice.
- The cache holds the item id, subreddit, title, link and up to 2,000 characters
  of text. Author usernames are not stored.
- Cached text is deleted after 48 hours.
- At the start of every run, each cached item is re-checked with `/api/info`.
  Anything deleted or removed on Reddit is deleted locally.
- `purge` deletes the whole cache at once.
- If a drafting assistant is used, the text of a listed item may be sent to a
  hosted AI model to draft a reply for the owner to review. It is not used for
  training.

## Setup

Requires Python 3.11 or newer.

1. Register a `script` app at <https://www.reddit.com/prefs/apps> with redirect
   URI `http://localhost:8080`, after Reddit has approved API access for the
   account.
2. Copy `config.example.toml` to `config.toml` and set the username, subreddits
   and keywords.
3. Put the app credentials in the environment:

   ```bash
   export REDDIT_CLIENT_ID=...
   export REDDIT_CLIENT_SECRET=...
   ```

4. Authorize once, then run a sweep:

   ```bash
   python3 reply_assistant.py auth
   python3 reply_assistant.py sweep
   ```

Other commands: `status` shows today's request count and cache size, and `purge`
deletes cached content.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests use a fake Reddit and make no network calls. They check that write
endpoints are refused, that a sweep sends only `GET` requests to the API, that
the daily budget and rate-limit stops work, and that expired or removed content
is deleted.

## License

MIT. See [LICENSE](LICENSE).
