from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from .errors import NotFound
from .utils import subobject, timestamp_to_datetime

if TYPE_CHECKING:
    from httpx import Response

    from .client.client import Client
    from .tweet import Tweet
    from .user import User
    from .utils import Result


class List:
    """
    Class representing a Twitter List.

    Attributes
    ----------
    id : :class:`str`
        The unique identifier of the List.
    created_at : :class:`int`
        The timestamp when the List was created.
    default_banner : :class:`dict`
        Information about the default banner of the List.
    banner : :class:`dict`
        Information about the banner of the List. If custom banner is not set,
        it defaults to the default banner.
    description : :class:`str`
        The description of the List.
    following : :class:`bool`
        Indicates if the authenticated user is following the List.
    is_member : :class:`bool`
        Indicates if the authenticated user is a member of the List.
    member_count : :class:`int`
        The number of members in the List.
    mode : {'Private', 'Public'}
        The mode of the List, either 'Private' or 'Public'.
    muting : :class:`bool`
        Indicates if the authenticated user is muting the List.
    name : :class:`str`
        The name of the List.
    pinning : :class:`bool`
        Indicates if the List is pinned.
    subscriber_count : :class:`int`
        The number of subscribers to the List.
    """
    def __init__(self, client: Client, data: dict) -> None:
        self._client = client

        # X returns an empty object for a list that does not exist, so
        # indexing straight through raised KeyError('created_at') instead of
        # something a caller can act on.
        #
        # A deleted or unknown id gets a shell back that still carries the id
        # it was asked about and nothing else - measured on a list deleted
        # seconds earlier, and on the id "1". Accepting it produced a List
        # whose name was None and whose member_count was 0, which a caller
        # cannot tell apart from a real, empty list. `name` is the marker:
        # every list X actually resolves has one.
        if not data.get('id_str') or data.get('name') is None:
            raise NotFound('The list does not exist.')

        self.id: str = data['id_str']
        self.created_at: int = data.get('created_at')
        self.default_banner: dict = subobject(
            data, 'default_banner_media'
        ).get('media_info')

        if 'custom_banner_media' in data:
            self.banner: dict = data["custom_banner_media"]["media_info"]
        else:
            self.banner: dict = self.default_banner

        # A list X cannot resolve comes back with an id and nothing else, so
        # every one of these used to raise a bare KeyError.
        self.description: str = data.get('description')
        self.following: bool = data.get('following', False)
        self.is_member: bool = data.get('is_member', False)
        self.member_count: int = data.get('member_count', 0)
        self.mode: Literal['Private', 'Public'] = data.get('mode')
        self.muting: bool = data.get('muting', False)
        self.name: str = data.get('name')
        self.pinning: bool = data.get('pinning', False)
        self.subscriber_count: int = data.get('subscriber_count', 0)

    @property
    def created_at_datetime(self) -> datetime:
        # Lists carry `created_at` as epoch milliseconds, not the
        # "Wed Oct 10 20:19:24 +0000 2018" string tweets and users use, so the
        # shared parser raised TypeError on every list.
        if self.created_at is None:
            return None
        if isinstance(self.created_at, str):
            return timestamp_to_datetime(self.created_at)
        # X has shipped this as seconds and as milliseconds at different
        # times; anything past ~5138 AD in seconds is really milliseconds.
        seconds = self.created_at
        if seconds > 100_000_000_000:
            seconds /= 1000
        return datetime.fromtimestamp(seconds, tz=timezone.utc)

    async def edit_banner(self, media_id: str) -> Response:
        """
        Edit the banner image of the list.

        Parameters
        ----------
        media_id : :class:`str`
            The ID of the media to use as the new banner image.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        Examples
        --------
        >>> media_id = await client.upload_media('image.png')
        >>> await media.edit_banner(media_id)
        """
        return await self._client.edit_list_banner(self.id, media_id)

    async def delete_banner(self) -> Response:
        """
        Deletes the list banner.
        """
        return await self._client.delete_list_banner(self.id)

    async def edit(
        self,
        name: str | None = None,
        description: str | None = None,
        is_private: bool | None = None
    ) -> List:
        """
        Edits list information.

        Parameters
        ----------
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
        >>> await list.edit(
        ...     'new name', 'new description', True
        ... )
        """
        return await self._client.edit_list(
            self.id, name, description, is_private
        )

    async def add_member(self, user_id: str) -> Response:
        """
        Adds a member to the list.
        """
        return await self._client.add_list_member(self.id, user_id)

    async def remove_member(self, user_id: str) -> Response:
        """
        Removes a member from the list.
        """
        return await self._client.remove_list_member(self.id, user_id)

    async def get_tweets(
        self, count: int = 20, cursor: str | None = None
    ) -> Result[Tweet]:
        """
        Retrieves tweets from the list.

        Parameters
        ----------
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
        >>> tweets = await list.get_tweets()
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
        return await self._client.get_list_tweets(self.id, count, cursor)

    async def get_members(
        self, count: int = 20, cursor: str | None = None
    ) -> Result[User]:
        """Retrieves members of the list.

        Parameters
        ----------
        count : :class:`int`, default=20
            Number of members to retrieve.

        Returns
        -------
        Result[:class:`User`]
            Members of the list

        Examples
        --------
        >>> members = list_.get_members()
        >>> for member in members:
        ...     print(member)
        <User id="...">
        <User id="...">
        ...
        ...
        >>> more_members = members.next()  # Retrieve more members
        """
        return await self._client.get_list_members(self.id, count, cursor)

    async def get_subscribers(
        self, count: int = 20, cursor: str | None = None
    ) -> Result[User]:
        """Retrieves subscribers of the list.

        Parameters
        ----------
        count : :class:`int`, default=20
            Number of subscribers to retrieve.

        Returns
        -------
        Result[:class:`User`]
            Subscribers of the list

        Examples
        --------
        >>> subscribers = list_.get_subscribers()
        >>> for subscriber in subscribers:
        ...     print(subscriber)
        <User id="...">
        <User id="...">
        ...
        ...
        >>> more_subscribers = subscribers.next()  # Retrieve more subscribers
        """
        return await self._client.get_list_subscribers(self.id, count, cursor)

    async def update(self) -> None:
        new = await self._client.get_list(self.id)
        self.__dict__.update(new.__dict__)

    def __eq__(self, __value: object) -> bool:
        return isinstance(__value, List) and self.id == __value.id

    def __ne__(self, __value: object) -> bool:
        return not self == __value

    def __repr__(self) -> str:
        return f'<List id="{self.id}">'
