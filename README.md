<p align="center">
  <img src="https://raw.githubusercontent.com/PawiX25/twifork/main/assets/banner.png" width="640" alt="twifork">
</p>

<p align="center">
  A <b>Twitter / X</b> API scraper for Python — <b>no API key required</b>.<br>
  A maintained fork of <a href="https://github.com/d60/twikit">d60/twikit</a>, fixed for the 2026 breakages that make the upstream release unusable.
</p>

<p align="center">
  <img src="https://img.shields.io/pypi/v/twifork?color=blue&label=PyPI" alt="PyPI">
  <img src="https://img.shields.io/badge/python-3.8%2B-blue" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <img src="https://img.shields.io/github/stars/PawiX25/twifork?style=flat&color=yellow" alt="Stars">
</p>

<p align="center">
  [English] · [<a href="README-ja.md">日本語</a>] · [<a href="README-zh.md">中文</a>]
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
- **`GuestClient.activate()` 404** — the guest client now sends a `User-Agent` header and parses user fields defensively. ([#402](https://github.com/d60/twikit/issues/402), [#385](https://github.com/d60/twikit/issues/385))
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

**Define a client and log in.**

```python
import asyncio
from twikit import Client

client = Client('en-US')

async def main():
    await client.login(
        auth_info_1='example_user',
        auth_info_2='email@example.com',
        password='password0000',
        cookies_file='cookies.json'
    )

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

## Documentation

Full API reference (upstream — the package surface is the same): https://twikit.readthedocs.io/en/latest/twikit.html

## Community

[![Discord](https://img.shields.io/badge/Discord-%235865F2.svg?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/nCrByrr8cX)

## Contributing

Found a bug or have a fix? Open an issue or PR on **[twifork issues](https://github.com/PawiX25/twifork/issues)**.

If twifork saved you a headache, consider leaving a ⭐.

## Credits

twifork is a fork of **[d60/twikit](https://github.com/d60/twikit)** by [@d60](https://github.com/d60) — all upstream credit goes to the original authors. Licensed under the **MIT License**.

## Disclaimer

twifork is an independent, unofficial project. It is **not affiliated with, endorsed by, or sponsored by X Corp.** "X" and "Twitter" are trademarks of X Corp. Use it in accordance with applicable terms and laws.
