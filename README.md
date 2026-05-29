> [!NOTE]
> https://github.com/d60/twitter_login (under development)

<img src="https://i.imgur.com/iJe6rsZ.png"  width="500">



![Number of GitHub stars](https://img.shields.io/github/stars/d60/twikit)
![GitHub commit activity](https://img.shields.io/github/commit-activity/m/d60/twikit)
![Version](https://img.shields.io/pypi/v/twikit?label=PyPI)
[![Tweet](https://img.shields.io/twitter/url/http/shields.io.svg?style=social)](https://twitter.com/intent/tweet?text=Create%20your%20own%20Twitter%20bot%20for%20free%20with%20%22Twikit%22!%20%23python%20%23twitter%20%23twikit%20%23programming%20%23github%20%23bot&url=https%3A%2F%2Fgithub.com%2Fd60%2Ftwikit)
[![Discord](https://img.shields.io/badge/Discord-%235865F2.svg?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/nCrByrr8cX)
[![BuyMeACoffee](https://img.shields.io/badge/-buy_me_a%C2%A0coffee-gray?logo=buy-me-a-coffee)](https://www.buymeacoffee.com/d60py)

[[日本語](https://github.com/d60/twikit/blob/main/README-ja.md)]
[[中文](https://github.com/d60/twikit/blob/main/README-zh.md)]


# Twikit <img height="35"  src="https://i.imgur.com/9HSdIl4.png"  valign="bottom">

A Simple Twitter API Scraper

You can use functions such as posting or searching for tweets without an API key using this library.

- [Documentation (English)](https://twikit.readthedocs.io/en/latest/twikit.html)


🔵 [Discord](https://discord.gg/nCrByrr8cX)

> [!NOTE]
> Released twikit_grok an extension for using Grok AI with Twikit.  
> For more details, visit: https://github.com/d60/twikit_grok.


## About this fork

A fork of [d60/twikit](https://github.com/d60/twikit) with fixes for breakages
that make the upstream PyPI release (`twikit==2.3.3`) unusable as of 2026.
Install it with:

```
pip install git+https://github.com/PawiX25/twikit.git
```

Fixes included (resolving the corresponding upstream issues):

- **ClientTransaction / `Couldn't get KEY_BYTE indices`** — updated `ondemand.s.js`
  parsing for the new X webpack bundle, so GraphQL requests work again
  (#408, #409).
- **`KeyError` on missing optional fields** in `User.__init__` and
  `Client.request` — defensive `.get()` parsing (#417, #350, #425, #344).
- **`get_tweet_by_id` `KeyError: 'itemContent'`** — handles both the legacy and
  the new trailing-cursor shapes (#332, #363).
- **`search_tweet` 404** — refreshed `SearchTimeline` query id and features.
- **`get_trends` deprecated / returns nothing** — rewritten on top of
  `GenericTimelineById`; also adds `get_explore_page()` (#389).
- **`RecursionError` on rate-limit** — the 429 recovery path no longer recurses.

Issues that stem from X-side restrictions (account suspension, Cloudflare/IP
blocks, captcha, automation limits) are not fixable in the library and are out
of scope.

### Browser TLS impersonation (optional)

Some X endpoints reject the default `httpx` TLS fingerprint with a `403`
(HTML) response even when the request is valid. Installing the optional
`curl_cffi` backend and passing `impersonate=` routes requests through a
browser TLS fingerprint, which avoids those 403s:

```
pip install "twikit[impersonate] @ git+https://github.com/PawiX25/twikit.git"
```

```python
client = Client('en-US', impersonate='chrome124')
```


## Features

### No API Key Required

This library uses scraping and does not require an API key.

### Free

This library is free to use.


## Functionality

By using Twikit, you can access functionalities such as the following:

-  Create tweets

-  Search tweets

-  Retrieve trending topics

- etc...



## Installing

```bash

pip install twikit

```



## Quick Example

**Define a client and log in to the account.**

```python
import asyncio
from twikit import Client

USERNAME = 'example_user'
EMAIL = 'email@example.com'
PASSWORD = 'password0000'

# Initialize client
client = Client('en-US')

async def main():
    await client.login(
        auth_info_1=USERNAME,
        auth_info_2=EMAIL,
        password=PASSWORD,
        cookies_file='cookies.json'
    )

asyncio.run(main())
```

**Create a tweet with media attached.**

```python
# Upload media files and obtain media_ids
media_ids = [
    await client.upload_media('media1.jpg'),
    await client.upload_media('media2.jpg')
]

# Create a tweet with the provided text and attached media
await client.create_tweet(
    text='Example Tweet',
    media_ids=media_ids
)

```

**Search the latest tweets based on a keyword**
```python
tweets = await client.search_tweet('python', 'Latest')

for tweet in tweets:
    print(
        tweet.user.name,
        tweet.text,
        tweet.created_at
    )
```

**Retrieve user tweets**
```python
tweets = await client.get_user_tweets('123456', 'Tweets')

for tweet in tweets:
    print(tweet.text)
```

**Send a dm**
```python
await client.send_dm('123456789', 'Hello')
```

**Get trends**
```python
await client.get_trends('trending')
```

More Examples: [examples](https://github.com/d60/twikit/tree/main/examples) <br>

## Contributing

If you encounter any bugs or issues, please report them on [issues](https://github.com/d60/twikit/issues).


If you find this library useful, consider starring this repository⭐️
