<p align="center">
  <img src="https://raw.githubusercontent.com/PawiX25/twifork/main/assets/banner.png" width="640" alt="twifork">
</p>

<p align="center">
  一个无需 <b>API 密钥</b> 的 Python <b>Twitter / X</b> 抓取库。<br>
  它是 <a href="https://github.com/d60/twikit">d60/twikit</a> 的分支，集中修复了让上游在 2026 年无法正常使用的那些问题。
</p>

<p align="center">
  <img src="https://img.shields.io/pypi/v/twifork?color=blue&label=PyPI" alt="PyPI">
  <img src="https://img.shields.io/badge/python-3.8%2B-blue" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <img src="https://img.shields.io/github/stars/PawiX25/twifork?style=flat&color=yellow" alt="Stars">
</p>

<p align="center">
  [<a href="README.md">English</a>] · [<a href="README-ja.md">日本語</a>] · [中文]
</p>

> **可直接替换。** 包的导入名仍然是 `twikit`，所以 `from twikit import Client` 这类现有代码无需改动即可继续使用。

---

## 安装

```bash
pip install twifork
```

如果想用浏览器 TLS 伪装来绕过部分 `403`，可以加上可选依赖：

```bash
pip install "twifork[impersonate]"
```

也可以直接从 git 安装最新版本：

```bash
pip install git+https://github.com/PawiX25/twifork.git
```

## 关于这个分支

上游的 PyPI 版本（`twikit==2.3.3`）在 2026 年已经有多处无法使用。twifork 把这些都修好了，下面每一项都链接到对应的上游 Issue。

- **ClientTransaction / `Couldn't get KEY_BYTE indices`** — 按 X 新的 webpack 打包结构更新了 `ondemand.s.js` 的解析，GraphQL 请求重新可用。([#408](https://github.com/d60/twikit/issues/408)、[#409](https://github.com/d60/twikit/issues/409)、[#304](https://github.com/d60/twikit/issues/304))
- **`SearchTimeline` 和 `friends/list` 上时有时无、且会"卡住"的 `404`** — 计算 `x-client-transaction-id` 的动画密钥时漏掉了 X 的 `frame_time` 取整步骤，导致某些 `Client` 会话在重新创建之前，所有严格校验的请求都会返回 404。修复之后，这种随机 404 就消失了。([#357](https://github.com/d60/twikit/issues/357)、[#397](https://github.com/d60/twikit/issues/397))
- **可选字段缺失时的 `KeyError`**（`User.__init__` 与 `Client.request`）— 改用 `.get()` 安全读取。([#417](https://github.com/d60/twikit/issues/417))
- **用户 `name` / `screen_name` 为空**（常见于搜索结果）— X 把 `name`、`screen_name`、`created_at`、头像、位置等字段从 `legacy` 移到了新的子对象，现在新旧两种结构都能读取。
- **`get_tweet_by_id` 的 `KeyError: 'itemContent'`** — 同时兼容旧版和新版的末尾游标结构。([#332](https://github.com/d60/twikit/issues/332)、[#363](https://github.com/d60/twikit/issues/363))
- **没有可见推文的账户上，`get_user_tweets` 的 `KeyError: 'entries'` / `IndexError`** — 空的、没有游标的时间线会返回空结果，而不再崩溃。([#361](https://github.com/d60/twikit/issues/361)、[#216](https://github.com/d60/twikit/issues/216))
- **`get_trends` 被弃用、什么都不返回** — 基于 `GenericTimelineById` 重写，并新增了 `get_explore_page()`。([#389](https://github.com/d60/twikit/issues/389))
- **限流时的 `RecursionError`** — 429 的恢复逻辑不再递归。
- **`GuestClient.activate()` 的 404** — 游客客户端现在会带上 `User-Agent`，并安全地解析用户字段。([#402](https://github.com/d60/twikit/issues/402)、[#385](https://github.com/d60/twikit/issues/385))
- **`get_latest_friends` 的 404** — 在 v1.1 端点下线后，改走 GraphQL 的 `Following` 端点。([#397](https://github.com/d60/twikit/issues/397))
- **`'Client' object has no attribute '_ui_metrix'`** — 修复了验证码解锁流程。([#333](https://github.com/d60/twikit/issues/333))
- **`get_bookmark_folders().next()` 死循环** — 修正了写错的分页参数。([#334](https://github.com/d60/twikit/issues/334)、[#335](https://github.com/d60/twikit/issues/335))
- **`get_latest_timeline` / `get_list_tweets` 漏掉会话推文** — 现在会展开 home / list 的会话条目。([#336](https://github.com/d60/twikit/issues/336)、[#337](https://github.com/d60/twikit/issues/337)、[#340](https://github.com/d60/twikit/issues/340))
- 新增用于获取原图的 **`Media.source_url`**（[#376](https://github.com/d60/twikit/issues/376)），以及返回被引用推文 ID 的 **`Tweet.quoted_status_id`**（[#222](https://github.com/d60/twikit/issues/222)）。

至于由 X 一侧限制引起的问题（账户封禁、Cloudflare / IP 封锁、验证码、自动化限制），库本身无能为力，不在本项目范围内。

### 浏览器 TLS 伪装（可选）

X 的某些端点即使请求本身没问题，也会用 `403`（HTML 页面）拒绝 `httpx` 默认的 TLS 指纹。安装可选的 `curl_cffi` 后端并传入 `impersonate=`，请求就会以真实浏览器的 TLS 指纹发出，从而避开这类 403。

```python
client = Client('en-US', impersonate='chrome124')
```

## 快速上手

**创建客户端并登录。**

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

**发布带图片的推文。**

```python
media_ids = [
    await client.upload_media('media1.jpg'),
    await client.upload_media('media2.jpg'),
]
await client.create_tweet(text='Example Tweet', media_ids=media_ids)
```

**按关键词搜索最新推文。**

```python
tweets = await client.search_tweet('python', 'Latest')
for tweet in tweets:
    print(tweet.user.name, tweet.text, tweet.created_at)
```

**其他常用调用。**

```python
await client.get_user_tweets('123456', 'Tweets')   # 某个用户的推文
await client.send_dm('123456789', 'Hello')          # 发送私信
await client.get_trends('trending')                 # 热门趋势
```

更多示例（上游的示例同样适用）: https://github.com/d60/twikit/tree/main/examples

## 特性

- **无需 API 密钥** — 通过抓取网页端工作。
- **免费、开源**（MIT）。
- **`twikit` 的直接替换** — 导入名相同，代码无需改动。
- 推文、搜索、时间线、趋势、用户、私信、媒体、书签等等。

## 文档

完整 API 参考（上游文档，包的接口完全一致，可直接参考）: https://twikit.readthedocs.io/en/latest/twikit.html

## 社区

[![Discord](https://img.shields.io/badge/Discord-%235865F2.svg?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/nCrByrr8cX)

## 参与贡献

发现 bug 或者有修复方案？欢迎到 **[twifork issues](https://github.com/PawiX25/twifork/issues)** 提交 Issue 或 PR。

如果 twifork 帮你省了麻烦，欢迎点个 ⭐。

## 致谢

twifork 是 [@d60](https://github.com/d60) 的 **[d60/twikit](https://github.com/d60/twikit)** 的分支，原始实现的全部功劳归原作者所有。基于 **MIT 许可证**发布。

## 免责声明

twifork 是一个独立的非官方项目，**与 X Corp. 不存在任何隶属、认可或赞助关系。**"X" 和 "Twitter" 是 X Corp. 的商标。请在遵守相关条款和法律的前提下使用。
