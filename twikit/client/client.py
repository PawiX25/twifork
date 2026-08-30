from __future__ import annotations

import asyncio
import inspect
import io
import json
import os
import secrets
import re

import warnings
from functools import partial
from typing import Any, AsyncGenerator, Awaitable, Callable, Literal
from urllib.parse import urlparse

import filetype
import pyotp
from httpx import AsyncClient, AsyncHTTPTransport, HTTPError, Request, Response
from httpx._utils import URLPattern

from .._captcha import Capsolver
from ..bookmark import BookmarkFolder
from ..community import Community, CommunityMember
from ..constants import COOKIE_DOMAINS, TOKEN, DOMAIN, TIMELINE_IDS
from ..errors import (
    AccountLocked,
    AccountSuspended,
    BadRequest,
    ClientTransactionError,
    LoginRetired,
    CouldNotTweet,
    Forbidden,
    InvalidMedia,
    NotFound,
    RequestTimeout,
    ServerError,
    TooManyRequests,
    TweetNotAvailable,
    TwitterException,
    Unauthorized,
    UserNotFound,
    UserUnavailable,
    raise_exceptions_from_response
)
from ..geo import Place, _places_from_response
from ..group import Group, GroupMessage
from ..list import List
from ..message import Conversation, Message
from ..notification import Notification
from ..streaming import Payload, StreamingSession, _payload_from_data
from ..trend import Location, PlaceTrend, PlaceTrends, Trend
from ..tweet import CommunityNote, Poll, ScheduledTweet, Tweet, tweet_from_data
from ..ui_metrics import solve_ui_metrics
from ..user import User
from ..utils import (
    Flow,
    Result,
    build_tweet_data,
    build_user_data,
    build_query,
    fatal_errors,
    limited,
    find_dict,
    cursor_at,
    find_entry_by_type,
    first_dict,
    last_cursor,
    httpx_transport_to_url,
    subobject
)
from ..x_client_transaction.utils import handle_x_migration
from ..x_client_transaction import ClientTransaction
from .gql import GQLClient
from .v11 import V11Client


def _check_media_ids(media_ids) -> None:
    """Rejects a bare string, which iterates into one id per character."""
    if isinstance(media_ids, str):
        # X answers "more than 4 mediaIds" for a 5-character string, which
        # points nowhere near the actual mistake.
        raise TypeError(
            '`media_ids` must be a list of ids, not a single string; '
            f'did you mean [{media_ids!r}]?'
        )


def _conversation_ids(content: dict) -> list[str] | None:
    """All tweet ids of a conversation module, when X ships them."""
    ids = subobject(
        subobject(content, 'metadata'), 'conversationMetadata'
    ).get('allTweetIds')
    return ids if isinstance(ids, list) else None


class _TransactionSession:
    """
    Minimal session the transaction handshake needs, routed through
    Client._send so impersonate= applies to it too.
    """
    def __init__(self, client: Client) -> None:
        self._client = client

    async def request(self, method: str = 'GET', url: str = '', **kwargs):
        return await self._client._send(method, url, **kwargs)


def _conversation_author_id(item: dict) -> str | None:
    """Author id of one entry inside a profile-conversation module."""
    result = (
        item.get('item', {})
        .get('itemContent', {})
        .get('tweet_results', {})
        .get('result', {})
    )
    # TweetWithVisibilityResults nests the real tweet one level down; without
    # this the author came back None, the user's own reply was filed as
    # somebody else's, and the Replies tab handed back the wrong tweet.
    if 'tweet' in result:
        result = result['tweet']
    user = result.get('core', {}).get('user_results', {}).get('result', {})
    return user.get('rest_id')


class Client:
    """
    A client for interacting with the Twitter API.
    Since this class is for asynchronous use,
    methods must be executed using await.

    Parameters
    ----------
    language : :class:`str` | None, default=None
        The language code to use in API requests.
    proxy : :class:`str` | None, default=None
        The proxy server URL to use for request
        (e.g., 'http://0.0.0.0:0000').
    captcha_solver : :class:`.Capsolver` | None, default=None
        See :class:`.Capsolver`.

    Examples
    --------
    >>> client = Client(language='en-US')

    >>> await client.login(
    ...     auth_info_1='example_user',
    ...     auth_info_2='email@example.com',
    ...     password='00000000'
    ... )
    """

    def __init__(
        self,
        language: str = 'en-US',
        proxy: str | None = None,
        captcha_solver: Capsolver | None = None,
        user_agent: str | None = None,
        impersonate: str | None = None,
        **kwargs
    ) -> None:
        if 'proxies' in kwargs:
            message = (
                "The 'proxies' argument is now deprecated. Use 'proxy' "
                "instead. https://github.com/encode/httpx/pull/2879"
            )
            warnings.warn(message)

        self.http = AsyncClient(proxy=proxy, **kwargs)
        self.language = language
        self.proxy = proxy
        # Optional curl_cffi transport: some X edges reject httpx's TLS fingerprint
        # with a 403 HTML page, so impersonate a browser when requested.
        self._impersonate = impersonate
        self._curl_session = None
        if impersonate is not None:
            from curl_cffi.requests import AsyncSession
            self._curl_session = AsyncSession(proxy=proxy, impersonate=impersonate)
        self.captcha_solver = captcha_solver
        if captcha_solver is not None:
            captcha_solver.client = self
        self.client_transaction = ClientTransaction()

        self._token = TOKEN
        self._user_id = None
        self._user_agent = user_agent or 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15'
        self._act_as = None

        self.gql = GQLClient(self)
        self.v11 = V11Client(self)
        # Headers of the most recent response. X reports the remaining budget
        # on every call, but until now it was only reachable from a 429, which
        # is exactly one request too late to be useful.
        self._response_headers: dict | None = None

    async def _send(self, method, url, **kwargs) -> Response:
        if self._curl_session is None:
            return await self.http.request(method, url, **kwargs)
        # Route through curl_cffi, sharing the httpx cookie jar both ways so that
        # ct0, auth and cookie persistence keep working unchanged.
        #
        # Everything the caller passed has to be forwarded. Cherry-picking
        # headers/params/data silently dropped `json=` - which every GraphQL
        # mutation uses - and `files=`, which carries the media chunks, so with
        # impersonate= enabled create_tweet posted an empty body and media
        # uploads failed with "media parameter is missing".
        forwarded = {
            key: kwargs[key] for key in (
                'headers', 'params', 'data', 'json', 'timeout'
            ) if key in kwargs
        }
        files = kwargs.get('files')
        if files:
            # curl_cffi has no `files=`; it wants a CurlMime. Translate the
            # httpx shape {name: (filename, fileobj_or_bytes, content_type)}
            # so media uploads work under impersonate too.
            from curl_cffi import CurlMime
            mime = CurlMime()
            for name, spec in files.items():
                if isinstance(spec, (tuple, list)):
                    filename, payload, *rest = spec
                    content_type = rest[0] if rest else None
                else:
                    filename, payload, content_type = name, spec, None
                if hasattr(payload, 'read'):
                    payload = payload.read()
                mime.addpart(
                    name=name,
                    filename=filename,
                    content_type=content_type,
                    data=payload
                )
            forwarded['multipart'] = mime
        # curl_cffi spells this allow_redirects and defaults it to True, httpx
        # calls it follow_redirects and defaults to False. Passing it only when
        # the caller did left the two transports disagreeing on every other
        # call, so turning impersonate= on silently started following redirects
        # - including the /account/access bounce this client checks for.
        forwarded['allow_redirects'] = kwargs.get('follow_redirects', False)
        # Handing curl_cffi the whole jar as a flat dict strips the domains and
        # sends every cookie to whatever host is being called - measured:
        # auth_token, ct0, kdt and twid all went to abs.twimg.com on the
        # handshake fetch, and to pbs.twimg.com on media downloads. Send only
        # what belongs to this host.
        sent = self._cookies_for(url)
        r = await self._curl_session.request(
            method, url,
            cookies=sent,
            **forwarded
        )
        # `Cookies.items()` looks up each name and raises CookieConflict as soon
        # as one exists for two hosts - and X sets twid on both api.x.com and
        # abs.twimg.com during the handshake, so the first v1.1 call after it
        # killed the client. Walk the jar, which carries the domain with it.
        #
        # `r.cookies` is the curl session jar, not this response's Set-Cookie,
        # so copying it wholesale re-imported a host-scoped duplicate of every
        # cookie we had just sent - which is what resurrected the previous
        # account's auth_token after a rotation. Take only what actually
        # changed.
        for cookie in r.cookies.jar:
            if sent.get(cookie.name) == cookie.value:
                continue
            self.http.cookies.set(
                cookie.name, cookie.value, domain=cookie.domain or ''
            )
        # Every request supplies its cookies explicitly, so curl's own jar is
        # not state we need - and leaving it to accumulate lets the two jars
        # drift and merge conflicting values into one Cookie header.
        self._curl_session.cookies.clear()
        # curl_cffi already decompressed the body; drop encoding/length headers
        # so httpx doesn't try to decode it a second time.
        resp_headers = {
            k: v for k, v in r.headers.items()
            if k.lower() not in ('content-encoding', 'content-length')
        }
        return Response(
            status_code=r.status_code,
            headers=resp_headers,
            content=r.content,
            request=Request(method, url),
        )

    @property
    def rate_limit_remaining(self) -> int | None:
        """
        Requests left in the current window, from the last response.
        """
        value = (self._response_headers or {}).get('x-rate-limit-remaining')
        return int(value) if value is not None else None

    @property
    def rate_limit_reset(self) -> int | None:
        """
        Unix time at which the current rate limit window resets.
        """
        value = (self._response_headers or {}).get('x-rate-limit-reset')
        return int(value) if value is not None else None

    async def request(
        self,
        method: str,
        url: str,
        auto_unlock: bool = True,
        raise_exception: bool = True,
        check_user_state: bool = True,
        **kwargs
    ) -> tuple[dict | Any, Response]:
        ':meta private:'
        headers = kwargs.pop('headers', {})

        if not self.client_transaction.is_inited():
            cookies_backup = self.get_cookies().copy()
            ct_headers = {
                'Accept-Language': f'{self.language},{self.language.split("-")[0]};q=0.9',
                'Cache-Control': 'no-cache',
                'Referer': f'https://{DOMAIN}',
                'User-Agent': self._user_agent
            }
            # The handshake used to go out on raw httpx, so impersonate= did
            # not cover the very first (and most filtered) request - users set
            # it to get past Cloudflare and still got 403 here.
            await self.client_transaction.init(
                _TransactionSession(self), ct_headers
            )
            # Internal restore: must not invalidate the handshake we just did.
            self._restore_cookies(cookies_backup)

        tid = self.client_transaction.generate_transaction_id(method=method, path=urlparse(url).path)
        headers['X-Client-Transaction-Id'] = tid

        cookies_backup = self.get_cookies().copy()
        response = await self._send(method, url, headers=headers, **kwargs)
        self._response_headers = dict(response.headers)
        self._remove_duplicate_ct0_cookie()

        try:
            response_data = response.json()
        except json.decoder.JSONDecodeError:
            response_data = response.text

        errors = response_data.get('errors') if isinstance(response_data, dict) else None
        # X returns `"errors": []` during partial outages, and occasionally a
        # non-dict entry, so the shape has to be checked before indexing it.
        if errors and isinstance(errors, list):
            first_error = errors[0] if isinstance(errors[0], dict) else {}
            error_code = first_error.get('code')
            error_message = first_error.get('message')
            # X reuses code 37 for plain authorization refusals - being told
            # you cannot use bookmark collections is not a suspension - so the
            # message has to agree before reporting one. Anything else falls
            # through to the status code, which says Forbidden and means it.
            if error_code in (37, 64) and 'suspend' in (error_message or '').lower():
                raise AccountSuspended(error_message)

            if error_code == 326:
                # Account unlocking
                if self.captcha_solver is None:
                    raise AccountLocked(
                        'Your account is locked. Visit '
                        f'https://{DOMAIN}/account/access to unlock it.'
                    )
                if auto_unlock:
                    await self.unlock()
                    self._restore_cookies(cookies_backup)
                    # The retry used to go out without headers, so a freshly
                    # unlocked account still got 401 - the whole unlock path
                    # was effectively dead. The id also has to be minted
                    # again: it encodes a timestamp and X enforces the header
                    # selectively, so replaying the failed attempt's id is
                    # exactly the stale-id case that answers 404.
                    headers['X-Client-Transaction-Id'] = (
                        self.client_transaction.generate_transaction_id(
                            method=method, path=urlparse(url).path
                        )
                    )
                    response = await self._send(
                        method, url, headers=headers, **kwargs
                    )
                    self._response_headers = dict(response.headers)
                    self._remove_duplicate_ct0_cookie()
                    try:
                        response_data = response.json()
                    except json.decoder.JSONDecodeError:
                        response_data = response.text

        status_code = response.status_code

        if status_code >= 400 and raise_exception:
            message = f'status: {status_code}, message: "{response.text}"'
            if status_code == 400:
                raise BadRequest(message, headers=response.headers)
            elif status_code == 401:
                raise Unauthorized(message, headers=response.headers)
            elif status_code == 403:
                raise Forbidden(message, headers=response.headers)
            elif status_code == 404:
                raise NotFound(message, headers=response.headers)
            elif status_code == 408:
                raise RequestTimeout(message, headers=response.headers)
            elif status_code == 429:
                # `check_user_state=False` when called recursively from
                # `_get_user_state()` itself — otherwise a 429 on the nested
                # user_state GET would re-enter this branch, call
                # `_get_user_state()` again, and loop until RecursionError.
                if check_user_state and await self._get_user_state() == 'suspended':
                    raise AccountSuspended(message, headers=response.headers)
                raise TooManyRequests(message, headers=response.headers)
            elif 500 <= status_code < 600:
                raise ServerError(message, headers=response.headers)
            else:
                raise TwitterException(message, headers=response.headers)

        if status_code == 200:
            return response_data, response

        return response_data, response

    async def get(self, url, **kwargs) -> tuple[dict | Any, Response]:
        ':meta private:'
        return await self.request('GET', url, **kwargs)

    async def post(self, url, **kwargs) -> tuple[dict | Any, Response]:
        ':meta private:'
        return await self.request('POST', url, **kwargs)

    def _remove_duplicate_ct0_cookie(self) -> None:
        # Rebuilding the jar from bare name/value pairs dropped every domain,
        # and a domain-less cookie is sent to *every* host - auth_token and
        # ct0 were going to abs.twimg.com on each handshake. Drop only the
        # surplus ct0 and leave the rest of the jar, attributes included.
        #
        # "Surplus" means host-scoped: X answers with a ct0 pinned to the
        # exact host it was talking to, and those are the duplicates worth
        # losing. Keeping whichever copy happened to come first instead threw
        # away the .twitter.com one this client writes deliberately, so after
        # a single response _ui_metrics - which stays on twitter.com - went
        # out with no CSRF cookie at all.
        for cookie in list(self.http.cookies.jar):
            if cookie.name != 'ct0' or cookie.domain in COOKIE_DOMAINS:
                continue
            self.http.cookies.jar.clear(
                cookie.domain, cookie.path, cookie.name
            )

    @property
    def proxy(self) -> str:
        ':meta private:'
        transport: AsyncHTTPTransport = self.http._mounts.get(URLPattern('all://'))
        if transport is None:
            return None
        if not hasattr(transport._pool, '_proxy_url'):
            return None
        return httpx_transport_to_url(transport)

    @proxy.setter
    def proxy(self, url: str) -> None:
        self.http._mounts = {URLPattern('all://'): AsyncHTTPTransport(proxy=url)}

    def _get_csrf_token(self) -> str:
        """
        Retrieves the Cross-Site Request Forgery (CSRF) token from the
        current session's cookies.

        Returns
        -------
        :class:`str`
            The CSRF token as a string.
        """
        # A raw `.get('ct0')` raises CookieConflict the moment two ct0 cookies
        # exist for different domains - the same failure get_cookies() had.
        # Walk the jar so this never dies on the one header every write
        # request needs.
        return self.get_cookies().get('ct0')

    @property
    def _base_headers(self) -> dict[str, str]:
        """
        Base headers for Twitter API requests.
        """
        headers = {
            'authorization': f'Bearer {self._token}',
            'content-type': 'application/json',
            'X-Twitter-Auth-Type': 'OAuth2Session',
            'X-Twitter-Active-User': 'yes',
            'Referer': f'https://{DOMAIN}/',
            'User-Agent': self._user_agent,
        }

        if self.language is not None:
            headers['Accept-Language'] = self.language
            headers['X-Twitter-Client-Language'] = self.language

        csrf_token = self._get_csrf_token()
        if csrf_token is not None:
            headers['X-Csrf-Token'] = csrf_token
        if self._act_as is not None:
            headers['X-Act-As-User-Id'] = self._act_as
        return headers

    async def _get_guest_token(self) -> str:
        response, _ = await self.v11.guest_activate()
        guest_token = response['guest_token']
        return guest_token

    async def _ui_metrics(self) -> str:
        response, _ = await self.get(f'https://twitter.com/i/js_inst?c_name=ui_metrics') # keep twitter.com here
        return response

    #: Explains why password login cannot work against X as it stands.
    _LOGIN_RETIRED = (
        'X no longer serves the LoginFlow onboarding task this method drives; '
        'it answers code 366, "flow name LoginFlow is currently not accessible". '
        'Measured against live x.com: /i/flow/login redirects to '
        '/i/jf/onboarding/web and the site now posts to '
        '/i/jfapi/onboarding/web/actions/begin_login, which requires a ~5 KB '
        '$castle_token produced by obfuscated in-page JavaScript, and offers '
        'passkey/WebAuthn as a first-factor path. None of that is reachable '
        'from a plain HTTP client. Authenticate with cookies instead - see '
        'Client.set_cookies and Client.load_cookies.'
    )

    async def login(
        self,
        *,
        auth_info_1: str,
        auth_info_2: str | None = None,
        password: str,
        totp_secret: str | None = None,
        cookies_file: str | None = None,
        enable_ui_metrics: bool = True,
        code_callback: Callable[[str], str | Awaitable[str]] | None = None
    ) -> dict:
        """
        Logs into the account using the specified login information.

        .. warning::
            This no longer works. X retired the onboarding flow this drives -
            it answers code 366, "flow name LoginFlow is currently not
            accessible". Use :func:`set_cookies` or :func:`load_cookies`
            instead; see the note on :attr:`_LOGIN_RETIRED` for what X
            replaced it with.

        `auth_info_1` and `password` are required parameters.
        `auth_info_2` is optional and can be omitted, but it is
        recommended to provide if available.
        The order in which you specify authentication information
        (auth_info_1 and auth_info_2) is flexible.

        Parameters
        ----------
        auth_info_1 : :class:`str`
            The first piece of authentication information,
            which can be a username, email address, or phone number.
        auth_info_2 : :class:`str`, default=None
            The second piece of authentication information,
            which is optional but recommended to provide.
            It can be a username, email address, or phone number.
        password : :class:`str`
            The password associated with the account.
        totp_secret : :class:`str`, default=None
            The TOTP (Time-Based One-Time Password) secret key used for
            two-factor authentication (2FA).
        code_callback : Callable[[:class:`str`], :class:`str`], default=None
            Called when the login flow asks for a code that cannot be derived
            locally - the confirmation code mailed by X, or a 2FA code when no
            `totp_secret` was given. It receives the prompt shown by X and
            must return the code. May be a coroutine function. Defaults to
            reading from stdin with :func:`input`, which blocks the event loop
            and is unusable in a service.
        cookies_file : :class:`str`, default=None
            The file path used for storing and loading cookies.
            If the specified file exists, cookies will be loaded from it, potentially bypassing the login process.
            After a successful login, cookies will be saved to this file for future use.
        enable_ui_metrics : :class:`bool`, default=True
            If set to True, obfuscated ui_metrics function will be executed using js2py,
            and the result will be sent to the API. Enabling this may reduce the risk of account suspension.

        Examples
        --------
        >>> await client.login(
        ...     auth_info_1='example_user',
        ...     auth_info_2='email@example.com',
        ...     password='00000000'
        ... )
        """
        self.http.cookies.clear()

        if cookies_file and os.path.exists(cookies_file):
            self.load_cookies(cookies_file)
            return

        try:
            guest_token = await self._get_guest_token()
        except ClientTransactionError as e:
            # The handshake needs a logged-in home page; while logging in there
            # are no cookies yet, so it reports "refresh your cookies", which is
            # nonsense advice in this context.
            raise LoginRetired(self._LOGIN_RETIRED) from e

        flow = Flow(self, guest_token)

        await flow.execute_task(params={'flow_name': 'login'}, data={
            'input_flow_data': {
                'flow_context': {
                    'debug_overrides': {},
                    'start_location': {
                        'location': 'splash_screen'
                    }
                }
            },
            'subtask_versions': {
                'action_list': 2,
                'alert_dialog': 1,
                'app_download_cta': 1,
                'check_logged_in_account': 1,
                'choice_selection': 3,
                'contacts_live_sync_permission_prompt': 0,
                'cta': 7,
                'email_verification': 2,
                'end_flow': 1,
                'enter_date': 1,
                'enter_email': 2,
                'enter_password': 5,
                'enter_phone': 2,
                'enter_recaptcha': 1,
                'enter_text': 5,
                'enter_username': 2,
                'generic_urt': 3,
                'in_app_notification': 1,
                'interest_picker': 3,
                'js_instrumentation': 1,
                'menu_dialog': 1,
                'notifications_permission_prompt': 2,
                'open_account': 2,
                'open_home_timeline': 1,
                'open_link': 1,
                'phone_verification': 4,
                'privacy_options': 1,
                'security_key': 3,
                'select_avatar': 4,
                'select_banner': 2,
                'settings_list': 7,
                'show_code': 1,
                'sign_up': 2,
                'sign_up_review': 4,
                'tweet_selection_urt': 1,
                'update_users': 1,
                'upload_media': 1,
                'user_recommendations_list': 4,
                'user_recommendations_urt': 1,
                'wait_spinner': 3,
                'web_modal': 1
            }
        })
        await flow.sso_init('apple')

        if enable_ui_metrics:
            ui_metrics_response = solve_ui_metrics(
                await self._ui_metrics()
            )
        else:
            ui_metrics_response = ''

        await flow.execute_task({
            'subtask_id': 'LoginJsInstrumentationSubtask',
            'js_instrumentation': {
                'response': ui_metrics_response,
                'link': 'next_link'
            }
        })
        await flow.execute_task({
            'subtask_id': 'LoginEnterUserIdentifierSSO',
            'settings_list': {
                'setting_responses': [
                    {
                        'key': 'user_identifier',
                        'response_data': {
                            'text_data': {'result': auth_info_1}
                        }
                    }
                ],
                'link': 'next_link'
            }
        })

        if flow.task_id == 'LoginEnterAlternateIdentifierSubtask':
            await flow.execute_task({
                'subtask_id': 'LoginEnterAlternateIdentifierSubtask',
                'enter_text': {
                    'text': auth_info_2,
                    'link': 'next_link'
                }
            })

        if flow.task_id == 'DenyLoginSubtask':
            raise TwitterException(flow.response['subtasks'][0]['cta']['secondary_text']['text'])

        await flow.execute_task({
            'subtask_id': 'LoginEnterPassword',
            'enter_password': {
                'password': password,
                'link': 'next_link'
            }
        })

        if flow.task_id == 'DenyLoginSubtask':
            raise TwitterException(flow.response['subtasks'][0]['cta']['secondary_text']['text'])

        # X hands out LoginAcid and LoginTwoFactorAuthChallenge in whatever
        # order it likes, and an account can be asked for both. Handling them
        # as a fixed if/if sequence returned early after LoginAcid, so a
        # 2FA step that arrived second was silently skipped and login came
        # back not logged in. Keep consuming challenges until none is left.
        for _ in range(4):
            if flow.task_id == 'LoginAcid':
                prompt = find_dict(
                    flow.response, 'secondary_text', find_one=True
                )
                code = await self._ask_login_code(
                    code_callback, prompt[0]['text'] if prompt else ''
                )
                await flow.execute_task({
                    'subtask_id': 'LoginAcid',
                    'enter_text': {'text': code, 'link': 'next_link'}
                })
            elif flow.task_id == 'LoginTwoFactorAuthChallenge':
                if totp_secret is None:
                    prompt = find_dict(
                        flow.response, 'secondary_text', find_one=True
                    )
                    totp_code = await self._ask_login_code(
                        code_callback, prompt[0]['text'] if prompt else ''
                    )
                else:
                    totp_code = pyotp.TOTP(totp_secret).now()

                await flow.execute_task({
                    'subtask_id': 'LoginTwoFactorAuthChallenge',
                    'enter_text': {'text': totp_code, 'link': 'next_link'}
                })
            else:
                break

            if flow.task_id == 'DenyLoginSubtask':
                raise TwitterException(
                    flow.response['subtasks'][0]['cta']['secondary_text']['text']
                )

        # The old code returned right after LoginAcid, so it never reached
        # this. Now that the challenge loop falls through, only answer the
        # duplication check when X is actually asking for it.
        if flow.task_id == 'AccountDuplicationCheck':
            await flow.execute_task({
                'subtask_id': 'AccountDuplicationCheck',
                'check_logged_in_account': {
                    'link': 'AccountDuplicationCheck_false'
                }
            })

        if cookies_file:
            self.save_cookies(cookies_file)

        if not flow.response['subtasks']:
            return

        user_id = first_dict(flow.response, 'id_str')
        if user_id is None:
            raise TwitterException(
                'Login did not complete - X returned no account. '
                f'Last subtask: {flow.task_id}'
            )
        self._user_id = user_id
        return flow.response

    @staticmethod
    async def _ask_login_code(callback, prompt: str) -> str:
        ''':meta private:'''
        if callback is None:
            print(prompt)
            return input('>>> ')
        result = callback(prompt)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def logout(self) -> Response:
        """
        Logs out of the currently logged-in account.
        """
        response, _ = await self.v11.account_logout()
        return response

    async def unlock(self) -> None:
        """
        Unlocks the account using the provided CAPTCHA solver.

        See Also
        --------
        .capsolver
        """
        if self.captcha_solver is None:
            raise ValueError('Captcha solver is not provided.')

        response, html = await self.captcha_solver.get_unlock_html()

        if html.delete_button:
            response, html = await self.captcha_solver.confirm_unlock(
                html.authenticity_token,
                html.assignment_token,
                ui_metrics=True
            )

        if html.start_button or html.finish_button:
            response, html = await self.captcha_solver.confirm_unlock(
                html.authenticity_token,
                html.assignment_token,
                ui_metrics=True
            )

        cookies_backup = self.get_cookies().copy()
        max_unlock_attempts = self.captcha_solver.max_attempts
        attempt = 0
        while attempt < max_unlock_attempts:
            attempt += 1

            if html.authenticity_token is None:
                response, html = await self.captcha_solver.get_unlock_html()

            result = self.captcha_solver.solve_funcaptcha(html.blob)
            if result['errorId'] == 1:
                continue

            self._restore_cookies(cookies_backup)
            response, html = await self.captcha_solver.confirm_unlock(
                html.authenticity_token,
                html.assignment_token,
                result['solution']['token'],
            )

            if html.finish_button:
                response, html = await self.captcha_solver.confirm_unlock(
                    html.authenticity_token,
                    html.assignment_token,
                    ui_metrics=True
                )
            # `next_request` is only ever populated by httpx's own redirect
            # machinery, and the Response synthesised for the curl_cffi
            # transport never goes through it - so with impersonate= set this
            # was unconditionally None and the loop could only ever run out of
            # attempts. Read the redirect target off the header instead, which
            # both transports carry.
            location = response.headers.get('location', '')
            finished = urlparse(location).path in ('/', '/home')
            if finished:
                return
        raise TwitterException('Could not unlock the account.')

    def refresh_transaction(self) -> None:
        """
        Forces the X-Client-Transaction-Id handshake to run again.

        The keys behind that header come from a webpack bundle X rotates every
        few days, and a client holds them for its whole life - so a
        long-running process eventually signs requests with stale keys and X
        answers sporadic 404s. Calling this is the cheap fix; rebuilding the
        client also works but throws the cookie jar away with it.

        Examples
        --------
        >>> client.refresh_transaction()
        """
        self.client_transaction.reset()

    def get_cookies(self) -> dict:
        """
        Get the cookies.
        You can skip the login procedure by loading the saved cookies
        using the :func:`set_cookies` method.

        Examples
        --------
        >>> client.get_cookies()

        See Also
        --------
        .set_cookies
        .load_cookies
        .save_cookies
        """
        # dict(jar) raises CookieConflict as soon as X sets the same name for
        # two domains - __cf_bm does exactly that - and this is called on
        # every transaction-id handshake, so the whole client would die on a
        # duplicate. Walk the jar instead, last value wins.
        cookies = {}
        for cookie in self.http.cookies.jar:
            cookies[cookie.name] = cookie.value
        return cookies

    def save_cookies(self, path: str) -> None:
        """
        Save cookies to file in json format.
        You can skip the login procedure by loading the saved cookies
        using the :func:`load_cookies` method.

        Parameters
        ----------
        path : :class:`str`
            The path to the file where the cookie will be stored.

        Examples
        --------
        >>> client.save_cookies('cookies.json')

        See Also
        --------
        .load_cookies
        .get_cookies
        .set_cookies
        """
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.get_cookies(), f)

    def _cookies_for(self, url: str) -> dict:
        """Cookies from the jar that belong to this URL's host."""
        host = urlparse(url).hostname or ''
        cookies = {}
        for cookie in self.http.cookies.jar:
            domain = cookie.domain or ''
            if not domain:
                cookies[cookie.name] = cookie.value
            elif domain.startswith('.'):
                # Domain cookie: this host and everything under it.
                base = domain[1:]
                if host == base or host.endswith('.' + base):
                    cookies[cookie.name] = cookie.value
            elif host == domain:
                # Host-only cookie (RFC 6265): exactly this host, not its
                # subdomains. Suffix-matching these sent a cookie X scoped to
                # x.com out to every *.x.com host as well.
                cookies[cookie.name] = cookie.value
        return cookies

    def _set_cookie(self, name: str, value: str) -> None:
        ''':meta private:'''
        # X answers with Set-Cookie scoped to the exact host, so after a few
        # calls the jar holds auth_token under x.com, api.x.com, abs.twimg.com
        # *and* .x.com. Writing only the dotted domain left those host copies
        # holding the previous account, httpx preferred the more specific one,
        # and rotating cookies produced 403 code 353 - the old auth_token
        # travelling with the new ct0. Drop every copy first.
        for cookie in list(self.http.cookies.jar):
            if cookie.name == name:
                self.http.cookies.jar.clear(
                    cookie.domain, cookie.path, cookie.name
                )
        # curl_cffi keeps its own jar, and anything left there is sent again on
        # the next call - so clearing only the httpx side let the previous
        # account's cookies come back.
        if self._curl_session is not None:
            for cookie in list(self._curl_session.cookies.jar):
                if cookie.name == name:
                    self._curl_session.cookies.jar.clear(
                        cookie.domain, cookie.path, cookie.name
                    )
        for domain in COOKIE_DOMAINS:
            self.http.cookies.set(name, value, domain=domain)

    def _restore_cookies(self, cookies: dict) -> None:
        ''':meta private:'''
        self.http.cookies.clear()
        for key, value in dict(cookies).items():
            self._set_cookie(key, value)

    def set_cookies(
        self, cookies: dict | list, clear_cookies: bool = False
    ) -> None:
        """
        Sets cookies.
        You can skip the login procedure by loading a saved cookies.

        Parameters
        ----------
        cookies : :class:`dict` | :class:`list`
            The cookies to be set as key value pair. A list of cookie objects
            as exported by a browser / Playwright / the "EditThisCookie"
            extension (each item a dict with ``name`` and ``value``) is also
            accepted, so you can log in once in a real browser and reuse the
            exported jar directly.

        Examples
        --------
        >>> with open('cookies.json', 'r', encoding='utf-8') as f:
        ...     client.set_cookies(json.load(f))

        See Also
        --------
        .get_cookies
        .load_cookies
        .save_cookies
        """
        # A browser / Playwright export is a list of cookie objects, not the
        # flat name->value dict this method was written for. Passing that list
        # straight to dict() below raises "cannot convert dictionary update
        # sequence element", so normalise it here - login once in a browser,
        # export the jar, reuse it as-is.
        if isinstance(cookies, list):
            cookies = {
                c['name']: c['value']
                for c in cookies
                if isinstance(c, dict) and 'name' in c and 'value' in c
            }
        if clear_cookies:
            self.http.cookies.clear()
        # Updating from a plain dict produces cookies with no domain, and
        # httpx sends those to *every* host - the handshake alone would ship
        # auth_token and ct0 to abs.twimg.com. Pin them to X.
        for key, value in dict(cookies).items():
            self._set_cookie(key, value)
        # Without a ct0, the first write request answers 403 code 353 ("this
        # request requires a matching csrf cookie and header") - X only
        # issues one via Set-Cookie *after* that failure. Measured against
        # live X: a real ct0 is 160 hex chars, but a self-generated one only
        # clears the check at exactly 32 - longer values that still match
        # between cookie and header (64, 128, 160) were rejected, so this is
        # not a pure double-submit check, X validates the shape too.
        #
        # Reading the jar with `.get()` raises CookieConflict once X has set
        # ct0 for more than one host, which it routinely does - the same trap
        # get_cookies() and _get_csrf_token() were rewritten to avoid.
        if 'ct0' not in cookies and not self.get_cookies().get('ct0'):
            self._set_cookie('ct0', secrets.token_hex(16))
        # user_id() memoises the account, so rotating to another set of cookies
        # kept answering with the previous account - every call that resolves
        # "me" silently addressed the wrong user. The transaction keys are tied
        # to the session that fetched them, so they have to go as well.
        self._user_id = None
        self.client_transaction.reset()

    def load_cookies(self, path: str) -> None:
        """
        Loads cookies from a file.
        You can skip the login procedure by loading a saved cookies.

        Parameters
        ----------
        path : :class:`str`
            Path to the file where the cookie is stored.

        Examples
        --------
        >>> client.load_cookies('cookies.json')

        See Also
        --------
        .get_cookies
        .save_cookies
        .set_cookies
        """
        with open(path, 'r', encoding='utf-8') as f:
            self.set_cookies(json.load(f))

    def set_delegate_account(self, user_id: str | None) -> None:
        """
        Sets the account to act as.

        Parameters
        ----------
        user_id : :class:`str` | None
            The user ID of the account to act as.
            Set to None to clear the delegated account.

        Note
        ----
        X only honours delegation on part of its internal API. Endpoints that
        refuse it answer 403 with code 90, ``Contributor access is not
        permitted on this endpoint`` - ``client.user()`` is one of them,
        because it goes through v1.1 account settings. That is X's
        restriction, not a missing header here.
        """
        self._act_as = user_id

    async def user_id(self) -> str:
        """
        Retrieves the user ID associated with the authenticated account.
        """
        if self._user_id is not None:
            return self._user_id
        response, _ = await self.v11.settings()
        screen_name = response['screen_name']
        self._user_id = (await self.get_user_by_screen_name(screen_name)).id
        return self._user_id

    async def user(self) -> User:
        """
        Retrieve detailed information about the authenticated user.
        """
        return await self.get_user_by_id(await self.user_id())

    async def is_logged_in(self) -> bool:
        """
        Checks whether the current cookies still authenticate an account.

        Cookies loaded from a file go stale silently - every later call then
        fails with a different error depending on which endpoint was hit
        first, which is why this is worth asking directly.

        Returns
        -------
        :class:`bool`
            True if the session is usable, False if it has expired or the
            account is no longer accessible.

        Examples
        --------
        >>> client.load_cookies('cookies.json')
        >>> if not await client.is_logged_in():
        ...     await client.login(...)

        Note
        ----
        This only reports whether X still accepts the session. A locked or
        suspended account answers False as well.
        """
        try:
            response, _ = await self.v11.settings()
        except (Unauthorized, Forbidden, AccountLocked, AccountSuspended,
                ClientTransactionError):
            return False
        except HTTPError:
            raise
        # A 200 that is not JSON (a Cloudflare interstitial, most often) comes
        # back as a bare string, and calling .get on it crashed instead of
        # reporting "not logged in" the way every other non-2xx case does.
        if not isinstance(response, dict):
            return False
        return bool(response.get('screen_name'))

    async def search_tweet(
        self,
        query: str,
        product: Literal['Top', 'Latest', 'Media'],
        count: int = 20,
        cursor: str | None = None
    ) -> Result[Tweet]:
        """
        Searches for tweets based on the specified query and
        product type.

        Parameters
        ----------
        query : :class:`str`
            The search query.
        product : {'Top', 'Latest', 'Media'}
            The type of tweets to retrieve.
        count : :class:`int`, default=20
            The number of tweets to retrieve, between 1 and 20.
        cursor : :class:`str`, default=20
            Token to retrieve more tweets.

        Returns
        -------
        Result[:class:`Tweet`]
            An instance of the `Result` class containing the
            search results.

        Examples
        --------
        >>> tweets = await client.search_tweet('query', 'Top')
        >>> for tweet in tweets:
        ...    print(tweet)
        <Tweet id="...">
        <Tweet id="...">
        ...
        ...

        >>> more_tweets = await tweets.next()  # Retrieve more tweets
        >>> for tweet in more_tweets:
        ...     print(tweet)
        <Tweet id="...">
        <Tweet id="...">
        ...
        ...

        >>> # Retrieve previous tweets
        >>> previous_tweets = await tweets.previous()
        """
        product = product.capitalize()

        response, _ = await self.gql.search_timeline(query, product, count, cursor)
        instructions = find_dict(response, 'instructions', find_one=True)
        if not instructions:
            return Result([])
        instructions = instructions[0]

        if product == 'Media' and cursor is not None:
            items = first_dict(instructions, 'moduleItems', [])
        else:
            items_ = find_dict(instructions, 'entries', find_one=True)
            if items_:
                items = items_[0]
            else:
                items = []
            if product == 'Media':
                if items and 'items' in (items[0].get('content') or {}):
                    items = items[0]['content']['items']
                else:
                    items = []

        next_cursor = None
        previous_cursor = None

        results = []
        for item in items:
            if item['entryId'].startswith('cursor-bottom'):
                next_cursor = item['content']['value']
            if item['entryId'].startswith('cursor-top'):
                previous_cursor = item['content']['value']
            if not item['entryId'].startswith(('tweet', 'search-grid')):
                continue

            try:
                tweet = tweet_from_data(self, item)
            except KeyError:
                tweet = None
                
            if tweet is not None:
                results.append(tweet)

        if next_cursor is None:
            if product == 'Media':
                entries = first_dict(instructions, 'entries', [])
                next_cursor = last_cursor(entries)
                previous_cursor = cursor_at(entries, -2)
            else:
                # An instruction without an `entry` key (TerminateTimeline) or
                # a list shorter than two used to raise KeyError/IndexError
                # here and take down the whole search, not just pagination.
                def _entry_cursor(index):
                    try:
                        entry = instructions[index].get('entry') or {}
                    except IndexError:
                        return None
                    return (entry.get('content') or {}).get('value')

                next_cursor = _entry_cursor(-1)
                previous_cursor = _entry_cursor(-2)

        # X treats `count` as a hint on this endpoint too; trim client-side
        # and hand the surplus back through next() instead of dropping it.
        results, overflow = limited(results, count)

        return Result(
            results,
            partial(self.search_tweet, query, product, count, next_cursor) if next_cursor else None,
            next_cursor,
            partial(self.search_tweet, query, product, count, previous_cursor) if previous_cursor else None,
            previous_cursor,
            overflow=overflow,
            page_size=count
        )

    async def search_user(
        self,
        query: str,
        count: int = 20,
        cursor: str | None = None
    ) -> Result[User]:
        """
        Searches for users based on the provided query.

        Parameters
        ----------
        query : :class:`str`
            The search query for finding users.
        count : :class:`int`, default=20
            The number of users to retrieve in each request.
        cursor : :class:`str`, default=None
            Token to retrieve more users.

        Returns
        -------
        Result[:class:`User`]
            An instance of the `Result` class containing the
            search results.

        Examples
        --------
        >>> result = await client.search_user('query')
        >>> for user in result:
        ...     print(user)
        <User id="...">
        <User id="...">
        ...
        ...

        >>> more_results = await result.next()  # Retrieve more search results
        >>> for user in more_results:
        ...     print(user)
        <User id="...">
        <User id="...">
        ...
        ...
        """
        response, _ = await self.gql.search_timeline(query, 'People', count, cursor)
        items = first_dict(response, 'entries', [])
        next_cursor = last_cursor(items)

        results = []
        for item in items:
            if 'itemContent' not in item['content']:
                continue
            user_info = first_dict(item, 'result')
            if user_info is None:
                # An entry X could not resolve (deleted or restricted user)
                # arrives without `result`; skip it instead of dying.
                continue
            results.append(User(self, user_info))

        results, overflow = limited(results, count)
        return Result(
            results,
            partial(self.search_user, query, count, next_cursor) if next_cursor else None,
            next_cursor,
            overflow=overflow,
            page_size=count
        )

    async def get_similar_tweets(self, tweet_id: str) -> list[Tweet]:
        """
        Retrieves tweets similar to the specified tweet (Twitter premium only).

        Parameters
        ----------
        tweet_id : :class:`str`
            The ID of the tweet for which similar tweets are to be retrieved.

        Returns
        -------
        list[:class:`Tweet`]
            A list of Tweet objects representing tweets
            similar to the specified tweet.
        """
        response, _ = await self.gql.similar_posts(tweet_id)
        items_ = find_dict(response, 'entries', find_one=True)
        results = []
        if not items_:
            return results

        for item in items_[0]:
            if not item['entryId'].startswith('tweet'):
                continue

            tweet = tweet_from_data(self, item)
            if tweet is not None:
                results.append(tweet)

        return results

    async def get_user_highlights_tweets(
        self,
        user_id: str,
        count: int = 20,
        cursor: str | None = None
    ) -> Result[Tweet]:
        """
        Retrieves highlighted tweets from a user's timeline.

        Parameters
        ----------
        user_id : :class:`str`
            The user ID
        count : :class:`int`, default=20
            The number of tweets to retrieve.

        Returns
        -------
        Result[:class:`Tweet`]
            An instance of the `Result` class containing the highlighted tweets.

        Examples
        --------
        >>> result = await client.get_user_highlights_tweets('123456789')
        >>> for tweet in result:
        ...     print(tweet)
        <Tweet id="...">
        <Tweet id="...">
        ...
        ...

        >>> more_results = await result.next()  # Retrieve more highlighted tweets
        >>> for tweet in more_results:
        ...     print(tweet)
        <Tweet id="...">
        <Tweet id="...">
        ...
        ...
        """
        response, _ = await self.gql.user_highlights_tweets(user_id, count, cursor)

        instructions = first_dict(response, 'instructions', [])
        instruction = find_entry_by_type(instructions, 'TimelineAddEntries')
        if instruction is None:
            return Result.empty()
        entries = instruction['entries']
        previous_cursor = None
        next_cursor = None
        results = []

        for entry in entries:
            entryId = entry['entryId']
            if entryId.startswith('tweet'):
                results.append(tweet_from_data(self, entry))
            elif entryId.startswith('cursor-top'):
                previous_cursor = entry['content']['value']
            elif entryId.startswith('cursor-bottom'):
                next_cursor = entry['content']['value']

        # X treats `count` as a hint on this endpoint too; trim client-side
        # and hand the surplus back through next() instead of dropping it.
        results, overflow = limited(results, count)

        return Result(
            results,
            partial(self.get_user_highlights_tweets, user_id, count, next_cursor) if next_cursor else None,
            next_cursor,
            partial(self.get_user_highlights_tweets, user_id, count, previous_cursor) if previous_cursor else None,
            previous_cursor,
            overflow=overflow,
            page_size=count
        )

    async def upload_media(
        self,
        source: str | bytes,
        wait_for_completion: bool = False,
        status_check_interval: float | None = None,
        media_type: str | None = None,
        media_category: str | None = None,
        is_long_video: bool = False
    ) -> str:
        """
        Uploads media to twitter.

        Parameters
        ----------
        source : :class:`str` | :class:`bytes`
            The source of the media to be uploaded.
            It can be either a file path or bytes of the media content.
        wait_for_completion : :class:`bool`, default=False
            Whether to wait for the completion of the media upload process.
        status_check_interval : :class:`float`, default=1.0
            The interval (in seconds) to check the status of the
            media upload process.
        media_type : :class:`str`, default=None
            The MIME type of the media.
            If not specified, it will be guessed from the source.
        media_category : :class:`str`, default=None
            The media category.
        is_long_video : :class:`bool`, default=False
            If this is True, videos longer than 2:20 can be uploaded.
            (Twitter Premium only)

        Returns
        -------
        :class:`str`
            The media ID of the uploaded media.

        Examples
        --------
        Videos, images and gifs can be uploaded.

        >>> media_id_1 = await client.upload_media(
        ...     'media1.jpg',
        ... )

        >>> media_id_2 = await client.upload_media(
        ...     'media2.mp4',
        ...     wait_for_completion=True
        ... )

        >>> media_id_3 = await client.upload_media(
        ...     'media3.gif',
        ...     wait_for_completion=True,
        ...     media_category='tweet_gif'  # media_category must be specified
        ... )
        """
        if not isinstance(wait_for_completion, bool):
            raise TypeError(
                'wait_for_completion must be bool,'
                f' not {wait_for_completion.__class__.__name__}'
            )

        if isinstance(source, str):
            # If the source is a path
            with open(source, 'rb') as file:
                binary = file.read()
        elif isinstance(source, bytes):
            # If the source is bytes
            binary = source

        if media_type is None:
            # Guess mimetype if not specified
            media_type = filetype.guess(binary).mime

        if wait_for_completion:
            if media_type == 'image/gif':
                if media_category is None:
                    raise TwitterException(
                        "`media_category` must be specified to check the "
                        "upload status of gif images ('dm_gif' or 'tweet_gif')"
                    )
            elif media_type.startswith('image'):
                # Checking the upload status of an image is impossible.
                wait_for_completion = False

        total_bytes = len(binary)

        # ============ INIT =============
        response, _ = await self.v11.upload_media_init(
            media_type, total_bytes, media_category, is_long_video
        )
        media_id = response['media_id']
        # =========== APPEND ============
        segment_index = 0
        bytes_sent = 0
        MAX_SEGMENT_SIZE = 8 * 1024 * 1024  # The maximum segment size is 8 MB
        append_tasks = []
        chunk_streams: list[io.BytesIO] = []

        while bytes_sent < total_bytes:
            chunk = binary[bytes_sent:bytes_sent + MAX_SEGMENT_SIZE]
            chunk_stream = io.BytesIO(chunk)
            coro = self.v11.upload_media_append(is_long_video, media_id, segment_index, chunk_stream)
            append_tasks.append(asyncio.create_task(coro))
            chunk_streams.append(chunk_stream)

            segment_index += 1
            bytes_sent += len(chunk)

        append_gather = asyncio.gather(*append_tasks)
        await append_gather

        # Close chunk streams
        for chunk_stream in chunk_streams:
            chunk_stream.close()

        # ========== FINALIZE ===========
        await self.v11.upload_media_finelize(is_long_video, media_id)
        # ===============================

        if wait_for_completion:
            while True:
                state = await self.check_media_status(media_id, is_long_video)
                processing_info = state['processing_info']
                if 'error' in processing_info:
                    raise InvalidMedia(processing_info['error'].get('message'))
                if processing_info['state'] == 'succeeded':
                    break
                await asyncio.sleep(status_check_interval or processing_info['check_after_secs'])

        return media_id

    async def check_media_status(
        self, media_id: str, is_long_video: bool = False
    ) -> dict:
        """
        Check the status of uploaded media.

        Parameters
        ----------
        media_id : :class:`str`
            The media ID of the uploaded media.

        Returns
        -------
        dict
            A dictionary containing information about the status of
            the uploaded media.

        Raises
        ------
        NotFound
            If X has no status for this media. The STATUS command only exists
            for chunked uploads - an image finishes in one request and has no
            ``processing_info``, so asking about one answers 404. This is why
            :func:`upload_media` turns ``wait_for_completion`` off for images.
        """
        response, _ = await self.v11.upload_media_status(is_long_video, media_id)
        return response

    async def create_media_metadata(
        self,
        media_id: str,
        alt_text: str | None = None,
        sensitive_warning: list[Literal['adult_content', 'graphic_violence', 'other']] = None
    ) -> Response:
        """
        Adds metadata to uploaded media.

        Parameters
        ----------
        media_id : :class:`str`
            The media id for which to create metadata.
        alt_text : :class:`str` | None, default=None
            Alternative text for the media.
        sensitive_warning : list{'adult_content', 'graphic_violence', 'other'}
            A list of sensitive content warnings for the media.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        Examples
        --------
        >>> media_id = await client.upload_media('media.jpg')
        >>> await client.create_media_metadata(
        ...     media_id,
        ...     alt_text='This is a sample media',
        ...     sensitive_warning=['other']
        ... )
        >>> await client.create_tweet(media_ids=[media_id])
        """
        _, response = await self.v11.create_media_metadata(media_id, alt_text, sensitive_warning)
        return response

    async def create_poll(
        self,
        choices: list[str],
        duration_minutes: int
    ) -> str:
        """
        Creates a poll and returns card-uri.

        Parameters
        ----------
        choices : list[:class:`str`]
            A list of choices for the poll. Maximum of 4 choices.
        duration_minutes : :class:`int`
            The duration of the poll in minutes.

        Returns
        -------
        :class:`str`
            The URI of the created poll card.

        Examples
        --------
        Create a poll with three choices lasting for 60 minutes:

        >>> choices = ['Option A', 'Option B', 'Option C']
        >>> duration_minutes = 60
        >>> card_uri = await client.create_poll(choices, duration_minutes)
        >>> print(card_uri)
        'card://0000000000000000000'
        """
        response, _ = await self.v11.create_card(choices, duration_minutes)
        return response['card_uri']

    async def vote(
        self,
        selected_choice: str,
        card_uri: str,
        tweet_id: str,
        card_name: str
    ) -> Poll:
        """
        Vote on a poll with the selected choice.
        Parameters
        ----------
        selected_choice : :class:`str`
            The label of the selected choice for the vote.
        card_uri : :class:`str`
            The URI of the poll card.
        tweet_id : :class:`str`
            The ID of the original tweet containing the poll.
        card_name : :class:`str`
            The name of the poll card.
        Returns
        -------
        :class:`Poll`
            The Poll object representing the updated poll after voting.
        """
        response, _ = await self.v11.vote(selected_choice, card_uri, tweet_id, card_name)
        card_data = {
            'rest_id': response['card']['url'],
            'legacy': response['card']
        }
        return Poll(self, card_data, None)

    async def create_tweet(
        self,
        text: str = '',
        media_ids: list[str] | None = None,
        poll_uri: str | None = None,
        reply_to: str | None = None,
        conversation_control: Literal['followers', 'verified', 'mentioned'] | None = None,
        attachment_url: str | None = None,
        community_id: str | None = None,
        share_with_followers: bool = False,
        is_note_tweet: bool = False,
        richtext_options: list[dict] = None,
        edit_tweet_id: str | None = None
    ) -> Tweet:
        """
        Creates a new tweet on Twitter with the specified
        text, media, and poll.

        Parameters
        ----------
        text : :class:`str`, default=''
            The text content of the tweet.
        media_ids : list[:class:`str`], default=None
            A list of media IDs or URIs to attach to the tweet.
            media IDs can be obtained by using the `upload_media` method.
        poll_uri : :class:`str`, default=None
            The URI of a Twitter poll card to attach to the tweet.
            Poll URIs can be obtained by using the `create_poll` method.
        reply_to : :class:`str`, default=None
            The ID of the tweet to which this tweet is a reply.
        conversation_control : {'followers', 'verified', 'mentioned'}
            The type of conversation control for the tweet:
            - 'followers': Limits replies to followers only.
            - 'verified': Limits replies to verified accounts only.
            - 'mentioned': Limits replies to mentioned accounts only.
        attachment_url : :class:`str`
            URL of the tweet to be quoted.
        is_note_tweet : :class:`bool`, default=False
            If this option is set to True, tweets longer than 280 characters
            can be posted (Twitter Premium only).
        richtext_options : list[:class:`dict`], default=None
            Options for decorating text (Twitter Premium only).
        edit_tweet_id : :class:`str` | None, default=None
            ID of the tweet to edit (Twitter Premium only).

        Raises
        ------
        :exc:`DuplicateTweet` : If the tweet is a duplicate of another tweet.

        Returns
        -------
        :class:`Tweet`
            The Created Tweet.

        Examples
        --------
        Create a tweet with media:

        >>> tweet_text = 'Example text'
        >>> media_ids = [
        ...     await client.upload_media('image1.png'),
        ...     await client.upload_media('image2.png')
        ... ]
        >>> await client.create_tweet(
        ...     tweet_text,
        ...     media_ids=media_ids
        ... )

        Create a tweet with a poll:

        >>> tweet_text = 'Example text'
        >>> poll_choices = ['Option A', 'Option B', 'Option C']
        >>> duration_minutes = 60
        >>> poll_uri = await client.create_poll(poll_choices, duration_minutes)
        >>> await client.create_tweet(
        ...     tweet_text,
        ...     poll_uri=poll_uri
        ... )

        See Also
        --------
        .upload_media
        .create_poll
        """
        _check_media_ids(media_ids)
        media_entities = [
            {'media_id': media_id, 'tagged_users': []}
            for media_id in (media_ids or [])
        ]
        limit_mode = None
        if conversation_control is not None:
            conversation_control = conversation_control.lower()
            limit_mode = {
                'followers': 'Community',
                'verified': 'Verified',
                'mentioned': 'ByInvitation'
            }[conversation_control]

        response, _ = await self.gql.create_tweet(
            is_note_tweet, text, media_entities, poll_uri,
            reply_to, attachment_url, community_id, share_with_followers,
            richtext_options, edit_tweet_id, limit_mode
        )
        errors = fatal_errors(response, 'tweet_results')
        if errors:
            raise_exceptions_from_response(errors)
            # Raising the whole error dict buried the one line that says what
            # went wrong - e.g. "You've hit the daily limit. Subscribe to
            # Premium for higher limits. (501)" - behind a wall of GraphQL
            # bookkeeping. Lead with the message, keep the rest reachable.
            error = errors[0]
            message = error.get('message') if isinstance(error, dict) else None
            raise CouldNotTweet(message or error)
        if is_note_tweet:
            _result = response['data']['notetweet_create']['tweet_results']
        else:
            _result = response['data']['create_tweet']['tweet_results']
        return tweet_from_data(self, _result)

    async def create_scheduled_tweet(
        self,
        scheduled_at: int,
        text: str = '',
        media_ids: list[str] | None = None,
    ) -> str:
        """
        Schedules a tweet to be posted at a specified timestamp.

        Parameters
        ----------
        scheduled_at : :class:`int`
            The timestamp when the tweet should be scheduled for posting.
        text : :class:`str`, default=''
            The text content of the tweet, by default an empty string.
        media_ids : list[:class:`str`], default=None
            A list of media IDs to be attached to the tweet, by default None.

        Returns
        -------
        :class:`str`
            The ID of the scheduled tweet.

        Examples
        --------
        Create a tweet with media:

        >>> scheduled_time = int(time.time()) + 3600  # One hour from now
        >>> tweet_text = 'Example text'
        >>> media_ids = [
        ...     await client.upload_media('image1.png'),
        ...     await client.upload_media('image2.png')
        ... ]
        >>> await client.create_scheduled_tweet(
        ...     scheduled_time
        ...     tweet_text,
        ...     media_ids=media_ids
        ... )
        """
        _check_media_ids(media_ids)
        response, _ = await self.gql.create_scheduled_tweet(scheduled_at, text, media_ids)
        errors = fatal_errors(response, 'tweet')
        if errors:
            raise_exceptions_from_response(errors)
            raise CouldNotTweet(
                errors[0].get('message') if isinstance(errors[0], dict)
                else errors[0]
            )
        return response['data']['tweet']['rest_id']

    async def delete_tweet(self, tweet_id: str) -> Response:
        """Deletes a tweet.

        Parameters
        ----------
        tweet_id : :class:`str`
            ID of the tweet to be deleted.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        Examples
        --------
        >>> tweet_id = '0000000000'
        >>> await delete_tweet(tweet_id)
        """
        _, response = await self.gql.delete_tweet(tweet_id)
        return response

    async def get_user_by_screen_name(self, screen_name: str) -> User:
        """
        Fetches a user by screen name.

        Parameter
        ---------
        screen_name : :class:`str`
            The screen name of the Twitter user.

        Returns
        -------
        :class:`User`
            An instance of the User class representing the
            Twitter user.

        Examples
        --------
        >>> target_screen_name = 'example_user'
        >>> user = await client.get_user_by_name(target_screen_name)
        >>> print(user)
        <User id="...">
        """
        response, _ = await self.gql.user_by_screen_name(screen_name)

        if 'user' not in response['data']:
            raise UserNotFound('The user does not exist.')
        user_data = subobject(response['data']['user'], 'result')
        if not user_data:
            raise UserNotFound('The user does not exist.')
        if user_data.get('__typename') == 'UserUnavailable':
            raise UserUnavailable(user_data.get('message'))

        return User(self, user_data)

    async def get_user_by_id(self, user_id: str) -> User:
        """
        Fetches a user by ID

        Parameter
        ---------
        user_id : :class:`str`
            The ID of the Twitter user.

        Returns
        -------
        :class:`User`
            An instance of the User class representing the
            Twitter user.

        Examples
        --------
        >>> target_screen_name = '000000000'
        >>> user = await client.get_user_by_id(target_screen_name)
        >>> print(user)
        <User id="000000000">
        """
        response, _ = await self.gql.user_by_rest_id(user_id)
        if 'result' not in response['data']['user']:
            raise TwitterException(f'Invalid user id: {user_id}')
        user_data = response['data']['user']['result']
        if user_data.get('__typename') == 'UserUnavailable':
            raise UserUnavailable(user_data.get('message'))
        return User(self, user_data)

    async def reverse_geocode(
        self, lat: float, long: float, accuracy: str | float | None = None,
        granularity: str | None = None, max_results: int | None = None
    ) -> list[Place]:
        """
        Given a latitude and a longitude, searches for up to 20 places that

        Parameters
        ----------
        lat : :class:`float`
            The latitude to search around.
        long : :class:`float`
            The longitude to search around.
        accuracy : :class:`str` | :class:`float` None, default=None
            A hint on the "region" in which to search.
        granularity : :class:`str` | None, default=None
            This is the minimal granularity of place types to return and must
            be one of: `neighborhood`, `city`, `admin` or `country`.
        max_results : :class:`int` | None, default=None
            A hint as to the number of results to return.

        Returns
        -------
        list[:class:`.Place`]
        """
        response, _ = await self.v11.reverse_geocode(lat, long, accuracy, granularity, max_results)
        return _places_from_response(self, response)

    async def search_geo(
        self, lat: float | None = None, long: float | None = None,
        query: str | None = None, ip: str | None = None,
        granularity: str | None = None, max_results: int | None = None
    ) -> list[Place]:
        """
        Search for places that can be attached to a Tweet via POST
        statuses/update.

        Parameters
        ----------
        lat : :class:`float` | None
            The latitude to search around.
        long : :class:`float` | None
            	The longitude to search around.
        query : :class:`str` | None
            Free-form text to match against while executing a geo-based query,
            best suited for finding nearby locations by name.
            Remember to URL encode the query.
        ip : :class:`str` | None
            An IP address. Used when attempting to
            fix geolocation based off of the user's IP address.
        granularity : :class:`str` | None
            This is the minimal granularity of place types to return and must
            be one of: `neighborhood`, `city`, `admin` or `country`.
        max_results : :class:`int` | None
            A hint as to the number of results to return.

        Returns
        -------
        list[:class:`.Place`]
        """
        response, _ = await self.v11.search_geo(lat, long, query, ip, granularity, max_results)
        return _places_from_response(self, response)

    async def get_place(self, id: str) -> Place:
        """
        Parameters
        ----------
        id : :class:`str`
            The ID of the place.

        Returns
        -------
        :class:`.Place`
        """
        response, _ = await self.v11.get_place(id)
        return Place(self, response)

    async def _get_more_replies(
        self, tweet_id: str, cursor: str
    ) -> Result[Tweet]:
        response, _ = await self.gql.tweet_detail(tweet_id, cursor)
        entries = first_dict(response, 'entries', [])

        results = []
        for entry in entries:
            if entry['entryId'].startswith(('cursor', 'label')):
                continue
            tweet = tweet_from_data(self, entry)
            if tweet is not None:
                results.append(tweet)

        # Mirror the two-shape handling added to `get_tweet_by_id`: without it
        # the first `await tweet.replies.next()` call would re-introduce the
        # KeyError that the parent fix eliminated (X serves the trailing cursor
        # as either `content.itemContent.value` or flat `content.value`).
        next_cursor = None
        _fetch_next_result = None
        if entries and entries[-1].get('entryId', '').startswith('cursor'):
            content = entries[-1].get('content') or {}
            item_content = content.get('itemContent')
            if isinstance(item_content, dict) and 'value' in item_content:
                next_cursor = item_content['value']
            elif 'value' in content:
                next_cursor = content['value']
            if next_cursor is not None:
                _fetch_next_result = partial(
                    self._get_more_replies, tweet_id, next_cursor)

        return Result(
            results,
            _fetch_next_result,
            next_cursor
        )

    async def _show_more_replies(
        self, tweet_id: str, cursor: str
    ) -> Result[Tweet]:
        response, _ = await self.gql.tweet_detail(tweet_id, cursor)
        items = first_dict(response, 'moduleItems', [])
        results = []
        for item in items:
            if 'tweet' not in item['entryId']:
                continue
            tweet = tweet_from_data(self, item)
            if tweet is not None:
                results.append(tweet)
        return Result(results)

    async def get_tweet_by_id(
        self, tweet_id: str, cursor: str | None = None
    ) -> Tweet:
        """
        Fetches a tweet by tweet ID.

        Parameters
        ----------
        tweet_id : :class:`str`
            The ID of the tweet.

        Returns
        -------
        :class:`Tweet`
            A Tweet object representing the fetched tweet.

        Examples
        --------
        >>> target_tweet_id = '...'
        >>> tweet = client.get_tweet_by_id(target_tweet_id)
        >>> print(tweet)
        <Tweet id="...">
        """
        response, _ = await self.gql.tweet_detail(tweet_id, cursor)

        errors = fatal_errors(response, 'entries')
        if errors:
            raise TweetNotAvailable(
                errors[0].get('message', 'The tweet is not available.')
            )

        entries = first_dict(response, 'entries', [])
        reply_to = []
        replies_list = []
        related_tweets = []
        tweet = None

        for entry in entries:
            if entry['entryId'].startswith('cursor'):
                continue
            tweet_object = tweet_from_data(self, entry)
            if tweet_object is None:
                continue

            if entry['entryId'].startswith('tweetdetailrelatedtweets'):
                related_tweets.append(tweet_object)
                continue

            if entry['entryId'] == f'tweet-{tweet_id}':
                tweet = tweet_object
            else:
                if tweet is None:
                    reply_to.append(tweet_object)
                else:
                    replies = []
                    sr_cursor = None
                    show_replies = None

                    for reply in (entry['content'].get('items') or [])[1:]:
                        if 'tweetcomposer' in reply['entryId']:
                            continue
                        if 'tweet' in reply.get('entryId'):
                            rpl = tweet_from_data(self, reply)
                            if rpl is None:
                                continue
                            replies.append(rpl)
                        if 'cursor' in reply.get('entryId'):
                            sr_cursor = reply['item']['itemContent']['value']
                            show_replies = partial(
                                self._show_more_replies,
                                tweet_id,
                                sr_cursor
                            )
                    tweet_object.replies = Result(
                        replies,
                        show_replies,
                        sr_cursor
                    )
                    replies_list.append(tweet_object)

                    display_type = find_dict(entry, 'tweetDisplayType', True)
                    if display_type and display_type[0] == 'SelfThread':
                        # `thread` means the same thing on both builders: the
                        # author's chain, oldest first, the tweet itself
                        # included. This one used to start at the first
                        # continuation instead, so the same tweet had a
                        # different thread[0] depending on which call produced
                        # it and callers could not treat the two alike.
                        tweet.thread = [tweet, tweet_object, *replies]

        if tweet is None:
            # X answers a nonexistent or hidden id with a timeline that has no
            # tweet entry and no `errors`, so nothing raised and the caller got
            # an AttributeError on None a few lines later.
            raise TweetNotAvailable(
                f'No tweet with id {tweet_id!r} is available.'
            )

        reply_next_cursor = None
        _fetch_more_replies = None
        if entries and entries[-1].get('entryId', '').startswith('cursor'):
            # X has two shapes for the trailing cursor entry: the legacy
            # `content.itemContent.value` and a newer, flatter `content.value`
            # (TimelineTimelineCursor without an itemContent wrapper). Reading
            # the old path unconditionally raises KeyError: 'itemContent' for
            # any tweet served with the new shape, which breaks the whole
            # `get_tweet_by_id` call — not just pagination of further replies.
            content = entries[-1].get('content') or {}
            item_content = content.get('itemContent')
            if isinstance(item_content, dict) and 'value' in item_content:
                reply_next_cursor = item_content['value']
            elif 'value' in content:
                reply_next_cursor = content['value']
            if reply_next_cursor is not None:
                _fetch_more_replies = partial(self._get_more_replies,
                                              tweet_id, reply_next_cursor)

        tweet.replies = Result(
            replies_list,
            _fetch_more_replies,
            reply_next_cursor
        )
        tweet.reply_to = reply_to
        tweet.related_tweets = related_tweets

        return tweet

    async def update_profile(
        self,
        name: str | None = None,
        description: str | None = None,
        location: str | None = None,
        url: str | None = None
    ) -> User:
        """
        Updates the profile of the logged in account.

        Only the arguments that are passed are changed; anything left as
        ``None`` keeps its current value. Pass an empty string to clear a
        field.

        Parameters
        ----------
        name : :class:`str` | None, default=None
            The display name, at most 50 characters.
        description : :class:`str` | None, default=None
            The bio.
        location : :class:`str` | None, default=None
            The location.
        url : :class:`str` | None, default=None
            The website shown on the profile.

        Returns
        -------
        :class:`User`
            The updated user.

        Examples
        --------
        >>> await client.update_profile(
        ...     description='Hello world', location='Tokyo'
        ... )
        """
        fields = {
            'name': name,
            'description': description,
            'location': location,
            'url': url
        }
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            raise ValueError('Nothing to update.')
        if name is not None and len(name) > 50:
            raise ValueError('`name` must be at most 50 characters.')

        response, _ = await self.v11.update_profile(fields)
        return User(self, build_user_data(response))

    async def get_about_account(self, screen_name: str) -> dict:
        """
        Retrieves the "About this account" panel of a user - the origin
        details X started showing on profiles.

        Parameters
        ----------
        screen_name : :class:`str`
            The screen name of the user.

        Returns
        -------
        dict
            Keys include ``account_based_in`` (the country X believes the
            account operates from), ``created_country_accurate``,
            ``location_accurate``, ``source`` (the platform it signed up on)
            and ``username_changes`` with a ``count``. The panel is empty for
            accounts X has nothing to show for.

        Examples
        --------
        >>> about = await client.get_about_account('nike')
        >>> print(about['account_based_in'], about['source'])
        United States Web
        """
        response, _ = await self.gql.about_account(screen_name)
        data = response.get('data') or {}
        # X answers an unresolvable handle with an entirely empty `data` and
        # no error, while a real account always comes back under
        # user_result_by_screen_name - even when the panel itself is empty.
        # Without this the two are the same answer, so a user id or a typo
        # read as "this account has nothing to show".
        if 'user_result_by_screen_name' not in data:
            raise UserNotFound('The user does not exist.')
        result = subobject(
            subobject(
                subobject(data, 'user_result_by_screen_name'), 'result'
            ) or {},
            'about_profile'
        )
        return result

    async def get_user_spotlights(self, screen_name: str) -> list[dict]:
        """
        Retrieves the spotlight modules pinned to a profile - the panels
        professional accounts can show above their timeline.

        Parameters
        ----------
        screen_name : :class:`str`
            The screen name of the user.

        Returns
        -------
        list[dict]
            The spotlight modules, empty for accounts that pin none.

        Examples
        --------
        >>> spotlights = await client.get_user_spotlights('nike')
        """
        response, _ = await self.gql.profile_spotlights(screen_name)
        data = response.get('data') or {}
        # X answers an unresolvable handle with an entirely empty `data` and
        # no error, while a real account always comes back under
        # user_result_by_screen_name - even when the panel itself is empty.
        # Without this the two are the same answer, so a user id or a typo
        # read as "this account has nothing to show".
        if 'user_result_by_screen_name' not in data:
            raise UserNotFound('The user does not exist.')
        result = subobject(
            subobject(data, 'user_result_by_screen_name'), 'result'
        )
        modules = subobject(result, 'profilemodules').get('v1')
        return modules if isinstance(modules, list) else []

    async def get_user_mentions(
        self,
        screen_name: str,
        count: int = 20,
        cursor: str | None = None
    ) -> Result[Tweet]:
        """
        Retrieves tweets mentioning a user.

        Unlike :func:`get_notifications`, this works for any account, not
        just the logged in one - it is a search, so it only reaches tweets
        the search index still holds.

        Parameters
        ----------
        screen_name : :class:`str`
            The screen name to look for, with or without a leading ``@``.
        count : :class:`int`, default=20
            The number of tweets to retrieve.
        cursor : :class:`str`, default=None
            A cursor for pagination.

        Returns
        -------
        Result[:class:`Tweet`]
            Tweets mentioning the user.

        Examples
        --------
        >>> mentions = await client.get_user_mentions('elonmusk')
        >>> for tweet in mentions:
        ...     print(tweet.text)

        See Also
        --------
        .search_tweet
        .get_notifications
        """
        handle = screen_name.lstrip('@')
        # This searches for the literal text "@handle", so a user id produces
        # the query "@1234567890", which matches nothing and comes back as an
        # ordinary empty result - no error, just a wrong answer that looks
        # like "nobody mentioned them".
        if handle.isdigit():
            raise ValueError(
                f'`screen_name` must be a handle, not a user id: {handle!r}. '
                'Resolve it first with get_user_by_id(...).screen_name.'
            )
        return await self.search_tweet(
            f'@{handle}', 'Latest', count, cursor
        )

    async def search_tweets_by_date(
        self,
        query: str,
        since: str,
        until: str,
        product: Literal['Top', 'Latest', 'Media'] = 'Latest',
        count: int = 20,
        cursor: str | None = None
    ) -> Result[Tweet]:
        """
        Searches tweets posted within a date range.

        Thin wrapper over :func:`search_tweet` and :func:`build_query` - the
        operators exist already, but everyone ends up rediscovering them.

        Parameters
        ----------
        query : :class:`str`
            The search text.
        since : :class:`str`
            Start date, ``YYYY-MM-DD``, inclusive.
        until : :class:`str`
            End date, ``YYYY-MM-DD``, exclusive.
        product : {'Top', 'Latest', 'Media'}, default='Latest'
            The search tab.
        count : :class:`int`, default=20
            The number of tweets to retrieve.
        cursor : :class:`str`, default=None
            A cursor for pagination.

        Returns
        -------
        Result[:class:`Tweet`]
            The matching tweets.

        Examples
        --------
        >>> tweets = await client.search_tweets_by_date(
        ...     'python', '2024-01-01', '2024-02-01'
        ... )

        See Also
        --------
        .search_tweet
        .build_query
        """
        return await self.search_tweet(
            build_query(query, {'since': since, 'until': until}),
            product, count, cursor
        )

    async def get_thread(self, tweet_id: str) -> list[Tweet]:
        """
        Retrieves a whole self-thread from any tweet inside it.

        No single call returns the full thread: asking about the head gives
        the continuation but not the head's own ancestors, and asking about
        the tail gives the ancestors but no continuation. This stitches
        `reply_to`, the tweet itself and `thread` together and drops replies
        written by anybody else, so the result is the author's chain in
        chronological order regardless of which tweet you started from.

        Parameters
        ----------
        tweet_id : :class:`str`
            The ID of any tweet in the thread.

        Returns
        -------
        list[:class:`Tweet`]
            The thread, oldest first. A tweet that is not part of a thread
            comes back as a single-element list.

        Examples
        --------
        >>> thread = await client.get_thread('0000000000')
        >>> for tweet in thread:
        ...     print(tweet.text)

        See Also
        --------
        .get_tweet_by_id
        """
        tweet = await self.get_tweet_by_id(tweet_id)
        author_id = tweet.user.id if tweet.user is not None else None

        chain = []
        seen = set()
        for part in [tweet.reply_to or [], [tweet], tweet.thread or []]:
            for item in part:
                if item.id in seen:
                    continue
                item_author = item.user.id if item.user is not None else None
                if author_id is not None and item_author != author_id:
                    continue
                seen.add(item.id)
                chain.append(item)
        return chain

    async def get_tweet_by_url(self, url: str) -> Tweet:
        """
        Fetches a tweet by its URL.

        Parameters
        ----------
        url : :class:`str`
            The URL of the tweet, e.g.
            ``https://x.com/elonmusk/status/1519480761749016577``.
            Query strings, trailing paths such as ``/photo/1`` and the
            ``twitter.com`` / ``fxtwitter.com`` style hosts are all accepted.

        Returns
        -------
        :class:`Tweet`
            The tweet.

        Examples
        --------
        >>> tweet = await client.get_tweet_by_url(
        ...     'https://x.com/elonmusk/status/1519480761749016577'
        ... )
        >>> print(tweet.text)

        See Also
        --------
        .get_tweet_by_id
        """
        match = re.search(r'/status(?:es)?/(\d+)', url)
        if match is None:
            raise ValueError(f'Not a tweet URL: {url!r}')
        return await self.get_tweet_by_id(match.group(1))

    async def get_tweets_by_ids(self, ids: list[str]) -> list[Tweet]:
        """
        Retrieve multiple tweets by IDs.

        Parameters
        ----------
        ids : list[:class:`str`]
            A list of tweet IDs to retrieve.

        Returns
        -------
        list[:class:`Tweet`]
            List of tweets.

        Examples
        --------
        >>> tweet_ids = ['1111111111', '1111111112', '111111113']
        >>> tweets = await client.get_tweets_by_ids(tweet_ids)
        >>> print(tweets)
        [<Tweet id="1111111111">, <Tweet id="1111111112">, <Tweet id="111111113">]
        """
        response, _ = await self.gql.tweet_results_by_rest_ids(ids)
        tweet_results = (response.get('data') or {}).get('tweetResult') or []
        results = []
        for tweet_result in tweet_results:
            results.append(tweet_from_data(self, tweet_result))
        return results

    async def get_scheduled_tweets(self) -> list[ScheduledTweet]:
        """
        Retrieves scheduled tweets.

        Returns
        -------
        list[:class:`ScheduledTweet`]
            List of ScheduledTweet objects representing the scheduled tweets.
        """
        response, _ = await self.gql.fetch_scheduled_tweets()
        tweets = first_dict(response, 'scheduled_tweet_list', [])
        return [ScheduledTweet(self, tweet) for tweet in tweets]

    async def delete_scheduled_tweet(self, tweet_id: str) -> Response:
        """
        Delete a scheduled tweet.

        Parameters
        ----------
        tweet_id : :class:`str`
            The ID of the scheduled tweet to delete.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.
        """
        _, response = await self.gql.delete_scheduled_tweet(tweet_id)
        return response

    async def _get_tweet_engagements(
        self, tweet_id: str, count: int, cursor: str, f
    ) -> Result[User]:
        """
        Base function to get tweet engagements.
        type0: retweeters
        type1: favoriters
        """
        response, _ = await f(tweet_id, count, cursor)
        items_ = find_dict(response, 'entries', True)
        if not items_:
            return Result([])
        items = items_[0]
        next_cursor = last_cursor(items)
        previous_cursor = cursor_at(items, -2)

        results = []
        for item in items:
            if not item['entryId'].startswith('user'):
                continue
            user_info_ = find_dict(item, 'result', True)
            if not user_info_:
                continue
            user_info = user_info_[0]
            results.append(User(self, user_info))

        results, overflow = limited(results, count)
        return Result(
            results,
            partial(self._get_tweet_engagements, tweet_id, count, next_cursor, f) if next_cursor else None,
            next_cursor,
            partial(self._get_tweet_engagements, tweet_id, count, previous_cursor, f) if previous_cursor else None,
            previous_cursor,
            overflow=overflow,
            page_size=count
        )

    async def get_retweeters(
        self, tweet_id: str, count: int = 40, cursor: str | None = None
    ) -> Result[User]:
        """
        Retrieve users who retweeted a specific tweet.

        Parameters
        ----------
        tweet_id : :class:`str`
            The ID of the tweet.
        count : :class:`int`, default=40
            The maximum number of users to retrieve.
        cursor : :class:`str`, default=None
            A string indicating the position of the cursor for pagination.

        Returns
        -------
        Result[:class:`User`]
            A list of users who retweeted the tweet.

        Examples
        --------
        >>> tweet_id = '...'
        >>> retweeters = client.get_retweeters(tweet_id)
        >>> print(retweeters)
        [<User id="...">, <User id="...">, ..., <User id="...">]

        >>> more_retweeters = retweeters.next()  # Retrieve more retweeters.
        >>> print(more_retweeters)
        [<User id="...">, <User id="...">, ..., <User id="...">]
        """
        return await self._get_tweet_engagements(tweet_id, count, cursor, self.gql.retweeters)

    async def get_favoriters(
        self, tweet_id: str, count: int = 40, cursor: str | None = None
    ) -> Result[User]:
        """
        Retrieve users who favorited a specific tweet.

        Parameters
        ----------
        tweet_id : :class:`str`
            The ID of the tweet.
        count : int, default=40
            The maximum number of users to retrieve.
        cursor : :class:`str`, default=None
            A string indicating the position of the cursor for pagination.

        Returns
        -------
        Result[:class:`User`]
            A list of users who favorited the tweet.

        Examples
        --------
        >>> tweet_id = '...'
        >>> favoriters = await client.get_favoriters(tweet_id)
        >>> print(favoriters)
        [<User id="...">, <User id="...">, ..., <User id="...">]

        >>> # Retrieve more favoriters.
        >>> more_favoriters = await favoriters.next()
        >>> print(more_favoriters)
        [<User id="...">, <User id="...">, ..., <User id="...">]
        """
        return await self._get_tweet_engagements(tweet_id, count, cursor, self.gql.favoriters)

    async def get_community_note(self, note_id: str) -> CommunityNote:
        """
        Fetches a community note by ID.

        Parameters
        ----------
        note_id : :class:`str`
            The ID of the community note.

        Returns
        -------
        :class:`CommunityNote`
            A CommunityNote object representing the fetched community note.

        Raises
        ------
        :exc:`TwitterException`
            Invalid note ID.

        Examples
        --------
        >>> note_id = '...'
        >>> note = client.get_community_note(note_id)
        >>> print(note)
        <CommunityNote id="...">
        """
        response, _ = await self.gql.bird_watch_one_note(note_id)
        note_data = (
            response.get('data') or {}
        ).get('birdwatch_note_by_rest_id')
        if note_data is None:
            raise NotFound(f'No community note with id {note_id!r}.')
        if 'data_v1' not in note_data:
            raise TwitterException(f'Invalid note id: {note_id}')
        return CommunityNote(self, note_data)

    async def get_user_tweets(
        self,
        user_id: str,
        tweet_type: Literal['Tweets', 'Replies', 'Media', 'Likes'],
        count: int = 40,
        cursor: str | None = None
    ) -> Result[Tweet]:
        """
        Fetches tweets from a specific user's timeline.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the Twitter user whose tweets to retrieve.
            To get the user id from the screen name, you can use
            `get_user_by_screen_name` method.
        tweet_type : {'Tweets', 'Replies', 'Media', 'Likes'}
            The type of tweets to retrieve.
        count : :class:`int`, default=40
            The number of tweets to retrieve.
        cursor : :class:`str`, default=None
            The cursor for fetching the next set of results.

        Returns
        -------
        Result[:class:`Tweet`]
            A Result object containing a list of `Tweet` objects.

        Examples
        --------
        >>> user_id = '...'

        If you only have the screen name, you can get the user id as follows:

        >>> screen_name = 'example_user'
        >>> user = client.get_user_by_screen_name(screen_name)
        >>> user_id = user.id

        >>> tweets = await client.get_user_tweets(user_id, 'Tweets', count=20)
        >>> for tweet in tweets:
        ...    print(tweet)
        <Tweet id="...">
        <Tweet id="...">
        ...
        ...

        >>> more_tweets = await tweets.next()  # Retrieve more tweets
        >>> for tweet in more_tweets:
        ...     print(tweet)
        <Tweet id="...">
        <Tweet id="...">
        ...
        ...

        >>> # Retrieve previous tweets
        >>> previous_tweets = await tweets.previous()

        See Also
        --------
        .get_user_by_screen_name
        """
        tweet_type = tweet_type.capitalize()
        endpoints = {
            'Tweets': self.gql.user_tweets,
            'Replies': self.gql.user_tweets_and_replies,
            'Media': self.gql.user_media,
            'Likes': self.gql.user_likes,
        }
        if tweet_type not in endpoints:
            # A typo used to surface as KeyError('Bzdura'), which says nothing
            # about what was expected.
            raise ValueError(
                f'Invalid tweet_type {tweet_type!r}; '
                f'expected one of {", ".join(endpoints)}.'
            )
        f = endpoints[tweet_type]
        response, _ = await f(user_id, count, cursor)

        # A protected (or suspended/deactivated) account answers with a bare
        # UserUnavailable result and no timeline at all. Without this it is
        # indistinguishable from an account that simply has not tweeted, so
        # callers got an empty Result and no idea why.
        user_result = subobject(subobject(response.get('data') or {}, 'user'), 'result')
        if user_result.get('__typename') == 'UserUnavailable':
            raise UserUnavailable(
                user_result.get('message') or
                'The account is protected, suspended or deactivated.'
            )

        instructions_ = find_dict(response, 'instructions', True)
        if not instructions_:
            return Result([])
        instructions = instructions_[0]

        # accounts with no visible tweets return a timeline with no entries or
        # cursor entries; derive cursors only when present, else return empty
        entries_instr = find_dict(response, 'entries', find_one=True)
        items = entries_instr[0] if entries_instr else []

        def _cursor(entries, kind):
            for entry in entries:
                if entry.get('entryId', '').startswith(f'cursor-{kind}'):
                    return entry.get('content', {}).get('value')
            return None

        next_cursor = _cursor(items, 'bottom')
        previous_cursor = _cursor(items, 'top')

        if tweet_type == 'Media':
            if cursor is None:
                module_items = [
                    e for e in items
                    if 'items' in e.get('content', {})
                ]
                items = module_items[0]['content']['items'] if module_items else []
            else:
                # TimelineAddToModule is rarely the first instruction, so
                # indexing [0] returned nothing and Media paginated to an
                # empty second page without any error.
                items = first_dict(instructions, 'moduleItems', [])

        results = []

        # A pinned tweet arrives in its own TimelinePinEntry instruction rather
        # than among the entries, so iterating the entries alone silently drops
        # it. It belongs at the top, the way the profile shows it, and only on
        # the first page - otherwise every page would repeat it.
        pinned_ids = set()
        if tweet_type == 'Tweets' and cursor is None:
            for instruction in instructions:
                if instruction.get('type') != 'TimelinePinEntry':
                    continue
                pinned = instruction.get('entry', {}).get('content', {})
                pinned_tweet = tweet_from_data(self, pinned.get('itemContent', {}))
                if pinned_tweet is not None:
                    pinned_ids.add(pinned_tweet.id)
                    results.append(pinned_tweet)

        for item in items:
            entry_id = item['entryId']

            if not entry_id.startswith(('tweet', 'profile-conversation', 'profile-grid')):
                continue

            # `item` gets reassigned to one of the module's children below,
            # so the module metadata has to be read off it first.
            conversation_ids = _conversation_ids(item.get('content') or {})

            if entry_id.startswith('profile-conversation'):
                tweets = item['content']['items']
                if tweet_type == 'Replies':
                    # On the Replies tab a conversation reads
                    # [what was replied to, the user's reply], so returning the
                    # first entry hands back somebody else's tweet - the
                    # opposite of what the tab is for. Emit the user's own
                    # entries and keep the rest as context.
                    own = [
                        t for t in tweets
                        if _conversation_author_id(t) == user_id
                    ]
                    context = [
                        t for t in tweets
                        if _conversation_author_id(t) != user_id
                    ]
                    if own:
                        replies = []
                        for other in context:
                            tweet_object = tweet_from_data(self, other)
                            if tweet_object is None:
                                continue
                            replies.append(tweet_object)
                        # Taking own[-1] threw away every earlier reply the
                        # author made in the same conversation - a self-thread
                        # under someone else's post lost all but its last
                        # tweet, silently. Emit them all.
                        extra_own = own[:-1]
                        for other in extra_own:
                            tweet_object = tweet_from_data(self, other)
                            if tweet_object is None:
                                continue
                            tweet_object.replies = replies
                            # These leave through a second door, so they need
                            # the same two things the tweet below gets: the
                            # module's ids, and the pinned check that stops a
                            # tweet already injected at the top coming back.
                            tweet_object.conversation_ids = conversation_ids
                            if tweet_object.id in pinned_ids:
                                continue
                            results.append(tweet_object)
                        item = own[-1]
                    else:
                        replies = None
                        item = tweets[0]
                else:
                    replies = []
                    for reply in tweets[1:]:
                        tweet_object = tweet_from_data(self, reply)
                        if tweet_object is None:
                            continue
                        replies.append(tweet_object)
                    item = tweets[0]
            else:
                replies = None

            tweet = tweet_from_data(self, item)
            if tweet is None:
                continue
            tweet.replies = replies
            tweet.conversation_ids = conversation_ids
            if replies and all(
                r.user is not None and tweet.user is not None
                and r.user.id == tweet.user.id
                for r in replies
            ):
                # Only a module where every entry is the same author is a
                # thread. On the Replies tab the module also carries the tweet
                # being replied to, written by somebody else - filing that
                # under `thread` claimed the author had written a self-thread
                # they never wrote.
                tweet.thread = [tweet, *replies]
            if tweet.id in pinned_ids:
                # X usually keeps the pinned tweet out of the entries, but not
                # when it heads a conversation module - then it arrives twice
                # and the injected copy above already covered it. (It can still
                # come back at its chronological place on a later page, which
                # is X repeating itself across cursors, not a duplicate here.)
                continue
            results.append(tweet)

        results, overflow = limited(results, count)

        return Result(
            results,
            partial(self.get_user_tweets, user_id, tweet_type, count, next_cursor) if next_cursor else None,
            next_cursor,
            partial(self.get_user_tweets, user_id, tweet_type, count, previous_cursor) if previous_cursor else None,
            previous_cursor,
            overflow=overflow,
            page_size=count
        )

    async def get_timeline(
        self,
        count: int = 20,
        seen_tweet_ids: list[str] | None = None,
        cursor: str | None = None
    ) -> Result[Tweet]:
        """
        Retrieves the timeline.
        Retrieves tweets from Home -> For You.

        Parameters
        ----------
        count : :class:`int`, default=20
            The number of tweets to retrieve.
        seen_tweet_ids : list[:class:`str`], default=None
            A list of tweet IDs that have been seen.
        cursor : :class:`str`, default=None
            A cursor for pagination.

        Returns
        -------
        Result[:class:`Tweet`]
            A Result object containing a list of Tweet objects.

        Example
        -------
        >>> tweets = await client.get_timeline()
        >>> for tweet in tweets:
        ...     print(tweet)
        <Tweet id="...">
        <Tweet id="...">
        ...
        ...
        >>> more_tweets = await tweets.next() # Retrieve more tweets
        >>> for tweet in more_tweets:
        ...     print(tweet)
        <Tweet id="...">
        <Tweet id="...">
        ...
        ...
        """
        response, _ = await self.gql.home_timeline(count, seen_tweet_ids, cursor)
        items = first_dict(response, 'entries', [])
        next_cursor = last_cursor(items)
        results = []

        for item in items:
            if 'itemContent' not in item['content']:
                continue
            tweet = tweet_from_data(self, item)
            if tweet is None:
                continue
            results.append(tweet)

        # X ignores `count` on the home timeline too - it kept returning ~28
        # no matter what was asked for.
        results, overflow = limited(results, count)

        return Result(
            results,
            partial(self.get_timeline, count, seen_tweet_ids, next_cursor) if next_cursor else None,
            next_cursor,
            overflow=overflow,
            page_size=count
        )

    async def get_latest_timeline(
        self,
        count: int = 20,
        seen_tweet_ids: list[str] | None = None,
        cursor: str | None = None
    ) -> Result[Tweet]:
        """
        Retrieves the timeline.
        Retrieves tweets from Home -> Following.

        Parameters
        ----------
        count : :class:`int`, default=20
            The number of tweets to retrieve.
        seen_tweet_ids : list[:class:`str`], default=None
            A list of tweet IDs that have been seen.
        cursor : :class:`str`, default=None
            A cursor for pagination.

        Returns
        -------
        Result[:class:`Tweet`]
            A Result object containing a list of Tweet objects.

        Example
        -------
        >>> tweets = await client.get_latest_timeline()
        >>> for tweet in tweets:
        ...     print(tweet)
        <Tweet id="...">
        <Tweet id="...">
        ...
        ...
        >>> more_tweets = await tweets.next() # Retrieve more tweets
        >>> for tweet in more_tweets:
        ...     print(tweet)
        <Tweet id="...">
        <Tweet id="...">
        ...
        ...
        """
        response, _ = await self.gql.home_latest_timeline(count, seen_tweet_ids, cursor)
        items = first_dict(response, 'entries', [])
        next_cursor = last_cursor(items)
        results = []

        def handle_item(item, conversation_ids=None):
            tweet = tweet_from_data(self, item)
            if tweet is not None:
                tweet.conversation_ids = conversation_ids
                results.append(tweet)

        for item in items:
            if 'items' in item['content']:  # home-conversation entries
                conversation_ids = _conversation_ids(item['content'])
                for sub_item in item['content']['items']:
                    if 'itemContent' not in sub_item['item']:
                        continue
                    handle_item(sub_item, conversation_ids)
            else:  # tweet entries
                if 'itemContent' not in item['content']:
                    continue
                handle_item(item)

        results, overflow = limited(results, count)

        return Result(
            results,
            partial(self.get_latest_timeline, count, seen_tweet_ids, next_cursor) if next_cursor else None,
            next_cursor,
            overflow=overflow,
            page_size=count
        )

    async def favorite_tweet(self, tweet_id: str) -> Response:
        """
        Favorites a tweet.

        Parameters
        ----------
        tweet_id : :class:`str`
            The ID of the tweet to be liked.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        Examples
        --------
        >>> tweet_id = '...'
        >>> await client.favorite_tweet(tweet_id)

        See Also
        --------
        .unfavorite_tweet
        """
        _, response = await self.gql.favorite_tweet(tweet_id)
        return response

    async def unfavorite_tweet(self, tweet_id: str) -> Response:
        """
        Unfavorites a tweet.

        Parameters
        ----------
        tweet_id : :class:`str`
            The ID of the tweet to be unliked.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        Examples
        --------
        >>> tweet_id = '...'
        >>> await client.unfavorite_tweet(tweet_id)

        See Also
        --------
        .favorite_tweet
        """
        _, response = await self.gql.unfavorite_tweet(tweet_id)
        return response

    async def retweet(self, tweet_id: str) -> Response:
        """
        Retweets a tweet.

        Parameters
        ----------
        tweet_id : :class:`str`
            The ID of the tweet to be retweeted. Passing a retweet's own id
            works, but X resolves it to the tweet being retweeted, so what
            appears on the timeline is a retweet of that original.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        Examples
        --------
        >>> tweet_id = '...'
        >>> await client.retweet(tweet_id)

        Note
        ----
        Undo this with the id of the **original** tweet, not the id you
        passed here and not the id X reports for the retweet it created -
        see :func:`delete_retweet`.

        See Also
        --------
        .delete_retweet
        """
        _, response = await self.gql.retweet(tweet_id)
        return response

    async def delete_retweet(self, tweet_id: str) -> Response:
        """
        Deletes the retweet.

        Parameters
        ----------
        tweet_id : :class:`str`
            The ID of the **original** tweet - the one that was retweeted.
            Not the id of the retweet itself.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        Examples
        --------
        >>> tweet_id = '...'
        >>> await client.delete_retweet(tweet_id)

        Warning
        -------
        X answers 200 whether or not anything was removed, and the body only
        echoes the id it was given - so a call that undid nothing looks
        exactly like one that worked. Measured: unretweeting a tweet that was
        never retweeted returns the same shape as a real one.

        This bites when the id came from a timeline. A retweet carries its own
        id, and passing that here silently does nothing; so does the id X
        reports for the retweet it created. Use ``tweet.retweeted_tweet.id``
        when the tweet is a retweet, and re-read the timeline if you need to
        be sure it is gone.

        See Also
        --------
        .retweet
        """
        _, response = await self.gql.delete_retweet(tweet_id)
        return response

    async def bookmark_tweet(
        self, tweet_id: str, folder_id: str | None = None
    ) -> Response:
        """
        Adds the tweet to bookmarks.

        Parameters
        ----------
        tweet_id : :class:`str`
            The ID of the tweet to be bookmarked.
        folder_id : :class:`str` | None, default=None
            The ID of the folder to add the bookmark to.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        Examples
        --------
        >>> tweet_id = '...'
        >>> await client.bookmark_tweet(tweet_id)
        """
        if folder_id is None:
            _, response = await self.gql.create_bookmark(tweet_id)
        else:
            _, response = await self.gql.bookmark_tweet_to_folder(tweet_id, folder_id)
        return response

    async def delete_bookmark(self, tweet_id: str) -> Response:
        """
        Removes the tweet from bookmarks.

        Parameters
        ----------
        tweet_id : :class:`str`
            The ID of the tweet to be removed from bookmarks.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        Examples
        --------
        >>> tweet_id = '...'
        >>> await client.delete_bookmark(tweet_id)

        See Also
        --------
        .bookmark_tweet
        """
        _, response = await self.gql.delete_bookmark(tweet_id)
        return response

    async def get_bookmarks(
        self, count: int = 20,
        cursor: str | None = None, folder_id: str | None = None
    ) -> Result[Tweet]:
        """
        Retrieves bookmarks from the authenticated user's Twitter account.

        Parameters
        ----------
        count : :class:`int`, default=20
            The number of bookmarks to retrieve.
        folder_id : :class:`str` | None, default=None
            Folder to retrieve bookmarks.

        Returns
        -------
        Result[:class:`Tweet`]
            A Result object containing a list of Tweet objects
            representing bookmarks.

        Example
        -------
        >>> bookmarks = await client.get_bookmarks()
        >>> for bookmark in bookmarks:
        ...     print(bookmark)
        <Tweet id="...">
        <Tweet id="...">

        >>> # # To retrieve more bookmarks
        >>> more_bookmarks = await bookmarks.next()
        >>> for bookmark in more_bookmarks:
        ...     print(bookmark)
        <Tweet id="...">
        <Tweet id="...">
        """
        if folder_id is None:
            response, _ = await self.gql.bookmarks(count, cursor)
        else:
            response, _ = await self.gql.bookmark_folder_timeline(count, cursor, folder_id)

        items_ = find_dict(response, 'entries', find_one=True)
        if not items_:
            return Result([])
        items = items_[0]
        next_cursor = last_cursor(items)
        if folder_id is None:
            previous_cursor = cursor_at(items, -2)
            fetch_previous_result = partial(self.get_bookmarks, count, previous_cursor, folder_id)
        else:
            previous_cursor = None
            fetch_previous_result = None

        results = []
        for item in items:
            tweet = tweet_from_data(self, item)
            if tweet is None:
                continue
            results.append(tweet)

        # X treats `count` as a hint on this endpoint too; trim client-side
        # and hand the surplus back through next() instead of dropping it.
        results, overflow = limited(results, count)

        return Result(
            results,
            partial(self.get_bookmarks, count, next_cursor, folder_id) if next_cursor else None,
            next_cursor,
            fetch_previous_result,
            previous_cursor,
            overflow=overflow,
            page_size=count
        )

    async def delete_all_bookmarks(self) -> Response:
        """
        Deleted all bookmarks.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        Examples
        --------
        >>> await client.delete_all_bookmarks()
        """
        _, response = await self.gql.delete_all_bookmarks()
        return response

    async def get_bookmark_folders(self, cursor: str | None = None) -> Result[BookmarkFolder]:
        """
        Retrieves bookmark folders.

        Returns
        -------
        Result[:class:`BookmarkFolder`]
            Result object containing a list of bookmark folders.

        Examples
        --------
        >>> folders = await client.get_bookmark_folders()
        >>> print(folders)
        [<BookmarkFolder id="...">, ..., <BookmarkFolder id="...">]
        >>> more_folders = await folders.next()  # Retrieve more folders
        """
        response, _ = await self.gql.bookmark_folders_slice(cursor)

        errors = fatal_errors(response, 'bookmark_collections_slice')
        if errors:
            raise TwitterException(
                errors[0].get('message', 'Failed to retrieve bookmark folders.')
            )

        slice_ = find_dict(response, 'bookmark_collections_slice', find_one=True)
        if not slice_:
            return Result([])
        slice = slice_[0]
        # X omits the bottom cursor on the last page, which left this
        # unbound and raised UnboundLocalError instead of ending the walk.
        next_cursor = None
        results = []
        for item in slice['items']:
            results.append(BookmarkFolder(self, item))

        if 'next_cursor' in slice['slice_info']:
            next_cursor = slice['slice_info']['next_cursor']
            fetch_next_result = partial(self.get_bookmark_folders, next_cursor)
        else:
            next_cursor = None
            fetch_next_result = None

        return Result(
            results,
            fetch_next_result,
            next_cursor
        )

    async def edit_bookmark_folder(
        self, folder_id: str, name: str
    ) -> BookmarkFolder:
        """
        Edits a bookmark folder.

        Parameters
        ----------
        folder_id : :class:`str`
            ID of the folder to edit.
        name : :class:`str`
            New name for the folder.

        Returns
        -------
        :class:`BookmarkFolder`
            Updated bookmark folder.

        Examples
        --------
        >>> await client.edit_bookmark_folder('123456789', 'MyFolder')
        """
        response, _ = await self.gql.edit_bookmark_folder(folder_id, name)
        return BookmarkFolder(self, response['data']['bookmark_collection_update'])

    async def delete_bookmark_folder(self, folder_id: str) -> Response:
        """
        Deletes a bookmark folder.

        Parameters
        ----------
        folder_id : :class:`str`
            ID of the folder to delete.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.
        """
        _, response = await self.gql.delete_bookmark_folder(folder_id)
        return response

    async def create_bookmark_folder(self, name: str) -> BookmarkFolder:
        """Creates a bookmark folder.

        Parameters
        ----------
        name : :class:`str`
            Name of the folder.

        Returns
        -------
        :class:`BookmarkFolder`
            Newly created bookmark folder.
        """
        response, _ = await self.gql.create_bookmark_folder(name)
        errors = fatal_errors(response, 'bookmark_collection_create')
        if errors:
            # Bookmark collections are Premium-only; X answers code 37,
            # "User is not authorized to use bookmark collections". Indexing
            # the missing key turned that into a bare KeyError.
            raise_exceptions_from_response(errors)
            raise TwitterException(errors[0].get('message') or errors[0])
        folder = (response.get('data') or {}).get('bookmark_collection_create')
        if folder is None:
            raise TwitterException(
                'X returned no folder for the new bookmark collection.'
            )
        return BookmarkFolder(self, folder)

    async def follow_user(self, user_id: str) -> User:
        """
        Follows a user.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the user to follow.

        Returns
        -------
        :class:`User`
            The followed user.

        Examples
        --------
        >>> user_id = '...'
        >>> await client.follow_user(user_id)

        See Also
        --------
        .unfollow_user
        """
        response, _ = await self.v11.create_friendships(user_id)
        return User(self, build_user_data(response))

    async def unfollow_user(self, user_id: str) -> User:
        """
        Unfollows a user.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the user to unfollow.

        Returns
        -------
        :class:`User`
            The unfollowed user.

        Examples
        --------
        >>> user_id = '...'
        >>> await client.unfollow_user(user_id)

        See Also
        --------
        .follow_user
        """
        response, _ = await self.v11.destroy_friendships(user_id)
        return User(self, build_user_data(response))

    async def block_user(self, user_id: str) -> User:
        """
        Blocks a user.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the user to block.

        Returns
        -------
        :class:`User`
            The blocked user.

        See Also
        --------
        .unblock_user
        """
        response, _ = await self.v11.create_blocks(user_id)
        return User(self, build_user_data(response))

    async def unblock_user(self, user_id: str) -> User:
        """
        Unblocks a user.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the user to unblock.

        Returns
        -------
        :class:`User`
            The unblocked user.

        See Also
        --------
        .block_user
        """
        response, _ = await self.v11.destroy_blocks(user_id)
        return User(self, build_user_data(response))

    async def mute_user(self, user_id: str) -> User:
        """
        Mutes a user.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the user to mute.

        Returns
        -------
        :class:`User`
            The muted user.

        See Also
        --------
        .unmute_user
        """
        response, _ = await self.v11.create_mutes(user_id)
        return User(self, build_user_data(response))

    async def unmute_user(self, user_id: str) -> User:
        """
        Unmutes a user.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the user to unmute.

        Returns
        -------
        :class:`User`
            The unmuted user.

        See Also
        --------
        .mute_user
        """
        response, _ = await self.v11.destroy_mutes(user_id)
        return User(self, build_user_data(response))

    async def get_trends(
        self,
        category: Literal['trending', 'for-you', 'news', 'sports', 'entertainment'],
        count: int = 20,
        retry: bool | int = True,
        additional_request_params: dict | None = None
    ) -> list[Trend]:
        """
        Retrieves trending topics on Twitter.

        Parameters
        ----------
        category : {'trending', 'for-you', 'news', 'sports', 'entertainment'}
            The category of trends to retrieve. Valid options include:
            - 'trending': General trending topics.
            - 'for-you': Trends personalized for the user.
            - 'news': News-related trends.
            - 'sports': Sports-related trends.
            - 'entertainment': Entertainment-related trends.
        count : :class:`int`, default=20
            The number of trends to retrieve.
        retry : :class:`bool` | :class:`int`, default=True
            If no trends are fetched continuously retry to fetch trends.
        additional_request_params : :class:`dict`, default=None
            Parameters to be added on top of the existing trends API
            parameters. Typically, it is used as `additional_request_params =
            {'candidate_source': 'trends'}` when this function doesn't work
            otherwise.

        Returns
        -------
        list[:class:`Trend`]
            A list of Trend objects representing the retrieved trends.

        Examples
        --------
        >>> trends = await client.get_trends('trending')
        >>> for trend in trends:
        ...     print(trend)
        <Trend name="...">
        <Trend name="...">
        ...
        """
        category = category.lower()
        timeline_id = TIMELINE_IDS.get(category)
        if timeline_id is None:
            return []

        response, _ = await self.gql.generic_timeline_by_id(
            timeline_id, count, additional_request_params
        )
        # The News / Sports / Entertainment tabs no longer put trends at the
        # top level: X wraps them in a `stories-*` module, so filtering on the
        # `trend-` prefix alone found nothing and those categories always came
        # back empty. Collect both shapes.
        item_contents = []
        for entry in first_dict(response, 'entries', []):
            entry_id = entry.get('entryId', '')
            content = entry.get('content') or {}
            if entry_id.startswith('trend'):
                item_contents.append(content.get('itemContent'))
            elif entry_id.startswith('stories'):
                for item in content.get('items') or []:
                    item_contents.append(
                        (item.get('item') or {}).get('itemContent')
                    )
        entries = [
            i for i in item_contents
            if i and i.get('itemType') == 'TimelineTrend'
        ]
        if not entries:
            if not retry:
                return []
            # Retrying passed `retry` through unchanged, so a category that
            # never yields trends recursed until X rate-limited the account -
            # a single call could burn hundreds of requests, which is what
            # made this look like "get_trends never returns". Count down.
            attempts_left = (retry - 1) if isinstance(retry, int) and retry is not True else 2
            if attempts_left <= 0:
                return []
            # A Twitter hiccup can drop the trend entries; give it a couple of
            # tries, then accept that the category has nothing to show.
            return await self.get_trends(
                category, count, attempts_left, additional_request_params
            )

        # Trends have no cursor, so honouring `count` here is a plain trim -
        # X hands back 30 regardless of what was requested.
        trends = [Trend(self, item_content) for item_content in entries]
        return trends[:count] if count and count > 0 else trends

    async def get_explore_page(self) -> list[Trend]:
        """
        Retrieves the trends shown on the Explore page.

        Returns
        -------
        list[:class:`Trend`]
            A list of Trend objects from the Explore page.

        Examples
        --------
        >>> trends = await client.get_explore_page()
        >>> for trend in trends:
        ...     print(trend)
        <Trend name="...">
        """
        response, _ = await self.gql.explore_page()
        entries = first_dict(response, 'entries', [])
        results = []
        for entry in entries:
            item_content = entry['content'].get('itemContent')
            if not item_content or item_content.get('itemType') != 'TimelineTrend':
                continue
            results.append(Trend(self, item_content))
        return results

    async def get_available_locations(self) -> list[Location]:
        """
        Retrieves locations where trends can be retrieved.

        Returns
        -------
        list[:class:`.Location`]
        """
        response, _ = await self.v11.available_trends()
        return [Location(self, data) for data in response]

    async def get_place_trends(self, woeid: int) -> PlaceTrends:
        """
        Retrieves the top 50 trending topics for a specific id.
        You can get available woeid using
        :attr:`.Client.get_available_locations`.
        """
        response, _ = await self.v11.place_trends(woeid)
        if not response:
            raise NotFound('No trends available for that location.')
        trend_data = response[0]
        trends = [PlaceTrend(self, data) for data in trend_data['trends']]
        trend_data['trends'] = trends
        return trend_data

    async def _get_user_friendship(
        self,
        user_id: str,
        count: int,
        f,
        cursor: str | None
    ) -> Result[User]:
        """
        Base function to get friendship.
        """
        response, _ = await f(user_id, count, cursor)

        # A protected (or suspended/deactivated) account answers with a bare
        # UserUnavailable and no timeline. Returning an empty Result made that
        # indistinguishable from an account that follows nobody, which is the
        # complaint behind d60/twikit#154.
        user_result = subobject(
            subobject(response.get('data') or {}, 'user'), 'result'
        )
        if user_result.get('__typename') == 'UserUnavailable':
            raise UserUnavailable(
                user_result.get('message') or
                'The account is protected, suspended or deactivated.'
            )

        # X omits the bottom cursor on the last page, which left this
        # unbound and raised UnboundLocalError instead of ending the walk.
        next_cursor = None
        items_ = find_dict(response, 'entries', find_one=True)
        if not items_:
            return Result.empty()
        items = items_[0]
        results = []
        for item in items:
            entry_id = item['entryId']
            if entry_id.startswith('user'):
                user_info = find_dict(item, 'result', find_one=True)
                if not user_info:
                    warnings.warn(
                        'Some followers are excluded because '
                        '"Quality Filter" is enabled. To get all followers, '
                        'turn off it in the Twitter settings.'
                    )
                    continue
                if user_info[0].get('__typename') == 'UserUnavailable':
                    continue
                results.append(User(self, user_info[0]))
            elif entry_id.startswith('cursor-bottom'):
                next_cursor = item['content']['value']

        # X ignores `count` here the same way it does on timelines - it kept
        # handing back 70 users no matter what was asked for. Trim client-side
        # and keep the surplus for the next page instead of dropping it.
        results, overflow = limited(results, count)

        return Result(
            results,
            partial(self._get_user_friendship, user_id, count, f, next_cursor) if next_cursor else None,
            next_cursor,
            overflow=overflow,
            page_size=count
        )

    async def _get_user_friendship_2(
        self, user_id: str, screen_name: str,
        count: int, f, cursor: str
    ) -> Result[User]:
        response, _ = await f(user_id, screen_name, count, cursor)
        users = response['users']
        results = []
        for user in users:
            results.append(User(self, build_user_data(user)))

        previous_cursor = response['previous_cursor']
        next_cursor = response['next_cursor']

        results, overflow = limited(results, count)

        return Result(
            results,
            partial(self._get_user_friendship_2, user_id, screen_name, count, f, next_cursor) if next_cursor else None,
            next_cursor,
            partial(self._get_user_friendship_2, user_id, screen_name, count, f, previous_cursor) if previous_cursor else None,
            previous_cursor,
            overflow=overflow,
            page_size=count
        )

    async def get_muted_users(
        self, count: int = 20, cursor: str | None = None
    ) -> Result[User]:
        """
        Retrieves the accounts the logged in user has muted.

        Parameters
        ----------
        count : :class:`int`, default=20
            The number of users to retrieve.
        cursor : :class:`str`, default=None
            A cursor for pagination.

        Returns
        -------
        Result[:class:`User`]
            The muted accounts.

        Examples
        --------
        >>> for user in await client.get_muted_users():
        ...     print(user.screen_name)

        See Also
        --------
        .mute_user
        .unmute_user
        """
        return await self._get_user_friendship(
            None, count, lambda _, c, cur: self.gql.muted_accounts(c, cur),
            cursor
        )

    async def get_blocked_users(
        self, count: int = 20, cursor: str | None = None
    ) -> Result[User]:
        """
        Retrieves the accounts the logged in user has blocked.

        Parameters
        ----------
        count : :class:`int`, default=20
            The number of users to retrieve.
        cursor : :class:`str`, default=None
            A cursor for pagination.

        Returns
        -------
        Result[:class:`User`]
            The blocked accounts.

        Examples
        --------
        >>> for user in await client.get_blocked_users():
        ...     print(user.screen_name)

        See Also
        --------
        .block_user
        .unblock_user
        """
        return await self._get_user_friendship(
            None, count, lambda _, c, cur: self.gql.blocked_accounts(c, cur),
            cursor
        )

    async def get_user_lists(
        self, user_id: str, count: int = 100, cursor: str | None = None
    ) -> Result[List]:
        """
        Retrieves the lists another user owns or subscribes to.

        :func:`get_lists` only ever reaches the logged in account; this reads
        anybody's public lists.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the user.
        count : :class:`int`, default=100
            The number of lists to retrieve.
        cursor : :class:`str`, default=None
            A cursor for pagination.

        Returns
        -------
        Result[:class:`List`]
            The user's lists.

        Examples
        --------
        >>> user = await client.get_user_by_screen_name('elonmusk')
        >>> for lst in await client.get_user_lists(user.id):
        ...     print(lst.name)

        See Also
        --------
        .get_lists
        """
        response, _ = await self.gql.combined_lists(user_id, count, cursor)

        entries_ = find_dict(response, 'entries', find_one=True)
        if not entries_:
            return Result([])
        entries = entries_[0]

        lists = []
        next_cursor = None
        for entry in entries:
            entry_id = entry.get('entryId', '')
            if entry_id.startswith('cursor-bottom'):
                next_cursor = entry.get('content', {}).get('value')
                continue
            list_data = find_dict(entry, 'list', find_one=True)
            if list_data:
                try:
                    lists.append(List(self, list_data[0]))
                except NotFound:
                    # An entry X did not resolve should cost that entry, not
                    # the rest of the page.
                    continue

        # X treats `count` as a hint on this endpoint too; trim client-side
        # and hand the surplus back through next() instead of dropping it.
        results, overflow = limited(lists, count)

        return Result(
            results,
            partial(self.get_user_lists, user_id, count, next_cursor) if next_cursor else None,
            next_cursor,
            overflow=overflow,
            page_size=count
        )

    async def get_user_followers(
        self, user_id: str, count: int = 20, cursor: str | None = None
    ) -> Result[User]:
        """
        Retrieves a list of followers for a given user.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the user for whom to retrieve followers.
        count : int, default=20
            The number of followers to retrieve.

        Returns
        -------
        Result[:class:`User`]
            A list of User objects representing the followers.
        """
        return await self._get_user_friendship(
            user_id, count, self.gql.followers, cursor
        )

    async def get_latest_followers(
        self, user_id: str | None = None, screen_name: str | None = None,
        count: int = 200, cursor: str | None = None
    ) -> Result[User]:
        """
        Retrieves the latest followers.
        Max count : 200
        """
        return await self._get_user_friendship_2(
            user_id, screen_name, count, self.v11.followers_list, cursor
        )

    async def get_latest_friends(
        self, user_id: str | None = None, screen_name: str | None = None,
        count: int = 200, cursor: str | None = None
    ) -> Result[User]:
        """
        Retrieves the latest friends (following users).
        Max count : 200
        """
        # X retired the v1.1 friends/list endpoint (now 404s); use the GraphQL
        # Following endpoint instead, resolving screen_name to an id when needed.
        if user_id is None:
            if screen_name is None:
                raise ValueError('user_id or screen_name is required')
            user_id = (await self.get_user_by_screen_name(screen_name)).id
        return await self._get_user_friendship(
            user_id, count, self.gql.following, cursor
        )

    async def get_user_verified_followers(
        self, user_id: str, count: int = 20, cursor: str | None = None
    ) -> Result[User]:
        """
        Retrieves a list of verified followers for a given user.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the user for whom to retrieve verified followers.
        count : :class:`int`, default=20
            The number of verified followers to retrieve.

        Returns
        -------
        Result[:class:`User`]
            A list of User objects representing the verified followers.
        """
        return await self._get_user_friendship(
            user_id, count, self.gql.blue_verified_followers, cursor
        )

    async def get_user_followers_you_know(
        self, user_id: str, count: int = 20, cursor: str | None = None
    ) -> Result[User]:
        """
        Retrieves a list of common followers.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the user for whom to retrieve followers you might know.
        count : :class:`int`, default=20
            The number of followers you might know to retrieve.

        Returns
        -------
        Result[:class:`User`]
            A list of User objects representing the followers you might know.
        """
        return await self._get_user_friendship(
            user_id, count, self.gql.followers_you_know, cursor
        )

    async def get_user_following(
        self, user_id: str, count: int = 20, cursor: str | None = None
    ) -> Result[User]:
        """
        Retrieves a list of users whom the given user is following.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the user for whom to retrieve the following users.
        count : :class:`int`, default=20
            The number of following users to retrieve.

        Returns
        -------
        Result[:class:`User`]
            A list of User objects representing the users being followed.
        """
        return await self._get_user_friendship(
            user_id, count, self.gql.following, cursor
        )

    async def get_user_subscriptions(
        self, user_id: str, count: int = 20, cursor: str | None = None
    ) -> Result[User]:
        """
        Retrieves a list of users to which the specified user is subscribed.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the user for whom to retrieve subscriptions.
        count : :class:`int`, default=20
            The number of subscriptions to retrieve.

        Returns
        -------
        Result[:class:`User`]
            A list of User objects representing the subscribed users.
        """
        return await self._get_user_friendship(
            user_id, count, self.gql.user_creator_subscriptions, cursor
        )

    async def _get_friendship_ids(
        self,
        user_id: str | None,
        screen_name: str | None,
        count: int,
        f,
        cursor: str | None
    ) -> Result[int]:
        response, _ = await f(user_id, screen_name, count, cursor)
        previous_cursor = response['previous_cursor']
        next_cursor = response['next_cursor']

        ids, overflow = limited(response['ids'], count)
        return Result(
            ids,
            partial(self._get_friendship_ids, user_id, screen_name, count, f, next_cursor) if next_cursor else None,
            next_cursor,
            partial(self._get_friendship_ids, user_id, screen_name, count, f, previous_cursor) if previous_cursor else None,
            previous_cursor,
            overflow=overflow,
            page_size=count
        )

    async def get_followers_ids(
        self,
        user_id: str | None = None,
        screen_name: str | None = None,
        count: int = 5000,
        cursor: str | None = None
    ) -> Result[int]:
        """
        Fetches the IDs of the followers of a specified user.

        Parameters
        ----------
        user_id : :class:`str` | None, default=None
            The ID of the user for whom to return results.
        screen_name : :class:`str` | None, default=None
            The screen name of the user for whom to return results.
        count : :class:`int`, default=5000
            The maximum number of IDs to retrieve.

        Returns
        -------
        :class:`Result`[:class:`int`]
            A Result object containing the IDs of the followers.
        """
        return await self._get_friendship_ids(user_id, screen_name, count, self.v11.followers_ids, cursor)

    async def get_friends_ids(
        self,
        user_id: str | None = None,
        screen_name: str | None = None,
        count: int = 5000,
        cursor: str | None = None
    ) -> Result[int]:
        """
        Fetches the IDs of the friends (following users) of a specified user.

        Parameters
        ----------
        user_id : :class:`str` | None, default=None
            The ID of the user for whom to return results.
        screen_name : :class:`str` | None, default=None
            The screen name of the user for whom to return results.
        count : :class:`int`, default=5000
            The maximum number of IDs to retrieve.

        Returns
        -------
        :class:`Result`[:class:`int`]
            A Result object containing the IDs of the friends.
        """
        return await self._get_friendship_ids(
            user_id, screen_name, count, self.v11.friends_ids, cursor
        )

    async def _send_dm(
        self,
        conversation_id: str,
        text: str,
        media_id: str | None,
        reply_to: str | None
    ) -> dict:
        """
        Base function to send dm.
        """
        response, _ = await self.v11.dm_new(conversation_id, text, media_id, reply_to)
        return response

    async def _get_dm_history(
        self,
        conversation_id: str,
        max_id: str | None = None
    ) -> dict:
        """
        Base function to get dm history.
        """
        response, _ = await self.v11.dm_conversation(conversation_id, max_id)
        return response

    async def send_dm(
        self,
        user_id: str,
        text: str,
        media_id: str | None = None,
        reply_to: str | None = None
    ) -> Message:
        """
        Send a direct message to a user.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the user to whom the direct message will be sent.
        text : :class:`str`
            The text content of the direct message.
        media_id : :class:`str`, default=None
            The media ID associated with any media content
            to be included in the message.
            Media ID can be received by using the :func:`.upload_media` method.
        reply_to : :class:`str`, default=None
            Message ID to reply to.

        Returns
        -------
        :class:`Message`
            `Message` object containing information about the message sent.

        Examples
        --------
        >>> # send DM with media
        >>> user_id = '000000000'
        >>> media_id = await client.upload_media('image.png')
        >>> message = await client.send_dm(user_id, 'text', media_id)
        >>> print(message)
        <Message id='...'>

        See Also
        --------
        .upload_media
        .delete_dm
        """
        response = await self._send_dm(
            f'{user_id}-{await self.user_id()}', text, media_id, reply_to
        )

        message_data = first_dict(response, 'message_data')
        if message_data is None:
            raise TwitterException(
                'X accepted the request but returned no message.'
            )
        users = list(response['users'].values())
        # The sender used to be read off dictionary order, which X does not
        # guarantee - a flipped pair made message.reply() answer yourself.
        sender_id = message_data.get('sender_id') or users[0]['id_str']
        recipient_id = message_data.get('recipient_id') or (
            users[1]['id_str'] if len(users) == 2 else users[0]['id_str']
        )
        return Message(
            self,
            message_data,
            sender_id,
            recipient_id
        )

    async def add_reaction_to_message(
        self, message_id: str, conversation_id: str, emoji: str
    ) -> Response:
        """
        Adds a reaction emoji to a specific message in a conversation.

        Parameters
        ----------
        message_id : :class:`str`
            The ID of the message to which the reaction emoji will be added.
            Group ID ('00000000') or partner_ID-your_ID ('00000000-00000001')
        conversation_id : :class:`str`
            The ID of the conversation containing the message.
        emoji : :class:`str`
            The emoji to be added as a reaction.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        Examples
        --------
        >>> message_id = '00000000'
        >>> conversation_id = f'00000001-{await client.user_id()}'
        >>> await client.add_reaction_to_message(
        ...    message_id, conversation_id, 'Emoji here'
        ... )
        """
        _, response = await self.gql.user_dm_reaction_mutation_add_mutation(
            message_id, conversation_id, emoji
        )
        return response

    async def remove_reaction_from_message(
        self, message_id: str, conversation_id: str, emoji: str
    ) -> Response:
        """
        Remove a reaction from a message.

        Parameters
        ----------
        message_id : :class:`str`
            The ID of the message from which to remove the reaction.
        conversation_id : :class:`str`
            The ID of the conversation where the message is located.
            Group ID ('00000000') or partner_ID-your_ID ('00000000-00000001')
        emoji : :class:`str`
            The emoji to remove as a reaction.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        Examples
        --------
        >>> message_id = '00000000'
        >>> conversation_id = f'00000001-{await client.user_id()}'
        >>> await client.remove_reaction_from_message(
        ...    message_id, conversation_id, 'Emoji here'
        ... )
        """
        _, response = await self.gql.user_dm_reaction_mutation_remove_mutation(
            message_id, conversation_id, emoji
        )
        return response

    async def delete_dm(self, message_id: str) -> Response:
        """
        Deletes a direct message with the specified message ID.

        Parameters
        ----------
        message_id : :class:`str`
            The ID of the direct message to be deleted.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        Examples
        --------
        >>> await client.delete_dm('0000000000')
        """
        _, response = await self.gql.dm_message_delete_mutation(message_id)
        return response

    async def get_dm_history(
        self,
        user_id: str,
        max_id: str | None = None
    ) -> Result[Message]:
        """
        Retrieves the DM conversation history with a specific user.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the user with whom the DM conversation
            history will be retrieved.
        max_id : :class:`str`, default=None
            If specified, retrieves messages older than the specified max_id.

        Returns
        -------
        Result[:class:`Message`]
            A Result object containing a list of Message objects representing
            the DM conversation history.

        Examples
        --------
        >>> messages = await client.get_dm_history('0000000000')
        >>> for message in messages:
        >>>     print(message)
        <Message id="...">
        <Message id="...">
        ...
        ...

        >>> more_messages = await messages.next()  # Retrieve more messages
        >>> for message in more_messages:
        >>>     print(message)
        <Message id="...">
        <Message id="...">
        ...
        ...
        """
        response = await self._get_dm_history(
            f'{user_id}-{await self.user_id()}', max_id
        )

        if 'entries' not in response['conversation_timeline']:
            return Result([])
        items = response['conversation_timeline']['entries']

        messages = []
        for item in items:
            # A conversation timeline also carries non-message entries such as
            # `trust_conversation`, which have no `message` key at all.
            if 'message' not in item:
                continue
            message_info = item['message']['message_data']
            messages.append(Message(
                self,
                message_info,
                message_info['sender_id'],
                message_info.get('recipient_id')
            ))

        if not messages:
            return Result([])

        return Result(
            messages,
            partial(self.get_dm_history, user_id, messages[-1].id),
            messages[-1].id
        )

    async def get_dm_inbox(
        self, cursor: str | None = None
    ) -> Result[Conversation]:
        """
        Retrieves the direct message inbox - the list of conversations,
        not their contents.

        Parameters
        ----------
        cursor : :class:`str`, default=None
            A cursor for pagination.

        Returns
        -------
        Result[:class:`Conversation`]
            The conversations in the inbox.

        Examples
        --------
        >>> conversations = await client.get_dm_inbox()
        >>> for conversation in conversations:
        ...     print(conversation.id, conversation.participant_ids)
        ...     messages = await conversation.get_history()

        See Also
        --------
        .get_dm_history
        """
        if cursor is None:
            response, _ = await self.v11.dm_inbox(None)
        else:
            # inbox_initial_state only ever serves the first page - passing it
            # a cursor returns the identical conversations, so walking the
            # inbox that way loops forever. Measured against a four-entry
            # inbox: page one and "page two" came back with the same four ids.
            response, _ = await self.v11.dm_inbox_timeline('trusted', cursor)
        state = response.get('inbox_initial_state') or response.get('user_events') or {}

        conversations = state.get('conversations') or {}
        my_id = await self.user_id()
        results = [
            Conversation(self, data, my_id)
            for data in conversations.values()
        ]
        # X sorts the inbox by recency; conversations arrives as a mapping so
        # that order is not guaranteed to survive. Sort it back explicitly.
        #
        # `sort_timestamp` is the recency X itself orders by. Sorting on
        # `last_read_event_id` instead ranked by how much the *reader* had
        # caught up, so a conversation with unread messages - the one that
        # belongs at the top - sank to the bottom. The ids are snowflakes but
        # nothing guarantees they parse, and one odd value must not take the
        # whole inbox down.
        def _recency(conversation: Conversation) -> int:
            for key in ('sort_timestamp', 'sort_event_id'):
                value = conversation._data.get(key)
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
            return 0

        results.sort(key=_recency, reverse=True)

        # X hands back a cursor on every inbox response, including the last
        # one, so treating it as "there is more" made next() re-fetch the same
        # page forever. The end of the inbox is announced by the timelines
        # instead.
        timelines = state.get('inbox_timelines') or {}
        trusted = timelines.get('trusted') or {}
        at_end = (
            trusted.get('status') == 'AT_END' if trusted
            else not timelines
        )

        # The next page is asked for by max_id, which is the oldest entry of
        # this one - the top-level `cursor` is not a paging token and X sends
        # it even on the last response.
        next_cursor = None if at_end else (
            trusted.get('min_entry_id') or state.get('min_entry_id')
        )
        return Result(
            results,
            partial(self.get_dm_inbox, next_cursor) if next_cursor else None,
            next_cursor
        )

    async def create_group(
        self,
        user_ids: list[str],
        text: str,
        media_id: str | None = None
    ) -> Group:
        """
        Creates a group conversation by sending its first message.

        X has no separate "create group" call - a DM addressed to more than
        one recipient becomes a group. With a single recipient it is just an
        ordinary one-to-one conversation, so pass at least two ids.

        Parameters
        ----------
        user_ids : list[:class:`str`]
            IDs of the users to put in the group.
        text : :class:`str`
            The first message.
        media_id : :class:`str`, default=None
            Media to attach to the first message.

        Returns
        -------
        :class:`Group`
            The group that was created.

        Examples
        --------
        >>> group = await client.create_group(
        ...     ['0000000', '1111111'], 'Hello'
        ... )
        >>> await group.add_members(['2222222'])

        See Also
        --------
        .send_dm_to_group
        .get_group
        """
        if not user_ids:
            raise ValueError('`user_ids` must not be empty.')

        response, _ = await self.v11.dm_new_group(user_ids, text, media_id)

        conversation_id = None
        entries = (response.get('entries') or [])
        for entry in entries:
            message = entry.get('message')
            if message:
                conversation_id = message.get('conversation_id')
                break
        if conversation_id is None:
            conversation_id = next(
                iter(response.get('conversations') or {}), None
            )
        if conversation_id is None:
            raise TwitterException(
                'X did not return a conversation for the new group.'
            )

        return await self.get_group(conversation_id)

    async def delete_dm_conversation(self, conversation_id: str) -> Response:
        """
        Deletes a conversation from the logged in account's inbox.

        This only clears it for the caller - the other participants keep
        their copy, which is how X itself behaves.

        Parameters
        ----------
        conversation_id : :class:`str`
            The ID of the conversation, as found on
            :attr:`Conversation.id`.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        See Also
        --------
        .get_dm_inbox
        """
        _, response = await self.v11.delete_conversation(conversation_id)
        return response

    async def send_dm_to_group(
        self,
        group_id: str,
        text: str,
        media_id: str | None = None,
        reply_to: str | None = None
    ) -> GroupMessage:
        """
        Sends a message to a group.

        Parameters
        ----------
        group_id : :class:`str`
            The ID of the group in which the direct message will be sent.
        text : :class:`str`
            The text content of the direct message.
        media_id : :class:`str`, default=None
            The media ID associated with any media content
            to be included in the message.
            Media ID can be received by using the :func:`.upload_media` method.
        reply_to : :class:`str`, default=None
            Message ID to reply to.

        Returns
        -------
        :class:`GroupMessage`
            `GroupMessage` object containing information about
            the message sent.

        Examples
        --------
        >>> # send DM with media
        >>> group_id = '000000000'
        >>> media_id = await client.upload_media('image.png')
        >>> message = await client.send_dm_to_group(group_id, 'text', media_id)
        >>> print(message)
        <GroupMessage id='...'>

        See Also
        --------
        .upload_media
        .delete_dm
        """
        response = await self._send_dm(group_id, text, media_id, reply_to)

        message_data = first_dict(response, 'message_data')
        if message_data is None:
            raise TwitterException(
                'X accepted the request but returned no message.'
            )
        users = list(response['users'].values())
        return GroupMessage(
            self,
            message_data,
            message_data.get('sender_id') or users[0]['id_str'],
            group_id
        )

    async def get_group_dm_history(
        self,
        group_id: str,
        max_id: str | None = None
    ) -> Result[GroupMessage]:
        """
        Retrieves the DM conversation history in a group.

        Parameters
        ----------
        group_id : :class:`str`
            The ID of the group in which the DM conversation
            history will be retrieved.
        max_id : :class:`str`, default=None
            If specified, retrieves messages older than the specified max_id.

        Returns
        -------
        Result[:class:`GroupMessage`]
            A Result object containing a list of GroupMessage objects
            representing the DM conversation history.

        Examples
        --------
        >>> messages = await client.get_group_dm_history('0000000000')
        >>> for message in messages:
        >>>     print(message)
        <GroupMessage id="...">
        <GroupMessage id="...">
        ...
        ...

        >>> more_messages = await messages.next()  # Retrieve more messages
        >>> for message in more_messages:
        >>>     print(message)
        <GroupMessage id="...">
        <GroupMessage id="...">
        ...
        ...
        """
        response = await self._get_dm_history(group_id, max_id)
        if 'entries' not in response['conversation_timeline']:
            return Result([])

        items = response['conversation_timeline']['entries']
        messages = []
        for item in items:
            if 'message' not in item:
                continue
            message_info = item['message']['message_data']
            messages.append(GroupMessage(
                self,
                message_info,
                message_info['sender_id'],
                group_id
            ))

        if not messages:
            return Result([])

        return Result(
            messages,
            partial(self.get_group_dm_history, group_id, messages[-1].id),
            messages[-1].id
        )

    async def get_group(self, group_id: str) -> Group:
        """
        Fetches a guild by ID.

        Parameters
        ----------
        group_id : :class:`str`
            The ID of the group to retrieve information for.

        Returns
        -------
        :class:`Group`
            An object representing the retrieved group.
        """
        response = await self._get_dm_history(group_id)
        return Group(self, group_id, response)

    async def add_members_to_group(
        self, group_id: str, user_ids: list[str]
    ) -> Response:
        """Adds members to a group.

        Parameters
        ----------
        group_id : :class:`str`
            ID of the group to which the member is to be added.
        user_ids : list[:class:`str`]
            List of IDs of users to be added.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        Examples
        --------
        >>> group_id = '...'
        >>> members = ['...']
        >>> await client.add_members_to_group(group_id, members)
        """
        _, response = await self.gql.add_participants_mutation(group_id, user_ids)
        return response

    async def change_group_name(self, group_id: str, name: str) -> Response:
        """Changes group name

        Parameters
        ----------
        group_id : :class:`str`
            ID of the group to be renamed.
        name : :class:`str`
            New name.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.
        """
        _, response = await self.v11.conversation_update_name(group_id, name)
        return response

    async def create_list(
        self, name: str, description: str = '', is_private: bool = False
    ) -> List:
        """
        Creates a list.

        Parameters
        ----------
        name : :class:`str`
            The name of the list.
        description : :class:`str`, default=''
            The description of the list.
        is_private : :class:`bool`, default=False
            Indicates whether the list is private (True) or public (False).

        Returns
        -------
        :class:`List`
            The created list.

        Examples
        --------
        >>> list = await client.create_list(
        ...     'list name',
        ...     'list description',
        ...     is_private=True
        ... )
        >>> print(list)
        <List id="...">
        """
        response, _ = await self.gql.create_list(name, description, is_private)
        list_info = first_dict(response, 'list')
        if list_info is None:
            raise NotFound('The list does not exist.')
        return List(self, list_info)

    async def delete_list(self, list_id: str) -> Response:
        """
        Deletes a list.

        Parameters
        ----------
        list_id : :class:`str`
            The ID of the list to delete.

        Examples
        --------
        >>> await client.delete_list('list id')
        """
        _, response = await self.gql.delete_list(list_id)
        return response

    async def edit_list_banner(self, list_id: str, media_id: str) -> Response:
        """
        Edit the banner image of a list.

        Parameters
        ----------
        list_id : :class:`str`
            The ID of the list.
        media_id : :class:`str`
            The ID of the media to use as the new banner image.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        Examples
        --------
        >>> list_id = '...'
        >>> media_id = await client.upload_media('image.png')
        >>> await client.edit_list_banner(list_id, media_id)
        """
        _, response = await self.gql.edit_list_banner(list_id, media_id)
        return response

    async def delete_list_banner(self, list_id: str) -> Response:
        """Deletes list banner.

        Parameters
        ----------
        list_id : :class:`str`
            ID of the list from which the banner is to be removed.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.
        """
        _, response = await self.gql.delete_list_banner(list_id)
        return response

    async def edit_list(
        self,
        list_id: str,
        name: str | None = None,
        description: str | None = None,
        is_private: bool | None = None
    ) -> List:
        """
        Edits list information.

        Parameters
        ----------
        list_id : :class:`str`
            The ID of the list to edit.
        name : :class:`str`, default=None
            The new name for the list.
        description : :class:`str`, default=None
            The new description for the list.
        is_private : :class:`bool`, default=None
            Indicates whether the list should be private
            (True) or public (False).

        Returns
        -------
        :class:`List`
            The updated Twitter list.

        Examples
        --------
        >>> await client.edit_list(
        ...     'new name', 'new description', True
        ... )
        """
        response, _ = await self.gql.update_list(list_id, name, description, is_private)
        list_info = first_dict(response, 'list')
        if list_info is None:
            raise NotFound('The list does not exist.')
        return List(self, list_info)

    async def add_list_member(self, list_id: str, user_id: str) -> List:
        """
        Adds a user to a list.

        Parameters
        ----------
        list_id : :class:`str`
            The ID of the list.
        user_id : :class:`str`
            The ID of the user to add to the list.

        Returns
        -------
        :class:`List`
            The updated Twitter list.

        Examples
        --------
        >>> await client.add_list_member('list id', 'user id')
        """
        response, _ = await self.gql.list_add_member(list_id, user_id)
        return List(self, response['data']['list'])

    async def remove_list_member(self, list_id: str, user_id: str) -> List:
        """
        Removes a user from a list.

        Parameters
        ----------
        list_id : :class:`str`
            The ID of the list.
        user_id : :class:`str`
            The ID of the user to remove from the list.

        Returns
        -------
        :class:`List`
            The updated Twitter list.

        Examples
        --------
        >>> await client.remove_list_member('list id', 'user id')
        """
        response, _ = await self.gql.list_remove_member(list_id, user_id)
        errors = fatal_errors(response, 'list')
        if errors:
            raise TwitterException(
                errors[0].get('message', 'Failed to remove the list member.')
            )
        return List(self, response['data']['list'])

    async def get_lists(
        self, count: int = 100, cursor: str = None
    ) -> Result[List]:
        """
        Retrieves a list of user lists.

        Parameters
        ----------
        count : :class:`int`
            The number of lists to retrieve.

        Returns
        -------
        Result[:class:`List`]
            Retrieved lists.

        Examples
        --------
        >>> lists = client.get_lists()
        >>> for list_ in lists:
        ...     print(list_)
        <List id="...">
        <List id="...">
        ...
        ...
        >>> more_lists = lists.next()  # Retrieve more lists
        """
        response, _ = await self.gql.list_management_pace_timeline(count, cursor)

        # X can answer with a viewer shell and an error instead of the
        # timeline - measured: code 214, "BadRequest:
        # com.twitter.strato.serialization.DecodeException". `data` is truthy
        # there, so the failure used to read as "you own no lists".
        errors = fatal_errors(response, 'entries')
        if errors:
            raise TwitterException(
                errors[0].get('message', 'Failed to retrieve the lists.')
            )

        entries_ = find_dict(response, 'entries', find_one=True)
        if not entries_:
            return Result([])
        entries = entries_[0]

        # The cursor is read before anything can bail out. A page that yields
        # no lists is not the end of the collection - X pads this module with
        # suggestion and empty-state cells - so an early return that dropped
        # the cursor ended the walk before the real lists further on.
        next_cursor = entries[-1].get('content', {}).get('value')

        lists = []
        items = find_dict(entries, 'items')
        for item in (items[1] if len(items) >= 2 else []):
            list_data = item.get('item', {}).get('itemContent', {}).get('list')
            if list_data is None:
                continue
            try:
                lists.append(List(self, list_data))
            except NotFound:
                # A cell can carry a `list` that X did not resolve; skip it
                # rather than losing the rest of the page with it.
                continue

        # The fetcher only goes out when there is a cursor to advance on:
        # pointing next() at a None cursor re-requests the first page, forever.
        results, overflow = limited(lists, count)

        return Result(
            results,
            partial(self.get_lists, count, next_cursor) if next_cursor else None,
            next_cursor,
            overflow=overflow,
            page_size=count
        )

    async def get_list(self, list_id: str) -> List:
        """
        Retrieve list by ID.

        Parameters
        ----------
        list_id : :class:`str`
            The ID of the list to retrieve.

        Returns
        -------
        :class:`List`
            List object.
        """
        response, _ = await self.gql.list_by_rest_id(list_id)
        list_data_ = find_dict(response, 'list', find_one=True)
        if not list_data_:
            raise ValueError(f'Invalid list id: {list_id}')
        return List(self, list_data_[0])

    async def get_list_tweets(
        self, list_id: str, count: int = 20, cursor: str | None = None
    ) -> Result[Tweet]:
        """
        Retrieves tweets from a list.

        Parameters
        ----------
        list_id : :class:`str`
            The ID of the list to retrieve tweets from.
        count : :class:`int`, default=20
            The number of tweets to retrieve.
        cursor : :class:`str`, default=None
            The cursor for pagination.

        Returns
        -------
        Result[:class:`Tweet`]
            A Result object containing the retrieved tweets.

        Examples
        --------
        >>> tweets = await client.get_list_tweets('list id')
        >>> for tweet in tweets:
        ...    print(tweet)
        <Tweet id="...">
        <Tweet id="...">
        ...
        ...

        >>> more_tweets = await tweets.next()  # Retrieve more tweets
        >>> for tweet in more_tweets:
        ...     print(tweet)
        <Tweet id="...">
        <Tweet id="...">
        ...
        ...
        """
        response, _ = await self.gql.list_latest_tweets_timeline(list_id, count, cursor)

        items_ = find_dict(response, 'entries', find_one=True)
        if not items_:
            raise ValueError(f'Invalid list id: {list_id}')
        items = items_[0]
        next_cursor = last_cursor(items)

        results = []

        def handle_item(item, conversation_ids=None):
            tweet = tweet_from_data(self, item)
            if tweet is not None:
                tweet.conversation_ids = conversation_ids
                results.append(tweet)

        for item in items:
            if item['entryId'].startswith('tweet'):
                handle_item(item)
            elif item['entryId'].startswith('list-conversation'):
                conversation_ids = _conversation_ids(item['content'])
                for sub_item in item['content']['items']:
                    handle_item(sub_item, conversation_ids)

        # X treats `count` as a hint on this endpoint too; trim client-side
        # and hand the surplus back through next() instead of dropping it.
        results, overflow = limited(results, count)

        return Result(
            results,
            partial(self.get_list_tweets, list_id, count, next_cursor) if next_cursor else None,
            next_cursor,
            overflow=overflow,
            page_size=count
        )

    async def _get_list_users(self, f: str, list_id: str, count: int, cursor: str) -> Result[User]:
        """
        Base function to retrieve the users associated with a list.
        """
        response, _ = await f(list_id, count, cursor)

        # X omits the bottom cursor on the last page, which left this
        # unbound and raised UnboundLocalError instead of ending the walk.
        next_cursor = None
        items = first_dict(response, 'entries', [])
        results = []
        for item in items:
            entry_id = item['entryId']
            if entry_id.startswith('user'):
                user_info = first_dict(item, 'result')
                if user_info is None:
                    # An entry X could not resolve (deleted or restricted
                    # user) arrives without `result`; skip it, do not die.
                    continue
                results.append(User(self, user_info))
            elif entry_id.startswith('cursor-bottom'):
                next_cursor = item['content']['value']
                break

        # X treats `count` as a hint on this endpoint too; trim client-side
        # and hand the surplus back through next() instead of dropping it.
        results, overflow = limited(results, count)

        return Result(
            results,
            partial(self._get_list_users, f, list_id, count, next_cursor) if next_cursor else None,
            next_cursor,
            overflow=overflow,
            page_size=count
        )

    async def get_list_members(
        self, list_id: str, count: int = 20, cursor: str | None = None
    ) -> Result[User]:
        """Retrieves members of a list.

        Parameters
        ----------
        list_id : :class:`str`
            List ID.
        count : int, default=20
            Number of members to retrieve.

        Returns
        -------
        Result[:class:`User`]
            Members of a list

        Examples
        --------
        >>> members = client.get_list_members(123456789)
        >>> for member in members:
        ...     print(member)
        <User id="...">
        <User id="...">
        ...
        ...
        >>> more_members = members.next()  # Retrieve more members
        """
        return await self._get_list_users(self.gql.list_members, list_id, count, cursor)

    async def get_list_subscribers(
        self, list_id: str, count: int = 20, cursor: str | None = None
    ) -> Result[User]:
        """Retrieves subscribers of a list.

        Parameters
        ----------
        list_id : :class:`str`
            List ID.
        count : :class:`int`, default=20
            Number of subscribers to retrieve.

        Returns
        -------
        Result[:class:`User`]
            Subscribers of a list

        Examples
        --------
        >>> members = client.get_list_subscribers(123456789)
        >>> for subscriber in subscribers:
        ...     print(subscriber)
        <User id="...">
        <User id="...">
        ...
        ...
        >>> more_subscribers = members.next()  # Retrieve more subscribers
        """
        return await self._get_list_users(self.gql.list_subscribers, list_id, count, cursor)

    async def search_list(
        self, query: str, count: int = 20, cursor: str | None = None
    ) -> Result[List]:
        """
        Search for lists based on the provided query.

        Parameters
        ----------
        query : :class:`str`
            The search query.
        count : :class:`int`, default=20
            The number of lists to retrieve.

        Returns
        -------
        Result[:class:`List`]
            An instance of the `Result` class containing the
            search results.

        Examples
        --------
        >>> lists = await client.search_list('query')
        >>> for list in lists:
        ...     print(list)
        <List id="...">
        <List id="...">
        ...

        >>> more_lists = await lists.next()  # Retrieve more lists
        """
        response, _ = await self.gql.search_timeline(query, 'Lists', count, cursor)
        entries = first_dict(response, 'entries', [])

        if cursor is None:
            items = (entries[0].get('content', {}).get('items') or []) if entries else []
        else:
            items = first_dict(response, 'moduleItems', [])

        lists = []
        for item in items:
            list_data = (
                item.get('item', {}).get('itemContent', {}).get('list')
            )
            if not list_data:
                continue
            try:
                lists.append(List(self, list_data))
            except NotFound:
                continue
        next_cursor = last_cursor(entries)

        lists, overflow = limited(lists, count)
        return Result(
            lists,
            partial(self.search_list, query, count, next_cursor) if next_cursor else None,
            next_cursor,
            overflow=overflow,
            page_size=count
        )

    async def get_notifications(
        self,
        type: Literal['All', 'Verified', 'Mentions'],
        count: int = 40,
        cursor: str | None = None
    ) -> Result[Notification]:
        """
        Retrieve notifications based on the provided type.

        Parameters
        ----------
        type : {'All', 'Verified', 'Mentions'}
            Type of notifications to retrieve.
            All: All notifications
            Verified: Notifications relating to authenticated users
            Mentions: Notifications with mentions
        count : :class:`int`, default=40
            Number of notifications to retrieve.

        Returns
        -------
        Result[:class:`Notification`]
            List of retrieved notifications.

        Examples
        --------
        >>> notifications = await client.get_notifications('All')
        >>> for notification in notifications:
        ...     print(notification)
        <Notification id="...">
        <Notification id="...">
        ...
        ...

        >>> # Retrieve more notifications
        >>> more_notifications = await notifications.next()
        """
        type = type.capitalize()
        endpoints = {
            'All': self.v11.notifications_all,
            'Verified': self.v11.notifications_verified,
            'Mentions': self.v11.notifications_mentions
        }
        if type not in endpoints:
            raise ValueError(
                f'Invalid type {type!r}; expected one of '
                f'{", ".join(endpoints)}.'
            )
        f = endpoints[type]
        response, _ = await f(count, cursor)

        # X omits the bottom cursor on the last page, which left this
        # unbound and raised UnboundLocalError instead of ending the walk.
        next_cursor = None
        global_objects = response['globalObjects']
        users = {
            id: User(self, build_user_data(data))
            for id, data in global_objects.get('users', {}).items()
        }
        tweets = {}

        for id, tweet_data in global_objects.get('tweets', {}).items():
            user_id = tweet_data['user_id_str']
            user = users[user_id]
            tweet = Tweet(self, build_tweet_data(tweet_data), user)
            tweets[id] = tweet

        notifications = []

        raw_notifications = global_objects.get('notifications')
        if raw_notifications is not None:
            for notification in raw_notifications.values():
                user_actions = notification['template']['aggregateUserActionsV1']
                target_objects = user_actions['targetObjects']
                if target_objects and 'tweet' in target_objects[0]:
                    tweet_id = target_objects[0]['tweet']['id']
                    tweet = tweets[tweet_id]
                else:
                    tweet = None

                from_users  = user_actions['fromUsers']
                if from_users and 'user' in from_users[0]:
                    user_id = from_users[0]['user']['id']
                    user = users[user_id]
                else:
                    user = None

                notifications.append(Notification(self, notification, tweet, user))
        elif type == 'Mentions':
            # The Mentions timeline omits the `notifications` key entirely:
            # the mention/reply tweets are referenced by `tweet-*` timeline
            # entries and stored in `globalObjects.tweets` alongside context
            # tweets. Only convert the entry tweets, not the whole object map.
            entries = first_dict(response, 'entries', [])
            seen_tweet_ids = set()
            for entry in entries:
                entry_id = entry.get('entryId', '')
                if not entry_id.startswith('tweet-'):
                    continue
                tweet_id = entry_id.removeprefix('tweet-')
                if tweet_id in seen_tweet_ids:
                    continue
                tweet = tweets.get(tweet_id)
                if tweet is None:
                    continue
                seen_tweet_ids.add(tweet_id)
                user = tweet.user
                # Tweet ids are snowflakes; recover the creation time without
                # inventing a timestamp or depending on locale-formatted text.
                timestamp_ms = (int(tweet.id) >> 22) + 1288834974657
                data = {
                    'id': tweet.id,
                    'timestampMs': str(timestamp_ms),
                    'icon': {},
                    'message': {'text': ''},
                }
                notifications.append(Notification(self, data, tweet, user))

        entries = first_dict(response, 'entries', [])
        cursor_bottom_entry = [
            i for i in entries
            if i['entryId'].startswith('cursor-bottom')
        ]
        if cursor_bottom_entry:
            next_cursor = first_dict(cursor_bottom_entry[0], 'value')
        else:
            next_cursor = None

        # X treats `count` as a hint on this endpoint too; trim client-side
        # and hand the surplus back through next() instead of dropping it.
        results, overflow = limited(notifications, count)

        return Result(
            results,
            partial(self.get_notifications, type, count, next_cursor) if next_cursor else None,
            next_cursor,
            overflow=overflow,
            page_size=count
        )

    async def search_community(
        self, query: str, cursor: str | None = None
    ) -> Result[Community]:
        """
        Searchs communities based on the specified query.

        Parameters
        ----------
        query : :class:`str`
            The search query.

        Returns
        -------
        Result[:class:`Community`]
            List of retrieved communities.

        Examples
        --------
        >>> communities = await client.search_communities('query')
        >>> for community in communities:
        ...     print(community)
        <Community id="...">
        <Community id="...">
        ...

        >>> # Retrieve more communities
        >>> more_communities = await communities.next()
        """
        response, _ = await self.gql.search_community(query, cursor)

        items = first_dict(response, 'items_results', [])
        communities = []
        for item in items:
            if 'result' not in item:
                continue
            try:
                communities.append(Community(self, item['result']))
            except NotFound:
                continue
        next_cursor_ = find_dict(response, 'next_cursor', find_one=True)
        next_cursor = next_cursor_[0] if next_cursor_ else None
        if next_cursor is None:
            fetch_next_result = None
        else:
            fetch_next_result = partial(self.search_community, query, next_cursor)
        return Result(
            communities,
            fetch_next_result,
            next_cursor
        )

    async def get_community(self, community_id: str) -> Community:
        """
        Retrieves community by ID.

        Parameters
        ----------
        list_id : :class:`str`
            The ID of the community to retrieve.

        Returns
        -------
        :class:`Community`
            Community object.
        """
        response, _ = await self.gql.community_query(community_id)
        community_data = first_dict(response, 'result')
        if community_data is None:
            raise NotFound('The community does not exist.')
        return Community(self, community_data)

    async def get_community_tweets(
        self,
        community_id: str,
        tweet_type: Literal['Top', 'Latest', 'Media'],
        count: int = 40,
        cursor: str | None = None
    ) -> Result[Tweet]:
        """
        Retrieves tweets from a community.

        Parameters
        ----------
        community_id : :class:`str`
            The ID of the community.
        tweet_type : {'Top', 'Latest', 'Media'}
            The type of tweets to retrieve.
        count : :class:`int`, default=40
            The number of tweets to retrieve.

        Returns
        -------
        Result[:class:`Tweet`]
            List of retrieved tweets.

        Examples
        --------
        >>> community_id = '...'
        >>> tweets = await client.get_community_tweets(community_id, 'Latest')
        >>> for tweet in tweets:
        ...     print(tweet)
        <Tweet id="...">
        <Tweet id="...">
        ...
        >>> more_tweets = await tweets.next()  # Retrieve more tweets
        """
        if tweet_type == 'Media':
            response, _ = await self.gql.community_media_timeline(community_id, count, cursor)
        elif tweet_type == 'Top':
            response, _ = await self.gql.community_tweets_timeline(community_id, 'Relevance', count, cursor)
        elif tweet_type == 'Latest':
            response, _ = await self.gql.community_tweets_timeline(community_id, 'Recency', count, cursor)
        else:
            raise ValueError(f'Invalid tweet_type: {tweet_type}')

        # X omits the bottom cursor on the last page, which left this
        # unbound and raised UnboundLocalError instead of ending the walk.
        next_cursor = None
        entries = first_dict(response, 'entries', [])
        if tweet_type == 'Media':
            if cursor is None:
                items = (entries[0].get('content', {}).get('items') or []) if entries else []
                next_cursor = last_cursor(entries)
                previous_cursor = cursor_at(entries, -2)
            else:
                items = first_dict(response, 'moduleItems', [])
                next_cursor = last_cursor(entries)
                previous_cursor = cursor_at(entries, -2)
        else:
            items = entries
            next_cursor = last_cursor(items)
            previous_cursor = cursor_at(items, -2)

        tweets = []
        for item in items:
            if not item['entryId'].startswith(('tweet', 'communities-grid')):
                continue

            tweet = tweet_from_data(self, item)
            if tweet is not None:
                tweets.append(tweet)

        # X treats `count` as a hint on this endpoint too; trim client-side
        # and hand the surplus back through next() instead of dropping it.
        results, overflow = limited(tweets, count)

        return Result(
            results,
            partial(self.get_community_tweets, community_id, tweet_type, count, next_cursor) if next_cursor else None,
            next_cursor,
            partial(self.get_community_tweets, community_id, tweet_type, count, previous_cursor) if previous_cursor else None,
            previous_cursor,
            overflow=overflow,
            page_size=count
        )

    async def get_communities_timeline(
        self, count: int = 20, cursor: str | None = None
    ) -> Result[Tweet]:
        """
        Retrieves tweets from communities timeline.

        Parameters
        ----------
        count : :class:`int`, default=20
            The number of tweets to retrieve.

        Returns
        -------
        Result[:class:`Tweet`]
            List of retrieved tweets.

        Examples
        --------
        >>> tweets = await client.get_communities_timeline()
        >>> for tweet in tweets:
        ...     print(tweet)
        <Tweet id="...">
        <Tweet id="...">
        ...
        >>> more_tweets = await tweets.next()  # Retrieve more tweets
        """
        response, _ = await self.gql.communities_main_page_timeline(count, cursor)
        items = first_dict(response, 'entries', [])
        tweets = []
        for item in items:
            if not item['entryId'].startswith('tweet'):
                continue
            tweet_data = first_dict(item, 'result')
            if tweet_data is None:
                continue
            if 'tweet' in tweet_data:
                tweet_data = tweet_data['tweet']
            # A promoted entry carries the advertiser's User object, whose
            # `core` has no user_results - and no community either.
            user_data = subobject(
                subobject(tweet_data, 'core'), 'user_results'
            ).get('result')
            community_data = subobject(
                tweet_data, 'community_results'
            ).get('result')
            if user_data is None or community_data is None:
                continue
            community_data['rest_id'] = community_data['id_str']
            community = Community(self, community_data)
            tweet = Tweet(self, tweet_data, User(self, user_data))
            tweet.community = community
            tweets.append(tweet)

        next_cursor = last_cursor(items)
        previous_cursor = cursor_at(items, -2)

        tweets, overflow = limited(tweets, count)
        return Result(
            tweets,
            partial(self.get_communities_timeline, count, next_cursor) if next_cursor else None,
            next_cursor,
            partial(self.get_communities_timeline, count, previous_cursor) if previous_cursor else None,
            previous_cursor,
            overflow=overflow,
            page_size=count
        )

    async def join_community(self, community_id: str) -> Community:
        """
        Join a community.

        Parameters
        ----------
        community_id : :class:`str`
            The ID of the community to join.

        Returns
        -------
        :class:`Community`
            The joined community.
        """
        response, _ = await self.gql.join_community(community_id)
        community_data = response['data']['community_join']
        community_data['rest_id'] = community_data['id_str']
        return Community(self, community_data)

    async def leave_community(self, community_id: str) -> Community:
        """
        Leave a community.

        Parameters
        ----------
        community_id : :class:`str`
            The ID of the community to leave.

        Returns
        -------
        :class:`Community`
            The left community.
        """
        response, _ = await self.gql.leave_community(community_id)
        community_data = response['data']['community_leave']
        community_data['rest_id'] = community_data['id_str']
        return Community(self, community_data)

    async def request_to_join_community(
        self, community_id: str, answer: str | None = None
    ) -> Community:
        """
        Request to join a community.

        Parameters
        ----------
        community_id : :class:`str`
            The ID of the community to request to join.
        answer : :class:`str`, default=None
            The answer to the join request.

        Returns
        -------
        :class:`Community`
            The requested community.
        """
        response, _ = await self.gql.request_to_join_community(community_id, answer)
        community_data = first_dict(response, 'result')
        if community_data is None:
            raise NotFound('The community does not exist.')
        community_data['rest_id'] = community_data['id_str']
        return Community(self, community_data)

    async def _get_community_users(self, f, community_id: str, count: int, cursor: str | None):
        """
        Base function to retrieve community users.
        """
        response, _ = await f(community_id, count, cursor)

        items = first_dict(response, 'items_results', [])
        users = []
        for item in items:
            if 'result' not in item:
                continue
            if item['result'].get('__typename') != 'User':
                continue
            try:
                users.append(CommunityMember(self, item['result']))
            except NotFound:
                continue

        next_cursor_ = find_dict(response, 'next_cursor', find_one=True)
        next_cursor = next_cursor_[0] if next_cursor_ else None

        if next_cursor is None:
            fetch_next_result = None
        else:
            fetch_next_result = partial(self._get_community_users, f, community_id, count, next_cursor)
        users, overflow = limited(users, count)
        return Result(
            users,
            fetch_next_result,
            next_cursor,
            overflow=overflow,
            page_size=count
        )

    async def get_community_members(
        self, community_id: str, count: int = 20, cursor: str | None = None
    ) -> Result[CommunityMember]:
        """
        Retrieves members of a community.

        Parameters
        ----------
        community_id : :class:`str`
            The ID of the community.
        count : :class:`int`, default=20
            The number of members to retrieve.

        Returns
        -------
        Result[:class:`CommunityMember`]
            List of retrieved members.
        """
        return await self._get_community_users(
            self.gql.members_slice_timeline_query, community_id, count, cursor
        )

    async def get_community_moderators(
        self, community_id: str, count: int = 20, cursor: str | None = None
    ) -> Result[CommunityMember]:
        """
        Retrieves moderators of a community.

        Parameters
        ----------
        community_id : :class:`str`
            The ID of the community.
        count : :class:`int`, default=20
            The number of moderators to retrieve.

        Returns
        -------
        Result[:class:`CommunityMember`]
            List of retrieved moderators.
        """
        return await self._get_community_users(
            self.gql.moderators_slice_timeline_query, community_id, count, cursor
        )

    async def search_community_tweet(
        self,
        community_id: str,
        query: str,
        count: int = 20,
        cursor: str | None = None
    ) -> Result[Tweet]:
        """Searchs tweets in a community.

        Parameters
        ----------
        community_id : :class:`str`
            The ID of the community.
        query : :class:`str`
            The search query.
        count : :class:`int`, default=20
            The number of tweets to retrieve.

        Returns
        -------
        Result[:class:`Tweet`]
            List of retrieved tweets.
        """
        response, _ = await self.gql.community_tweet_search_module_query(community_id, query, count, cursor)

        items = first_dict(response, 'entries', [])
        tweets = []
        for item in items:
            if not item['entryId'].startswith('tweet'):
                continue

            tweet = tweet_from_data(self, item)
            if tweet is not None:
                tweets.append(tweet)

        next_cursor = last_cursor(items)
        previous_cursor = cursor_at(items, -2)

        tweets, overflow = limited(tweets, count)
        return Result(
            tweets,
            partial(self.search_community_tweet, community_id, query, count, next_cursor) if next_cursor else None,
            next_cursor,
            partial(self.search_community_tweet, community_id, query, count, previous_cursor) if previous_cursor else None,
            previous_cursor,
            overflow=overflow,
            page_size=count
        )

    async def _stream(self, topics: set[str]) -> AsyncGenerator[tuple[str, Payload]]:
        url = f'https://api.{DOMAIN}/live_pipeline/events'
        params = {'topics': ','.join(topics)}
        headers = self._base_headers
        headers.pop('content-type', None)

        async with self.http.stream('GET', url, params=params, headers=headers, timeout=None) as response:
            self._remove_duplicate_ct0_cookie()
            async for line in response.aiter_lines():
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = _payload_from_data(data['payload'])
                yield data.get('topic'), payload

    async def get_streaming_session(
        self, topics: set[str], auto_reconnect: bool = True
    ) -> StreamingSession:
        """
        Returns a session for interacting with the streaming API.

        Note
        ----
        The streaming connection is the one request that does not go through
        ``impersonate=``: it stays on plain httpx, because curl_cffi cannot
        hold the long-lived response open (measured: the same URL times out
        after 15 s with a partial body). live_pipeline still accepts httpx's
        fingerprint, so this works - but if X ever starts filtering it the way
        it filters the v1.1 endpoints, ``impersonate=`` will not help here.

        Parameters
        ----------
        topics : set[:class:`str`]
            The set of topics to stream.
            Topics can be generated using :class:`.Topic`.
        auto_reconnect : :class:`bool`, default=True
            Whether to automatically reconnect when disconnected.

        Returns
        -------
        :class:`.StreamingSession`
            A stream session instance.

        Examples
        --------
        >>> from twikit.streaming import Topic
        >>>
        >>> topics = {
        ...     Topic.tweet_engagement('1739617652'), # Stream tweet engagement
        ...     Topic.dm_update('17544932482-174455537996'), # Stream DM update
        ...     Topic.dm_typing('17544932482-174455537996') # Stream DM typing
        ... }
        >>> session = await client.get_streaming_session(topics)
        >>>
        >>> async for topic, payload in session:
        ...     if payload.dm_update:
        ...         conversation_id = payload.dm_update.conversation_id
        ...         user_id = payload.dm_update.user_id
        ...         print(f'{conversation_id}: {user_id} sent a message')
        >>>
        >>>     if payload.dm_typing:
        ...         conversation_id = payload.dm_typing.conversation_id
        ...         user_id = payload.dm_typing.user_id
        ...         print(f'{conversation_id}: {user_id} is typing')
        >>>
        >>>     if payload.tweet_engagement:
        ...         like = payload.tweet_engagement.like_count
        ...         retweet = payload.tweet_engagement.retweet_count
        ...         view = payload.tweet_engagement.view_count
        ...         print('Tweet engagement updated:'
        ...               f'likes: {like} retweets: {retweet} views: {view}')

        Topics to stream can be added or deleted using
        :attr:`.StreamingSession.update_subscriptions` method.

        >>> subscribe_topics = {
        ...     Topic.tweet_engagement('1749528513'),
        ...     Topic.tweet_engagement('1765829534')
        ... }
        >>> unsubscribe_topics = {
        ...     Topic.tweet_engagement('1739617652'),
        ...     Topic.dm_update('17544932482-174455537996'),
        ...     Topic.dm_update('17544932482-174455537996')
        ... }
        >>> await session.update_subscriptions(
        ...     subscribe_topics, unsubscribe_topics
        ... )

        See Also
        --------
        .StreamingSession
        .StreamingSession.update_subscriptions
        .Payload
        .Topic
        """
        stream = self._stream(topics)
        session_id = (await anext(stream))[1].config.session_id
        return StreamingSession(self, session_id, stream, topics, auto_reconnect)

    async def _update_subscriptions(
        self,
        session: StreamingSession,
        subscribe: set[str] | None = None,
        unsubscribe: set[str] | None = None
    ) -> Payload:
        if subscribe is None:
            subscribe = set()
        if unsubscribe is None:
            unsubscribe = set()

        response, _ = await self.v11.live_pipeline_update_subscriptions(
            session.id, ','.join(subscribe), ','.join(unsubscribe)
        )
        session.topics |= subscribe
        session.topics -= unsubscribe

        return _payload_from_data(response)

    async def _get_user_state(self) -> Literal['normal', 'bounced', 'suspended']:
        # `request()` calls this method whenever it receives a 429, to
        # decide between `TooManyRequests` and `AccountSuspended`. But the
        # call itself goes through `request()` as well, so if the
        # user_state endpoint is ALSO rate-limited (very common — X rate
        # limits the whole account, not per-endpoint), we re-enter this
        # branch and recurse until Python raises `RecursionError`. That
        # masks the real 429 with an unrelated crash.
        #
        # Pass `check_user_state=False` to the nested request so that if
        # this user_state GET also 429s, `request()` raises `TooManyRequests`
        # directly instead of re-entering this branch. That eliminates the
        # recursion at the source — not just after N levels deep — so we
        # don't burn through HTTP calls climbing back up the stack.
        #
        # We still trap the remaining failure modes: the expected
        # `TooManyRequests` (now raised on the first retry, not at the
        # recursion limit), and any transport-level `HTTPError`. Anything
        # else (unexpected JSON, auth issues, programming errors) keeps
        # propagating so real bugs surface.
        try:
            response, _ = await self.v11.user_state(check_user_state=False)
            return response['userState']
        except (TooManyRequests, HTTPError):
            return 'normal'
