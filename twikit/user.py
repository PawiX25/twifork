from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from .utils import subobject, timestamp_to_datetime

if TYPE_CHECKING:
    from httpx import Response

    from .client.client import Client
    from .message import Message
    from .tweet import Tweet
    from .utils import Result


class User:
    """
    Attributes
    ----------
    id : :class:`str`
        The unique identifier of the user.
    created_at : :class:`str`
        The date and time when the user account was created.
    name : :class:`str`
        The user's name.
    screen_name : :class:`str`
        The user's screen name.
    profile_image_url : :class:`str`
        The URL of the user's profile image (HTTPS version).
    profile_banner_url : :class:`str`
        The URL of the user's profile banner.
    url : :class:`str`
        The user's URL.
    location : :class:`str`
        The user's location information.
    description : :class:`str`
        The user's profile description.
    description_urls : :class:`list`
        URLs found in the user's profile description.
    urls : :class:`list`
        URLs associated with the user.
    pinned_tweet_ids : :class:`str`
        The IDs of tweets that the user has pinned to their profile.
    is_blue_verified : :class:`bool`
        Indicates if the user is verified with a blue checkmark.
    verified : :class:`bool`
        Indicates if the user has the legacy verified badge.
    verified_type : :class:`str` | None
        ``'Business'`` or ``'Government'`` for organisation accounts, None
        otherwise. Organisations have :attr:`verified` False - the badge they
        show comes from this field and :attr:`is_blue_verified`.
    parody_commentary_fan_label : :class:`str` | None
        ``'Parody'``, ``'Commentary'`` or ``'Fan'`` for accounts X requires to
        declare themselves as such, None otherwise.
    automated_label : :class:`str` | None
        The affiliate label X shows on the profile, ``'Automated'`` for bot
        accounts, None when there is none.
    is_automated : :class:`bool`
        Whether X marks this account as automated.
    automated_by : :class:`str` | None
        Screen name of the account that operates this bot, when X names one.
    notifications_enabled : :class:`bool`
        Whether the logged in account has notifications turned on for this
        user.
    possibly_sensitive : :class:`bool`
        Indicates if the user's content may be sensitive.
    can_dm : :class:`bool`
        Indicates whether the user can receive direct messages.
    can_media_tag : :class:`bool`
        Indicates whether the user can be tagged in media.
    want_retweets : :class:`bool`
        Indicates if the user wants retweets.
    default_profile : :class:`bool`
        Indicates if the user has the default profile.
    default_profile_image : :class:`bool`
        Indicates if the user has the default profile image.
    has_custom_timelines : :class:`bool`
        Indicates if the user has custom timelines.
    followers_count : :class:`int`
        The count of followers.
    fast_followers_count : :class:`int`
        The count of fast followers.
    normal_followers_count : :class:`int`
        The count of normal followers.
    following_count : :class:`int`
        The count of users the user is following.
    following : :class:`bool`
        Whether the logged in account follows this user.
    followed_by : :class:`bool`
        Whether this user follows the logged in account.
    blocking : :class:`bool`
        Whether the logged in account blocks this user.
    blocked_by : :class:`bool`
        Whether this user blocks the logged in account.
    muting : :class:`bool`
        Whether the logged in account mutes this user.
    favourites_count : :class:`int`
        The count of favorites or likes.
    listed_count : :class:`int`
        The count of lists the user is a member of.
    media_count : :class:`int`
        The count of media items associated with the user.
    statuses_count : :class:`int`
        The count of tweets.
    is_translator : :class:`bool`
        Indicates if the user is a translator.
    translator_type : :class:`str`
        The type of translator.
    profile_interstitial_type : :class:`str`
        The type of profile interstitial.
    withheld_in_countries : list[:class:`str`]
        Countries where the user's content is withheld.

    Note
    ----
    These fields keep their default - 0, False or an empty list - and the
    default is not a measurement: ``listed_count``, ``fast_followers_count``,
    ``normal_followers_count``, ``default_profile``,
    ``default_profile_image``, ``has_custom_timelines``, ``want_retweets``,
    ``is_translator`` and ``withheld_in_countries``.

    They live in the ``legacy`` block, and the profile document this client
    asks for returns that block empty; the typed objects that carry
    everything else have no equivalent for these nine. X has not withdrawn
    the data - measured on the same account in the same minute, the previous
    document still answers with ``legacy`` populated
    (``listed_count`` 97030 for @NASA) - but that document carries none of
    the typed objects, nor the parody label, nor the notification setting.
    The two are alternatives, not versions, and this client takes the one
    with the richer profile.
    """

    def __init__(self, client: Client, data: dict) -> None:
        self._client = client
        legacy = data.get('legacy', {})
        # X moved several profile fields out of `legacy` into top-level objects
        # (core/avatar/location/verification/...); read those first, fall back to legacy.
        core = subobject(data, 'core')
        avatar = subobject(data, 'avatar')
        banner = subobject(data, 'banner')
        location = subobject(data, 'location')
        verification = subobject(data, 'verification')
        privacy = subobject(data, 'privacy')
        dm_permissions = subobject(data, 'dm_permissions')
        media_permissions = subobject(data, 'media_permissions')
        profile_bio = subobject(data, 'profile_bio')
        website = subobject(data, 'website')
        pinned_items = subobject(data, 'pinned_items')
        relationship_counts = subobject(data, 'relationship_counts')
        tweet_counts = subobject(data, 'tweet_counts')
        action_counts = subobject(data, 'action_counts')
        profile_metadata = subobject(data, 'profile_metadata')
        relationship = subobject(data, 'relationship_perspectives')
        bio_entities = subobject(profile_bio, 'entities')

        self.id: str = data.get('rest_id', '')
        self.created_at: str = core.get('created_at') or legacy.get('created_at', '')
        self.name: str = core.get('name') or legacy.get('name', '')
        self.screen_name: str = core.get('screen_name') or legacy.get('screen_name', '')
        self.profile_image_url: str = avatar.get('image_url') or legacy.get('profile_image_url_https', '')
        self.profile_banner_url: str = banner.get('image_url') or legacy.get('profile_banner_url')
        self.url: str = website.get('url') or legacy.get('url')
        self.location: str = location.get('location') or legacy.get('location', '')
        self.description: str = profile_bio.get('description') or legacy.get('description', '')
        self.description_urls: list = (
            subobject(bio_entities, 'description').get('urls')
            or legacy.get('entities', {}).get('description', {}).get('urls', [])
        )
        self.urls: list = (
            subobject(bio_entities, 'url').get('urls')
            or legacy.get('entities', {}).get('url', {}).get('urls')
        )
        self.pinned_tweet_ids: list[str] = (
            pinned_items.get('tweet_ids_str')
            or legacy.get('pinned_tweet_ids_str', [])
        )
        self.is_blue_verified: bool = data.get('is_blue_verified', False)
        self.verified: bool = verification.get('verified', legacy.get('verified', False))
        # X split verification into a legacy boolean and a type. Organisations
        # come back with verified=False and verified_type='Business', which
        # reads like a bug unless the type is visible too.
        self.verified_type: str | None = verification.get(
            'verified_type', legacy.get('verified_type'))
        # X labels accounts that impersonate or comment on someone else.
        # Delivered only when the request carries
        # `profile_label_improvements_pcf_label_in_post_enabled`; X sends the
        # string 'None' rather than null for an account with no label.
        label = data.get('parody_commentary_fan_label')
        self.parody_commentary_fan_label: str | None = (
            None if label in (None, 'None') else label)
        # X marks bot accounts with an affiliate label whose long description
        # names the operator, e.g. "Automated by @billsnitzer". It only comes
        # back when the request asks for auxiliary user labels, which is why
        # it looked absent - and it is empty on accounts that carry no label,
        # so checking a handful of bots proves nothing either way.
        highlighted = subobject(data, 'affiliates_highlighted_label')
        label_data = subobject(highlighted, 'label')
        self.automated_label: str | None = label_data.get('description')
        self.is_automated: bool = self.automated_label == 'Automated'
        long_description = subobject(
            label_data, 'longDescription').get('text') or ''
        match = re.search(r'Automated by @(\w+)', long_description)
        self.automated_by: str | None = match.group(1) if match else None
        self.notifications_enabled: bool = subobject(
            data, 'notifications_settings').get('notifications_enabled', False)
        self.possibly_sensitive: bool = data.get(
            'possibly_sensitive', legacy.get('possibly_sensitive', False))
        self.can_dm: bool = dm_permissions.get('can_dm', legacy.get('can_dm', False))
        self.can_media_tag: bool = media_permissions.get('can_media_tag', legacy.get('can_media_tag', False))
        self.want_retweets: bool = legacy.get('want_retweets', False)
        self.default_profile: bool = legacy.get('default_profile', False)
        self.default_profile_image: bool = legacy.get('default_profile_image', False)
        self.has_custom_timelines: bool = legacy.get('has_custom_timelines', False)
        self.followers_count: int = relationship_counts.get(
            'followers', legacy.get('followers_count', 0))
        self.fast_followers_count: int = legacy.get('fast_followers_count', 0)
        self.normal_followers_count: int = legacy.get('normal_followers_count', 0)
        self.following_count: int = relationship_counts.get(
            'following', legacy.get('friends_count', 0))
        self.favourites_count: int = action_counts.get(
            'favorites_count', legacy.get('favourites_count', 0))
        self.listed_count: int = legacy.get('listed_count', 0)
        self.media_count = tweet_counts.get(
            'media_tweets', legacy.get('media_count', 0))
        self.statuses_count: int = tweet_counts.get(
            'tweets', legacy.get('statuses_count', 0))
        self.is_translator: bool = legacy.get('is_translator', False)
        self.translator_type: str = subobject(
            data, 'profile_translation'
        ).get('translator_type', legacy.get('translator_type', ''))
        self.profile_interstitial_type: str = profile_metadata.get(
            'profile_interstitial_type', legacy.get('profile_interstitial_type', ''))
        self.withheld_in_countries: list[str] = legacy.get('withheld_in_countries', [])
        self.protected: bool = privacy.get('protected', legacy.get('protected', False))
        # X moved the viewer's relationship with this account into
        # relationship_perspectives; the legacy flags are the old home.
        self.following: bool = relationship.get(
            'following', legacy.get('following', False))
        self.followed_by: bool = relationship.get(
            'followed_by', legacy.get('followed_by', False))
        self.blocking: bool = relationship.get(
            'blocking', legacy.get('blocking', False))
        self.blocked_by: bool = relationship.get(
            'blocked_by', legacy.get('blocked_by', False))
        self.muting: bool = relationship.get(
            'muting', legacy.get('muting', False))
        self.live_following: bool = relationship.get(
            'live_following', legacy.get('live_following', False))

    @property
    def created_at_datetime(self) -> datetime:
        return timestamp_to_datetime(self.created_at)

    async def get_tweets(
        self,
        tweet_type: Literal['Tweets', 'Replies', 'Media', 'Likes'],
        count: int = 40,
    ) -> Result[Tweet]:
        """
        Retrieves the user's tweets.

        Parameters
        ----------
        tweet_type : {'Tweets', 'Replies', 'Media', 'Likes'}
            The type of tweets to retrieve.
        count : :class:`int`, default=40
            The number of tweets to retrieve.

        Returns
        -------
        Result[:class:`Tweet`]
            A Result object containing a list of `Tweet` objects.

        Examples
        --------
        >>> user = await client.get_user_by_screen_name('example_user')
        >>> tweets = await user.get_tweets('Tweets', count=20)
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
        return await self._client.get_user_tweets(self.id, tweet_type, count)

    async def follow(self) -> Response:
        """
        Follows the user.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        See Also
        --------
        Client.follow_user
        """
        return await self._client.follow_user(self.id)

    async def unfollow(self) -> Response:
        """
        Unfollows the user.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        See Also
        --------
        Client.unfollow_user
        """
        return await self._client.unfollow_user(self.id)

    async def block(self) -> Response:
        """
        Blocks a user.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the user to block.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        See Also
        --------
        .unblock
        """
        return await self._client.block_user(self.id)

    async def unblock(self) -> Response:
        """
        Unblocks a user.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the user to unblock.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        See Also
        --------
        .block
        """
        return await self._client.unblock_user(self.id)

    async def mute(self) -> Response:
        """
        Mutes a user.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the user to mute.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        See Also
        --------
        .unmute
        """
        return await self._client.mute_user(self.id)

    async def unmute(self) -> Response:
        """
        Unmutes a user.

        Parameters
        ----------
        user_id : :class:`str`
            The ID of the user to unmute.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        See Also
        --------
        .mute
        """
        return await self._client.unmute_user(self.id)

    async def get_followers(self, count: int = 20) -> Result[User]:
        """
        Retrieves a list of followers for the user.

        Parameters
        ----------
        count : :class:`int`, default=20
            The number of followers to retrieve.

        Returns
        -------
        Result[:class:`User`]
            A list of User objects representing the followers.

        See Also
        --------
        Client.get_user_followers
        """
        return await self._client.get_user_followers(self.id, count)

    async def get_verified_followers(self, count: int = 20) -> Result[User]:
        """
        Retrieves a list of verified followers for the user.

        Parameters
        ----------
        count : :class:`int`, default=20
            The number of verified followers to retrieve.

        Returns
        -------
        Result[:class:`User`]
            A list of User objects representing the verified followers.

        See Also
        --------
        Client.get_user_verified_followers
        """
        return await self._client.get_user_verified_followers(self.id, count)

    async def get_followers_you_know(self, count: int = 20) -> Result[User]:
        """
        Retrieves a list of followers whom the user might know.

        Parameters
        ----------
        count : :class:`int`, default=20
            The number of followers you might know to retrieve.

        Returns
        -------
        Result[:class:`User`]
            A list of User objects representing the followers you might know.

        See Also
        --------
        Client.get_user_followers_you_know
        """
        return await self._client.get_user_followers_you_know(self.id, count)

    async def get_following(self, count: int = 20) -> Result[User]:
        """
        Retrieves a list of users whom the user is following.

        Parameters
        ----------
        count : :class:`int`, default=20
            The number of following users to retrieve.

        Returns
        -------
        Result[:class:`User`]
            A list of User objects representing the users being followed.

        See Also
        --------
        Client.get_user_following
        """
        return await self._client.get_user_following(self.id, count)

    async def get_subscriptions(self, count: int = 20) -> Result[User]:
        """
        Retrieves a list of users whom the user is subscribed to.

        Parameters
        ----------
        count : :class:`int`, default=20
            The number of subscriptions to retrieve.

        Returns
        -------
        Result[:class:`User`]
            A list of User objects representing the subscribed users.

        See Also
        --------
        Client.get_user_subscriptions
        """
        return await self._client.get_user_subscriptions(self.id, count)

    async def get_latest_followers(
        self, count: int | None = None, cursor: str | None = None
    ) -> Result[User]:
        """
        Retrieves the latest followers.
        Max count : 200
        """
        return await self._client.get_latest_followers(
            self.id, count=count, cursor=cursor
        )

    async def get_latest_friends(
        self, count: int | None = None, cursor: str | None = None
    ) -> Result[User]:
        """
        Retrieves the latest friends (following users).
        Max count : 200
        """
        return await self._client.get_latest_friends(
            self.id, count=count, cursor=cursor
        )

    async def send_dm(
        self, text: str, media_id: str = None, reply_to = None
    ) -> Message:
        """
        Send a direct message to the user.

        Parameters
        ----------
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
        >>> media_id = await client.upload_media('image.png')
        >>> message = await user.send_dm('text', media_id)
        >>> print(message)
        <Message id="...">

        See Also
        --------
        Client.upload_media
        Client.send_dm
        """
        return await self._client.send_dm(self.id, text, media_id, reply_to)

    async def get_dm_history(self, max_id: str = None) -> Result[Message]:
        """
        Retrieves the DM conversation history with the user.

        Parameters
        ----------
        max_id : :class:`str`, default=None
            If specified, retrieves messages older than the specified max_id.

        Returns
        -------
        Result[:class:`Message`]
            A Result object containing a list of Message objects representing
            the DM conversation history.

        Examples
        --------
        >>> messages = await user.get_dm_history()
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
        return await self._client.get_dm_history(self.id, max_id)

    async def get_highlights_tweets(self, count: int = 20, cursor: str | None = None) -> Result[Tweet]:
        """
        Retrieves highlighted tweets from the user's timeline.

        Parameters
        ----------
        count : :class:`int`, default=20
            The number of tweets to retrieve.

        Returns
        -------
        Result[:class:`Tweet`]
            An instance of the `Result` class containing the highlighted tweets.

        Examples
        --------
        >>> result = await user.get_highlights_tweets()
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
        return await self._client.get_user_highlights_tweets(self.id, count, cursor)

    async def update(self) -> None:
        new = await self._client.get_user_by_id(self.id)
        self.__dict__.update(new.__dict__)

    def __repr__(self) -> str:
        return f'<User id="{self.id}">'

    def __eq__(self, __value: object) -> bool:
        return isinstance(__value, User) and self.id == __value.id

    def __ne__(self, __value: object) -> bool:
        return not self == __value
