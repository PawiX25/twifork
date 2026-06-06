<p align="center">
  <img src="https://raw.githubusercontent.com/PawiX25/twifork/main/assets/banner.png" width="640" alt="twifork">
</p>

<p align="center">
  <b>API キー不要</b>で使える、Python 用の <b>Twitter / X</b> スクレイピングライブラリ。<br>
  <a href="https://github.com/d60/twikit">d60/twikit</a> のフォークで、2026 年時点で本家が動かなくなった不具合をまとめて直しています。
</p>

<p align="center">
  <img src="https://img.shields.io/pypi/v/twifork?color=blue&label=PyPI" alt="PyPI">
  <img src="https://img.shields.io/badge/python-3.8%2B-blue" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <img src="https://img.shields.io/github/stars/PawiX25/twifork?style=flat&color=yellow" alt="Stars">
</p>

<p align="center">
  [<a href="README.md">English</a>] · [日本語] · [<a href="README-zh.md">中文</a>]
</p>

> **そのまま差し替え可能。** パッケージ名は `twikit` のままなので、`from twikit import Client` などの既存コードはそのまま動きます。

---

## インストール

```bash
pip install twifork
```

ブラウザの TLS を偽装して一部の `403` を回避したい場合は、オプションを付けてください。

```bash
pip install "twifork[impersonate]"
```

git から最新版を直接入れることもできます。

```bash
pip install git+https://github.com/PawiX25/twifork.git
```

## このフォークについて

本家の PyPI 版（`twikit==2.3.3`）は、2026 年現在いくつもの箇所が壊れています。twifork ではそれらを修正しました。各項目は対応する本家の Issue にリンクしています。

- **ClientTransaction / `Couldn't get KEY_BYTE indices`** — X の新しい webpack バンドルに合わせて `ondemand.s.js` の解析を更新。GraphQL リクエストが再び通るようになりました。([#408](https://github.com/d60/twikit/issues/408), [#409](https://github.com/d60/twikit/issues/409), [#304](https://github.com/d60/twikit/issues/304))
- **`SearchTimeline` や `friends/list` で散発的に出る、居座り型の `404`** — `x-client-transaction-id` のアニメーションキー計算で X 側の `frame_time` の丸め処理が抜けており、一部の `Client` では作り直すまで厳格なリクエストがすべて 404 になっていました。これを直したので、ランダムに出ていた 404 は解消されています。([#357](https://github.com/d60/twikit/issues/357), [#397](https://github.com/d60/twikit/issues/397))
- **任意フィールド欠落時の `KeyError`**（`User.__init__` と `Client.request`）— `.get()` で安全に読むようにしました。([#417](https://github.com/d60/twikit/issues/417))
- **ユーザーの `name` / `screen_name` が空になる**（検索結果などで発生）— X が `name`・`screen_name`・`created_at`・アイコン・場所などを `legacy` から新しいサブオブジェクトへ移したため、新旧どちらの形でも読めるようにしました。
- **`get_tweet_by_id` の `KeyError: 'itemContent'`** — 旧形式と新しい末尾カーソル形式の両方に対応。([#332](https://github.com/d60/twikit/issues/332), [#363](https://github.com/d60/twikit/issues/363))
- **ツイートが無いアカウントでの `get_user_tweets` の `KeyError: 'entries'` / `IndexError`** — 空・カーソル無しのタイムラインでも、落ちずに空の結果を返します。([#361](https://github.com/d60/twikit/issues/361), [#216](https://github.com/d60/twikit/issues/216))
- **`get_trends` が非推奨で何も返さない** — `GenericTimelineById` ベースに作り直し。あわせて `get_explore_page()` も追加しました。([#389](https://github.com/d60/twikit/issues/389))
- **レート制限時の `RecursionError`** — 429 のリカバリ処理が再帰しないようにしました。
- **`GuestClient.activate()` の 404** — ゲストクライアントが `User-Agent` を送るようにし、ユーザー情報の解析も安全にしました。([#402](https://github.com/d60/twikit/issues/402), [#385](https://github.com/d60/twikit/issues/385))
- **`get_latest_friends` の 404** — 廃止された v1.1 の代わりに、GraphQL の `Following` エンドポイントを使うようにしました。([#397](https://github.com/d60/twikit/issues/397))
- **`'Client' object has no attribute '_ui_metrix'`** — captcha 解除の処理を修正。([#333](https://github.com/d60/twikit/issues/333))
- **`get_bookmark_folders().next()` が無限ループ** — 壊れていたページネーション用パラメータを修正。([#334](https://github.com/d60/twikit/issues/334), [#335](https://github.com/d60/twikit/issues/335))
- **`get_latest_timeline` / `get_list_tweets` が会話ツイートを取りこぼす** — home / list の会話エントリも展開するようにしました。([#336](https://github.com/d60/twikit/issues/336), [#337](https://github.com/d60/twikit/issues/337), [#340](https://github.com/d60/twikit/issues/340))
- フル解像度の画像を取得する **`Media.source_url`**（[#376](https://github.com/d60/twikit/issues/376)）と、引用元ツイートの ID を返す **`Tweet.quoted_status_id`**（[#222](https://github.com/d60/twikit/issues/222)）を追加。

なお、X 側の制限（アカウント凍結、Cloudflare / IP ブロック、captcha、自動化の制約）が原因の問題は、ライブラリ側では直しようがないため対象外です。

### ブラウザ TLS の偽装（任意）

X の一部のエンドポイントは、リクエスト自体が正しくても、`httpx` 既定の TLS フィンガープリントを `403`（HTML）で弾いてきます。オプションの `curl_cffi` を入れて `impersonate=` を渡すと、実ブラウザの TLS フィンガープリントで通信し、この種の 403 を避けられます。

```python
client = Client('en-US', impersonate='chrome124')
```

## 使い方

**クライアントを用意してログイン。**

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

**画像付きでツイートを投稿。**

```python
media_ids = [
    await client.upload_media('media1.jpg'),
    await client.upload_media('media2.jpg'),
]
await client.create_tweet(text='Example Tweet', media_ids=media_ids)
```

**キーワードで最新ツイートを検索。**

```python
tweets = await client.search_tweet('python', 'Latest')
for tweet in tweets:
    print(tweet.user.name, tweet.text, tweet.created_at)
```

**その他、よく使う呼び出し。**

```python
await client.get_user_tweets('123456', 'Tweets')   # ユーザーのツイート
await client.send_dm('123456789', 'Hello')          # DM を送る
await client.get_trends('trending')                 # トレンド
```

さらに詳しい例（本家のものがそのまま使えます）: https://github.com/d60/twikit/tree/main/examples

## 特長

- **API キー不要** — Web 版をスクレイピングして動作します。
- **無料・オープンソース**（MIT）。
- **`twikit` の置き換え** — インポート名は同じなので、コードはそのままで構いません。
- ツイート、検索、タイムライン、トレンド、ユーザー、DM、メディア、ブックマークなど。

## ドキュメント

API リファレンス（本家のもの。パッケージの中身は同じなので、そのまま使えます）: https://twikit.readthedocs.io/en/latest/twikit.html

## コミュニティ

[![Discord](https://img.shields.io/badge/Discord-%235865F2.svg?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/nCrByrr8cX)

## コントリビュート

不具合を見つけたとき、修正があるときは、**[twifork の Issues](https://github.com/PawiX25/twifork/issues)** に Issue や PR をお願いします。

twifork が役に立ったら、⭐ を付けてもらえると励みになります。

## クレジット

twifork は [@d60](https://github.com/d60) 氏による **[d60/twikit](https://github.com/d60/twikit)** のフォークです。元になる実装の功績はすべて原作者に帰属します。**MIT ライセンス**で公開しています。

## 免責事項

twifork は独立した非公式プロジェクトであり、**X Corp. とは一切の提携・承認・スポンサー関係はありません。**「X」および「Twitter」は X Corp. の商標です。利用にあたっては、適用される規約および法令を守ってください。
