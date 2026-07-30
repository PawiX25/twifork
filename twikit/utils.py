from __future__ import annotations

import base64
import json
from datetime import datetime
from httpx import AsyncHTTPTransport
from typing import TYPE_CHECKING, Any, Awaitable, Generic, Iterator, Literal, TypedDict, TypeVar

if TYPE_CHECKING:
    from .client.client import Client

T = TypeVar('T')


class Result(Generic[T]):
    """
    This class is for storing multiple results.
    The `next` method can be used to retrieve further results.
    As with a regular list, you can access elements by
    specifying indexes and iterate over elements using a for loop.

    Warning
    -------
    Paginate with :func:`next`, not by feeding :attr:`next_cursor` back into
    the method yourself. X ignores `count` on most endpoints and hands back
    far more than was asked for - 70 users for a requested 20 is normal - so
    the surplus is buffered inside this object and served by :func:`next`
    before the next request goes out. :attr:`next_cursor` already points past
    that surplus, so calling the method again with it skips every buffered
    item::

        # keeps everything
        page = await client.get_user_followers(user_id, 20)
        while page:
            page = await page.next()

        # silently drops the ~50 buffered users on each round
        page = await client.get_user_followers(user_id, 20)
        page = await client.get_user_followers(
            user_id, 20, cursor=page.next_cursor)

    Stop on the page, not on the cursor. A cursor marks a position, not the
    promise of more data, and X hands one back on an empty page too - so
    ``while page.next_cursor`` keeps re-fetching the same empty page forever,
    while ``while page`` ends as soon as one comes back with nothing in it.
    (That is deliberate on X's side: it lets you hold the position and poll
    it later, which is how the notification timeline is meant to be read.)

    Attributes
    ----------
    next_cursor : :class:`str`
        Cursor used to obtain the next result. Points past any buffered
        surplus - see the warning above.
    previous_cursor : :class:`str`
        Cursor used to obtain the previous result.
    token : :class:`str`
        Alias of `next_cursor`.
    cursor : :class:`str`
        Alias of `next_cursor`.
    """

    def __init__(
        self,
        results: list[T],
        fetch_next_result: Awaitable | None = None,
        next_cursor: str | None = None,
        fetch_previous_result: Awaitable | None = None,
        previous_cursor: str | None = None,
        overflow: list[T] | None = None,
        page_size: int | None = None
    ) -> None:
        self.__results = results
        self.next_cursor = next_cursor
        self.__fetch_next_result = fetch_next_result
        self.previous_cursor = previous_cursor
        self.__fetch_previous_result = fetch_previous_result
        # Items X sent beyond the requested count. Honouring `count` by simply
        # dropping them would skip data, because the cursor already points past
        # everything that arrived, so they are handed out first instead.
        self.__overflow = overflow or []
        # How many items the caller asked for, so the surplus is handed back
        # in pages of that size rather than in one lump.
        self.__page_size = page_size or len(results) or None

    async def next(self) -> Result[T]:
        """
        The next result.
        """
        if self.__overflow:
            page, rest = limited(self.__overflow, self.__page_size)
            return Result(
                page,
                self.__fetch_next_result,
                self.next_cursor,
                self.__fetch_previous_result,
                self.previous_cursor,
                rest,
                self.__page_size
            )
        if self.__fetch_next_result is None:
            return Result([])
        return await self.__fetch_next_result()

    async def previous(self) -> Result[T]:
        """
        The previous result.
        """
        if self.__fetch_previous_result is None:
            return Result([])
        return await self.__fetch_previous_result()

    @classmethod
    def empty(cls):
        return cls([])

    def __iter__(self) -> Iterator[T]:
        yield from self.__results

    def __getitem__(self, index: int) -> T:
        return self.__results[index]

    def __len__(self) -> int:
        return len(self.__results)

    def __repr__(self) -> str:
        return self.__results.__repr__()


class Flow:
    def __init__(self, client: Client, guest_token: str) -> None:
        self._client = client
        self.guest_token = guest_token
        self.response = None

    async def execute_task(self, *subtask_inputs, **kwargs) -> None:
        response, _ = await self._client.v11.onboarding_task(
            self.guest_token, self.token, list(subtask_inputs), **kwargs
        )
        self.response = response

    async def sso_init(self, provider: str) -> None:
        await self._client.v11.sso_init(provider, self.guest_token)

    @property
    def token(self) -> str | None:
        if self.response is None:
            return None
        return self.response.get('flow_token')

    @property
    def task_id(self) -> str | None:
        if self.response is None:
            return None
        if len(self.response['subtasks']) <= 0:
            return None
        return self.response['subtasks'][0]['subtask_id']


def find_dict(obj: list | dict, key: str | int, find_one: bool = False) -> list[Any]:
    """
    Retrieves elements from a nested dictionary.
    """
    results = []
    if isinstance(obj, dict):
        if key in obj:
            results.append(obj.get(key))
            if find_one:
                return results
    if isinstance(obj, (list, dict)):
        for elem in (obj if isinstance(obj, list) else obj.values()):
            r = find_dict(elem, key, find_one)
            results += r
            if r and find_one:
                return results
    return results


def httpx_transport_to_url(transport: AsyncHTTPTransport) -> str:
    url = transport._pool._proxy_url
    scheme = url.scheme.decode()
    host = url.host.decode()
    port = url.port
    auth = None
    if transport._pool._proxy_headers:
        auth_header = dict(transport._pool._proxy_headers)[b'Proxy-Authorization'].decode()
        auth = base64.b64decode(auth_header.split()[1]).decode()

    url_str = f'{scheme}://'
    if auth is not None:
        url_str += auth + '@'
    url_str += host
    if port is not None:
        url_str += f':{port}'
    return url_str


def get_query_id(url: str) -> str:
    """
    Extracts the identifier from a URL.

    Examples
    --------
    >>> get_query_id('https://twitter.com/i/api/graphql/queryid/...')
    'queryid'
    """
    return url.rsplit('/', 2)[-2]


def timestamp_to_datetime(timestamp: str) -> datetime:
    return datetime.strptime(timestamp, '%a %b %d %H:%M:%S %z %Y')


def build_tweet_data(raw_data: dict) -> dict:
    return {
        **raw_data,
        # v1.1 sends both; `id` is a number that loses precision the
        # moment it is re-serialised, `id_str` is the safe one.
        'rest_id': raw_data.get('id_str') or raw_data['id'],
        'is_translatable': None,
        'views': {},
        'edit_control': {},
        'legacy': {
            'created_at': raw_data.get('created_at'),
            'full_text': raw_data.get('full_text') or raw_data.get('text'),
            'lang': raw_data.get('lang'),
            'is_quote_status': raw_data.get('is_quote_status'),
            'in_reply_to_status_id_str': raw_data.get('in_reply_to_status_id_str'),
            'retweeted_status_result': raw_data.get('retweeted_status_result'),
            'possibly_sensitive': raw_data.get('possibly_sensitive'),
            'possibly_sensitive_editable': raw_data.get('possibly_sensitive_editable'),
            'quote_count': raw_data.get('quote_count'),
            'entities': raw_data.get('entities'),
            'reply_count': raw_data.get('reply_count'),
            'favorite_count': raw_data.get('favorite_count'),
            'favorited': raw_data.get('favorited'),
            'retweet_count': raw_data.get('retweet_count')
        }
    }


def build_user_data(raw_data: dict) -> dict:
    return {
        **raw_data,
        # v1.1 sends both; `id` is a number that loses precision the
        # moment it is re-serialised, `id_str` is the safe one.
        'rest_id': raw_data.get('id_str') or raw_data['id'],
        'is_blue_verified': raw_data.get('ext_is_blue_verified'),
        'legacy': {
            'created_at': raw_data.get('created_at'),
            'name': raw_data.get('name'),
            'screen_name': raw_data.get('screen_name'),
            'profile_image_url_https': raw_data.get('profile_image_url_https'),
            'location': raw_data.get('location'),
            'description': raw_data.get('description'),
            'entities': raw_data.get('entities'),
            'pinned_tweet_ids_str': raw_data.get('pinned_tweet_ids_str'),
            'verified': raw_data.get('verified'),
            'possibly_sensitive': raw_data.get('possibly_sensitive'),
            'can_dm': raw_data.get('can_dm'),
            'can_media_tag': raw_data.get('can_media_tag'),
            'want_retweets': raw_data.get('want_retweets'),
            'default_profile': raw_data.get('default_profile'),
            'default_profile_image': raw_data.get('default_profile_image'),
            'has_custom_timelines': raw_data.get('has_custom_timelines'),
            'followers_count': raw_data.get('followers_count'),
            'fast_followers_count': raw_data.get('fast_followers_count'),
            'normal_followers_count': raw_data.get('normal_followers_count'),
            'friends_count': raw_data.get('friends_count'),
            'favourites_count': raw_data.get('favourites_count'),
            'listed_count': raw_data.get('listed_count'),
            'media_count': raw_data.get('media_count'),
            'statuses_count': raw_data.get('statuses_count'),
            'is_translator': raw_data.get('is_translator'),
            'translator_type': raw_data.get('translator_type'),
            'withheld_in_countries': raw_data.get('withheld_in_countries'),
            'url': raw_data.get('url'),
            'profile_banner_url': raw_data.get('profile_banner_url')
        }
    }


def flatten_params(params: dict) -> dict:
    flattened_params = {}
    for key, value in params.items():
        if isinstance(value, (list, dict)):
            value = json.dumps(value)
        flattened_params[key] = value
    return flattened_params


def b64_to_str(b64: str) -> str:
    return base64.b64decode(b64).decode()


def find_entry_by_type(entries, type_filter):
    for entry in entries:
        if entry.get('type') == type_filter:
            return entry
    return None


FILTERS = Literal[
    'media',
    'retweets',
    'native_video',
    'periscope',
    'vine',
    'images',
    'twimg',
    'links'
]


class SearchOptions(TypedDict):
    exact_phrases: list[str]
    or_keywords: list[str]
    exclude_keywords: list[str]
    hashtags: list[str]
    from_user: str
    to_user: str
    place: str
    mentioned_users: list[str]
    filters: list[FILTERS]
    exclude_filters: list[FILTERS]
    urls: list[str]
    since: str
    until: str
    positive: bool
    negative: bool
    question: bool


def build_query(text: str, options: SearchOptions) -> str:
    """
    Builds a search query based on the given text and search options.

    Parameters
    ----------
    text : str
        The base text of the search query.
    options : SearchOptions
        A dictionary containing various search options.
        - exact_phrases: list[str]
            List of exact phrases to include in the search query.
        - or_keywords: list[str]
            List of keywords where tweets must contain at least
            one of these keywords.
        - exclude_keywords: list[str]
            A list of keywords that the tweet must contain these keywords.
        - hashtags: list[str]
            List of hashtags to include in the search query.
        - from_user: str
            Specify a username. Only tweets from this user will
            be includedin the search.
        - to_user: str
            Specify a username. Only tweets sent to this user will
            be included in the search.
        - place: str
            Restrict the search to a place, e.g. 'Warsaw'. Note that X has
            retired the `near:`, `within:` and `geocode:` operators - they
            return nothing - so this is the only location filter left.
        - mentioned_users: list[str]
            List of usernames. Only tweets mentioning these users will
            be included in the search.
        - filters: list[FILTERS]
            List of tweet filters to include in the search query.
        - exclude_filters: list[FILTERS]
            List of tweet filters to exclude from the search query.
        - urls: list[str]
            List of URLs. Only tweets containing these URLs will be
            included in the search.
        - since: str
            Specify a date (formatted as 'YYYY-MM-DD'). Only tweets since
            this date will be included in the search.
        - until: str
            Specify a date (formatted as 'YYYY-MM-DD'). Only tweets until
            this date will be included in the search.
        - positive: bool
            Include positive sentiment in the search.
        - negative: bool
            Include negative sentiment in the search.
        - question: bool
            Search for tweets in questionable form.

        https://developer.twitter.com/en/docs/twitter-api/v1/rules-and-filtering/search-operators

    Returns
    -------
    str
        The constructed Twitter search query.
    """
    if exact_phrases := options.get('exact_phrases'):
        text += ' ' + ' '.join(
            [f'"{i}"' for i in exact_phrases]
        )

    if or_keywords := options.get('or_keywords'):
        text += ' ' + ' OR '.join(or_keywords)

    if exclude_keywords := options.get('exclude_keywords'):
        text += ' ' + ' '.join(
            [f'-"{i}"' for i in exclude_keywords]
        )

    if hashtags := options.get('hashtags'):
        text += ' ' + ' '.join(
            [f'#{i}' for i in hashtags]
        )

    if from_user := options.get('from_user'):
        text +=f' from:{from_user}'

    if to_user := options.get('to_user'):
        text += f' to:{to_user}'

    if place := options.get('place'):
        text += f' place:"{place}"'

    if mentioned_users := options.get('mentioned_users'):
        text += ' ' + ' '.join(
            [f'@{i}' for i in mentioned_users]
        )

    if filters := options.get('filters'):
        text += ' ' + ' '.join(
            [f'filter:{i}' for i in filters]
        )

    if exclude_filters := options.get('exclude_filters'):
        text += ' ' + ' '.join(
            [f'-filter:{i}' for i in exclude_filters]
        )

    if urls := options.get('urls'):
        text += ' ' + ' '.join(
            [f'url:{i}' for i in urls]
        )

    if since := options.get('since'):
        text += f' since:{since}'

    if until := options.get('until'):
        text += f' until:{until}'

    if options.get('positive') is True:
        text += ' :)'

    if options.get('negative') is True:
        text += ' :('

    if options.get('question') is True:
        text += ' ?'

    return text


def first_dict(data: dict | list, key: str, default=None):
    """
    First value `find_dict` matches, or `default` when X omitted the key.

    Indexing ``find_dict(...)[0]`` directly is where nearly every reported
    "list index out of range" came from: X leaves keys out whenever a
    timeline is empty, an entry is unavailable or a lookup found nothing.
    """
    found = find_dict(data, key, find_one=True)
    return found[0] if found else default


def last_cursor(entries: list) -> str | None:
    """
    Cursor value carried by the last timeline entry, or None.

    Empty timelines have no last entry at all, so indexing ``[-1]`` is the
    other half of the "list index out of range" family.
    """
    if not entries:
        return None
    content = entries[-1].get('content')
    if not isinstance(content, dict):
        return None
    value = content.get('value')
    if value is not None:
        return value
    item_content = content.get('itemContent')
    if isinstance(item_content, dict):
        return item_content.get('value')
    return None


def cursor_at(entries: list, index: int) -> str | None:
    """
    Cursor value of the entry at `index`, or None when it is not there.

    Timelines routinely come back shorter than the code expects - an empty
    page has no entry at -2 at all - so indexing straight through raised
    IndexError instead of simply meaning "no cursor".
    """
    if not entries:
        return None
    try:
        entry = entries[index]
    except IndexError:
        return None
    content = entry.get('content')
    if not isinstance(content, dict):
        return None
    return content.get('value')


def subobject(data: dict, key: str) -> dict:
    """
    Reads one of the nested profile objects X now sends alongside `legacy`.

    A v1.1 payload keeps `location` as a plain string under that same name,
    so a value that is not a mapping has to read as absent and let the caller
    fall back to `legacy`.
    """
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def fatal_errors(response: dict, required: str | None = None) -> list | None:
    """
    Returns the errors that actually sank a GraphQL response, or None.

    X answers with `data` and `errors` together: a field buried in the payload
    can fail to decode while the operation itself went through, and the error
    entry then carries a `path` pointing at that field. Treating those as
    failures throws away a perfectly good result, so only errors that left
    nothing usable behind count.

    Parameters
    ----------
    required : :class:`str`, default=None
        Key the caller needs out of `data`. A refusal often comes back with
        `data` holding nothing but a shell - measured on bookmark folders:
        ``{"viewer": {"user_results": {"result": {"__typename": "User"}}}}``
        next to ``code 37, "User is not authorized to use bookmark
        collections"``. `data` is truthy there, so without this the error was
        dropped and the caller reported an empty list instead of a refusal.

        The key is looked for anywhere in `data`, so pick one that only the
        successful shape has. A generic name defeats the check: ``rest_id``
        appears inside the refusal shell above, so asking for that would
        read the refusal as a success.
    """
    if not isinstance(response, dict):
        return None
    errors = response.get('errors')
    if not errors or not isinstance(errors, list):
        return None
    # X occasionally sends a non-dict entry, and every caller reads
    # errors[0]['message'] - normalise here so none of them has to guard.
    errors = [
        e if isinstance(e, dict) else {'message': str(e)} for e in errors
    ]
    data = response.get('data')
    if not data:
        return errors
    if required is not None and not find_dict(data, required, find_one=True):
        return errors
    return None


def limited(results: list, count: int):
    """
    Splits a page at the requested count.

    Returns the head to hand back now and the tail to keep for `next()`, so a
    caller that asked for five items gets five without the rest going missing.
    """
    if count is None or count <= 0 or len(results) <= count:
        return results, []
    return results[:count], results[count:]
