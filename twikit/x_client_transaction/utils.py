import re
import bs4
import base64
from typing import Union


# Headers that only belong on API calls. A browser never sends them when it
# loads the page, and x.com refuses the page when they are present: with the
# GraphQL bearer attached it answers 401 with an empty body for live cookies
# (403, empty, for expired ones) instead of the app shell, so the handshake
# found no webpack manifest and reported the session as logged out.
_API_ONLY_HEADERS = frozenset({
    'authorization',
    'content-type',
    'x-csrf-token',
    'x-guest-token',
    'x-act-as-user-id',
    'x-client-transaction-id',
})


def page_headers(headers):
    """
    Return a copy of ``headers`` fit for fetching an HTML page: every
    API-only header (bearer, content-type, csrf, guest token,
    ``X-Twitter-*``...) is dropped, so passing the client's API headers by
    mistake still gets the real page.
    """
    return {
        key: value for key, value in (headers or {}).items()
        if key.lower() not in _API_ONLY_HEADERS
        and not key.lower().startswith('x-twitter-')
    }


async def fetch_home_page(session, headers):
    """
    Fetch the x.com home page, following the twitter.com -> x.com migration
    hop, and return ``(page, response)``: the parsed page and the response
    it was parsed from, so callers can tell an empty refusal from a page.
    """
    headers = page_headers(headers)
    home_page = None
    migration_redirection_regex = re.compile(
        r"""(http(?:s)?://(?:www\.)?(twitter|x){1}\.com(/x)?/migrate([/?])?tok=[a-zA-Z0-9%\-_]+)+""", re.VERBOSE)
    response = await session.request(method="GET", url="https://x.com", headers=headers)
    home_page = bs4.BeautifulSoup(response.content, 'lxml')
    migration_url = home_page.select_one("meta[http-equiv='refresh']")
    migration_redirection_url = re.search(migration_redirection_regex, str(
        migration_url)) or re.search(migration_redirection_regex, str(response.content))
    if migration_redirection_url:
        response = await session.request(method="GET", url=migration_redirection_url.group(0), headers=headers)
        home_page = bs4.BeautifulSoup(response.content, 'lxml')
    migration_form = home_page.select_one("form[name='f']") or home_page.select_one(f"form[action='https://x.com/x/migrate']")
    if migration_form:
        url = migration_form.attrs.get("action", "https://x.com/x/migrate") + "/?mx=2"
        method = migration_form.attrs.get("method", "POST")
        request_payload = {input_field.get("name"): input_field.get("value") for input_field in migration_form.select("input")}
        response = await session.request(method=method, url=url, data=request_payload, headers=headers)
        home_page = bs4.BeautifulSoup(response.content, 'lxml')
    return home_page, response


async def handle_x_migration(session, headers):
    home_page, _ = await fetch_home_page(session, headers)
    return home_page


def float_to_hex(x):
    result = []
    quotient = int(x)
    fraction = x - quotient

    while quotient > 0:
        quotient = int(x / 16)
        remainder = int(x - (float(quotient) * 16))

        if remainder > 9:
            result.insert(0, chr(remainder + 55))
        else:
            result.insert(0, str(remainder))

        x = float(quotient)

    if fraction == 0:
        return ''.join(result)

    result.append('.')

    while fraction > 0:
        fraction *= 16
        integer = int(fraction)
        fraction -= float(integer)

        if integer > 9:
            result.append(chr(integer + 55))
        else:
            result.append(str(integer))

    return ''.join(result)


def is_odd(num: Union[int, float]):
    if num % 2:
        return -1.0
    return 0.0


def base64_encode(string):
    string = string.encode() if isinstance(string, str) else string
    return base64.b64encode(string).decode()


def base64_decode(input):
    try:
        data = base64.b64decode(input)
        return data.decode()
    except Exception:
        # return bytes(input, "utf-8")
        return list(bytes(input, "utf-8"))


if __name__ == "__main__":
    pass
