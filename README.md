<p align="center">
  <img src="https://raw.githubusercontent.com/PawiX25/twifork/main/assets/banner.png" width="640" alt="twifork">
</p>

<p align="center">
  A <b>Twitter / X</b> API scraper for Python — <b>no API key required</b>.<br>
  A maintained fork of <a href="https://github.com/d60/twikit">d60/twikit</a>, fixed for the 2026 breakages that make the upstream release unusable.
</p>

<p align="center">
  <img src="https://img.shields.io/pypi/v/twifork?color=blue&label=PyPI" alt="PyPI">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <img src="https://img.shields.io/github/stars/PawiX25/twifork?style=flat&color=yellow" alt="Stars">
</p>

<p align="center">
  [English] · [<a href="https://github.com/PawiX25/twifork/blob/main/README-ja.md">日本語</a>] · [<a href="https://github.com/PawiX25/twifork/blob/main/README-zh.md">中文</a>]
</p>

> **Drop-in replacement** — the package still imports as `twikit`, so existing code (`from twikit import Client`) keeps working unchanged.

---

## Install

```bash
pip install twifork
```

With optional browser-TLS impersonation (gets past some `403` walls):

```bash
pip install "twifork[impersonate]"
```

Or grab the latest straight from git:

```bash
pip install git+https://github.com/PawiX25/twifork.git
```

## Why this fork?

The upstream PyPI release (`twikit==2.3.3`) is broken in several ways as of 2026. **twifork** fixes them — each item links to the upstream issue it resolves:

- **ClientTransaction / `Couldn't get KEY_BYTE indices`** — updated `ondemand.s.js` parsing for the new X webpack bundle, so GraphQL requests work again. ([#408](https://github.com/d60/twikit/issues/408), [#409](https://github.com/d60/twikit/issues/409), [#304](https://github.com/d60/twikit/issues/304))
- **Intermittent / sticky `404` on `SearchTimeline` and `friends/list`** — the `x-client-transaction-id` animation key was missing X's `frame_time` rounding step, so on some `Client` sessions every strict request 404'd until the client was recreated. Restored, so the semi-random 404s are gone. ([#357](https://github.com/d60/twikit/issues/357), [#397](https://github.com/d60/twikit/issues/397))
- **`KeyError` on missing optional fields** in `User.__init__` and `Client.request` — defensive `.get()` parsing. ([#417](https://github.com/d60/twikit/issues/417))
- **Empty user `name` / `screen_name`** (e.g. in search results) — X moved `name`, `screen_name`, `created_at`, avatar, location, and more out of `legacy` into new sub-objects; these are now read with a legacy fallback.
- **`get_tweet_by_id` `KeyError: 'itemContent'`** — handles both the legacy and the new trailing-cursor shapes. ([#332](https://github.com/d60/twikit/issues/332), [#363](https://github.com/d60/twikit/issues/363))
- **`KeyError: 'entries'` / `IndexError` on `get_user_tweets`** for accounts with no visible tweets — empty / cursor-less timelines return an empty result instead of crashing. ([#361](https://github.com/d60/twikit/issues/361), [#216](https://github.com/d60/twikit/issues/216))
- **`get_trends` deprecated / returns nothing** — rebuilt on top of `GenericTimelineById`; also adds `get_explore_page()`. ([#389](https://github.com/d60/twikit/issues/389))
- **`RecursionError` on rate-limit** — the 429 recovery path no longer recurses.
- **`GuestClient` does not work, and no fix here changes that.** The `User-Agent` header and the defensive user parsing are in place ([#402](https://github.com/d60/twikit/issues/402), [#385](https://github.com/d60/twikit/issues/385)), but `activate()` does not even get that far. Verified against live X: every call goes through the `x-client-transaction-id` handshake first, and a logged-out request only ever gets a ~34 KB page shell with no webpack manifest, so the handshake cannot complete - `activate()` itself raises `InvalidSession`. The `/1.1/guest/activate.json` endpoint is alive; reaching it is not. Guest access is closed on X's side; use cookies. ([#192](https://github.com/d60/twikit/issues/192))
- **`get_latest_friends` 404** — routed through the GraphQL `Following` endpoint after the v1.1 endpoint was retired. ([#397](https://github.com/d60/twikit/issues/397))
- **`'Client' object has no attribute '_ui_metrix'`** — fixed the captcha unlock path. ([#333](https://github.com/d60/twikit/issues/333))
- **`get_bookmark_folders().next()` infinite loop** — fixed malformed pagination variables. ([#334](https://github.com/d60/twikit/issues/334), [#335](https://github.com/d60/twikit/issues/335))
- **`get_latest_timeline` / `get_list_tweets` dropping conversation entries** — home- and list-conversation entries are now unpacked. ([#336](https://github.com/d60/twikit/issues/336), [#337](https://github.com/d60/twikit/issues/337), [#340](https://github.com/d60/twikit/issues/340))
- **`Media.source_url`** for the full-resolution image ([#376](https://github.com/d60/twikit/issues/376)), and **`Tweet.quoted_status_id`** for the quoted tweet id ([#222](https://github.com/d60/twikit/issues/222)).

Issues that stem from X-side restrictions (account suspension, Cloudflare/IP blocks, captcha, automation limits) aren't fixable in the library and are out of scope.

### Browser TLS impersonation (optional)

Some X endpoints reject the default `httpx` TLS fingerprint with a `403` (HTML) response even when the request is valid. Installing the optional `curl_cffi` backend and passing `impersonate=` routes requests through a real browser TLS fingerprint, which avoids those 403s:

```python
client = Client('en-US', impersonate='chrome124')
```

## Quick start

> **Log in with cookies, not with a password.**
> X has retired the onboarding flow that `Client.login()` drives — it answers
> `code 366, "flow name LoginFlow is currently not accessible"`. Verified against
> live x.com on 2026-07-29: `/i/flow/login` now redirects to
> `/i/jf/onboarding/web`, and the site posts to
> `/i/jfapi/onboarding/web/actions/begin_login`, which requires a ~5 KB
> `$castle_token` generated by obfuscated in-page JavaScript and offers
> passkey/WebAuthn as a first factor. None of that is reachable from a plain
> HTTP client, so **password login cannot be made to work** — no library fix
> will change it. Export your cookies from a browser session instead.

**Define a client and load cookies.**

```python
import asyncio
from twikit import Client

# impersonate= needs the extra: pip install twifork[impersonate]
# Without it the v1.1 endpoints answer 403 with a Cloudflare page.
client = Client('en-US', impersonate='chrome124')

async def main():
    # auth_token and ct0 are enough
    client.set_cookies({'auth_token': '...', 'ct0': '...'})
    # or: client.load_cookies('cookies.json')

    if not await client.is_logged_in():
        raise SystemExit('Cookies are stale - export them again.')

asyncio.run(main())
```

**Post a tweet with media attached.**

```python
media_ids = [
    await client.upload_media('media1.jpg'),
    await client.upload_media('media2.jpg'),
]
await client.create_tweet(text='Example Tweet', media_ids=media_ids)
```

**Search the latest tweets for a keyword.**

```python
tweets = await client.search_tweet('python', 'Latest')
for tweet in tweets:
    print(tweet.user.name, tweet.text, tweet.created_at)
```

**A few more common calls.**

```python
await client.get_user_tweets('123456', 'Tweets')   # a user's tweets
await client.send_dm('123456789', 'Hello')          # send a DM
await client.get_trends('trending')                 # trending topics
```

More examples (upstream, still apply): https://github.com/d60/twikit/tree/main/examples

## Features

- **No API key** — works by scraping the web client.
- **Free & open source** (MIT).
- **Drop-in `twikit` replacement** — same import, your code doesn't change.
- Tweets, search, timelines, trends, users, DMs, media, bookmarks, and more.
- **X Spaces** — read, search, create, publish, moderate and end audio
  Spaces, HLS stream urls, chat history and live chat. No extra packages for
  the core flow (`pip install twifork[spaces]` adds WebRTC voice + live chat
  WebSocket). See `examples/spaces.py`:

  ```python
  space = await client.spaces.get_space('1DXGydznBYWKM')
  stream = await client.spaces.get_stream(space.media_key)   # HLS url
  chat = await client.spaces.chat(space)                     # history
  created = await client.spaces.create_space(title='hi')     # go live
  await client.spaces.end_space(created['broadcast']['id'])
  ```

## Documentation

Full API reference (upstream — the package surface is the same): https://twikit.readthedocs.io/en/latest/twikit.html

Spaces guide: [docs/spaces.rst](docs/spaces.rst)

## Community

[![Discord](https://img.shields.io/badge/Discord-%235865F2.svg?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/nCrByrr8cX)

## Contributing

Found a bug or have a fix? Open an issue or PR on **[twifork issues](https://github.com/PawiX25/twifork/issues)**.

If twifork saved you a headache, consider leaving a ⭐.

## Credits

twifork is a fork of **[d60/twikit](https://github.com/d60/twikit)** by [@d60](https://github.com/d60) — all upstream credit goes to the original authors. Licensed under the **MIT License**.

## Disclaimer

twifork is an independent, unofficial project. It is **not affiliated with, endorsed by, or sponsored by X Corp.** "X" and "Twitter" are trademarks of X Corp. Use it in accordance with applicable terms and laws.
