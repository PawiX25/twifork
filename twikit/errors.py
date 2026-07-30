"""
Exceptions raised by twikit.

X error codes seen in practice
------------------------------
X answers with an HTTP status *and* a numeric code inside the body; the code
is the one that actually tells you what happened.

======  =======================================================================
code    meaning
======  =======================================================================
32      Bad or expired credentials. The usual cause of a dead cookie set.
34      The endpoint no longer exists. Several v1.1 endpoints answer this now.
37      Not authorised for this resource. **Not** a suspension, despite the
        wording X sometimes uses - only treat it as one if the message itself
        says "suspend".
63      The account is suspended.
64      The authenticating account is suspended.
88      Rate limit exceeded. See :attr:`Client.rate_limit_reset`.
89      Invalid or expired token.
139     The tweet is already favourited.
144     No tweet with that id.
179     Not authorised to see this tweet (protected account).
187     Duplicate tweet - the same text was posted recently.
226     X thinks the request is automated. Account-level spam score.
279     The direct message conversation does not exist.
326     The account is temporarily locked, see :class:`AccountLocked`.
349     You cannot send messages to this user (they do not accept DMs from
        people they do not follow).
398     X could not confirm you are human - Arkose/FunCaptcha during login.
399     Login challenge that needs a castle token.
======  =======================================================================

Intermittent 404s
-----------------
A 404 that comes and goes for the same call is almost always the
``x-client-transaction-id`` handshake, not the endpoint: X enforces the header
selectively, so an id built from stale keys passes on some operations and is
rejected on others. The keys are fetched once per client and never refreshed,
while the ``ondemand.s`` bundle they come from rotates every few days - so a
long-lived process degrades into sporadic 404s. Rebuilding the client makes
them go away.
"""

from __future__ import annotations


class TwitterException(Exception):
    """
    Base class for Twitter API related exceptions.
    """
    def __init__(self, *args: object, headers: dict | None = None) -> None:
        super().__init__(*args)
        if headers is None:
            self.headers = None
        else:
            self.headers = dict(headers)

class BadRequest(TwitterException):
    """
    Exception raised for 400 Bad Request errors.
    """

class Unauthorized(TwitterException):
    """
    Exception raised for 401 Unauthorized errors.
    """

class Forbidden(TwitterException):
    """
    Exception raised for 403 Forbidden errors.
    """

class NotFound(TwitterException):
    """
    Exception raised for 404 Not Found errors.
    """

class RequestTimeout(TwitterException):
    """
    Exception raised for 408 Request Timeout errors.
    """

class TooManyRequests(TwitterException):
    """
    Exception raised for 429 Too Many Requests errors.
    """
    def __init__(self, *args, headers: dict | None = None) -> None:
        super().__init__(*args, headers=headers)
        if headers is not None and 'x-rate-limit-reset' in headers:
            self.rate_limit_reset = int(headers.get('x-rate-limit-reset'))
        else:
            self.rate_limit_reset = None

class ServerError(TwitterException):
    """
    Exception raised for 5xx Server Error responses.
    """

class CouldNotTweet(TwitterException):
    """
    Exception raised when a tweet could not be sent.
    """

class DuplicateTweet(CouldNotTweet):
    """
    Exception raised when a tweet is a duplicate of another.
    """

class TweetNotAvailable(TwitterException):
    """
    Exceptions raised when a tweet is not available.
    """

class InvalidMedia(TwitterException):
    """
    Exception raised when there is a problem with the media ID
    sent with the tweet.
    """

class UserNotFound(TwitterException):
    """
    Exception raised when a user does not exsit.
    """

class UserUnavailable(TwitterException):
    """
    Exception raised when a user is unavailable.
    """

class AccountSuspended(TwitterException):
    """
    Exception raised when the account is suspended.
    """

class AccountLocked(TwitterException):
    """
    Exception raised when the account is locked (very likey is Arkose challenge).
    """

ERROR_CODE_TO_EXCEPTION: dict[int, TwitterException] = {
    187: DuplicateTweet,
    324: InvalidMedia
}


def raise_exceptions_from_response(errors: list[dict]):
    for error in errors:
        code = error.get('code')
        if code not in ERROR_CODE_TO_EXCEPTION:
            code = error.get('extensions', {}).get('code')
        exception = ERROR_CODE_TO_EXCEPTION.get(code)
        if exception is not None:
            raise exception(error['message'])


class ClientTransactionError(TwitterException):
    """
    Exception raised when the X-Client-Transaction-Id handshake
    cannot be completed.
    """


class LoginRetired(TwitterException):
    """
    Exception raised when password login is attempted.

    X retired the onboarding flow :func:`Client.login` drives. This is a
    separate type from :class:`ClientTransactionError` on purpose: the
    handshake is merely where the attempt dies first, and anyone catching
    that to retry a handshake should not silently swallow "password login
    no longer exists".
    """


class InvalidSession(ClientTransactionError):
    """
    Exception raised when x.com serves the logged-out page shell,
    which means the cookies are missing, expired or rejected.
    """
