from __future__ import annotations

from typing import TYPE_CHECKING, Literal, NamedTuple

from .tweet import Tweet
from .user import User
from .errors import NotFound
from .utils import Result, b64_to_str, subobject

if TYPE_CHECKING:
    from .client.client import Client


class CommunityCreator(NamedTuple):
    id: str
    screen_name: str
    verified: bool


class CommunityRule(NamedTuple):
    id: str
    name: str


class CommunityMember:
    def __init__(self, client: Client, data: dict) -> None:
        self._client = client
        # A member entry X could not resolve arrives without rest_id.
        if not data.get('rest_id'):
            raise NotFound('The community member does not exist.')
        self.id: str = data['rest_id']

        self.community_role: str = data.get('community_role')
        self.super_following: bool = data.get('super_following', False)
        self.super_follow_eligible: bool = data.get('super_follow_eligible', False)
        self.super_followed_by: bool = data.get('super_followed_by', False)
        self.smart_blocking: bool = data.get('smart_blocking', False)
        self.is_blue_verified: bool = data.get('is_blue_verified', False)

        # The current documents drop `legacy` and split the same fields across
        # typed objects, so both shapes have to be read.
        legacy = subobject(data, 'legacy')
        core = subobject(data, 'core')
        avatar = subobject(data, 'avatar')
        privacy = subobject(data, 'privacy')
        verification = subobject(data, 'verification')
        relationship = subobject(data, 'relationship_perspectives')

        self.screen_name: str = core.get('screen_name') or legacy.get('screen_name')
        self.name: str = core.get('name') or legacy.get('name')
        self.follow_request_sent: bool = data.get(
            'follow_request_sent', legacy.get('follow_request_sent', False))
        self.protected: bool = privacy.get('protected', legacy.get('protected', False))
        self.following: bool = relationship.get('following', legacy.get('following', False))
        self.followed_by: bool = relationship.get('followed_by', legacy.get('followed_by', False))
        self.blocking: bool = relationship.get('blocking', legacy.get('blocking', False))
        self.profile_image_url_https: str = (
            avatar.get('image_url') or legacy.get('profile_image_url_https'))
        self.verified: bool = verification.get('verified', legacy.get('verified', False))

    def __eq__(self, __value: object) -> bool:
        return isinstance(__value, CommunityMember) and self.id == __value.id

    def __ne__(self, __value: object) -> bool:
        return not self == __value

    def __repr__(self) -> str:
        return f'<CommunityMember id="{self.id}">'


class Community:
    """
    Attributes
    ----------
    id : :class:`str`
        The ID of the community.
    name : :class:`str`
        The name of the community.
    member_count : :class:`int`
        The count of members in the community.
    is_nsfw : :class:`bool`
        Indicates if the community is NSFW.
    members_facepile_results : list[:class:`str`]
        The profile image URLs of members.
    banner : :class:`dict`
        The banner information of the community.
    is_member : :class:`bool`
        Indicates if the user is a member of the community.
    role : :class:`str`
        The role of the user in the community.
    description : :class:`str`
        The description of the community.
    creator : :class:`User` | :class:`CommunityCreator`
        The creator of the community.
    admin : :class:`User`
        The admin of the community.
    join_policy : :class:`str`
        The join policy of the community.
    created_at : :class:`int`
        The timestamp of the community's creation.
    invites_policy : :class:`str`
        The invites policy of the community.
    is_pinned : :class:`bool`
        Indicates if the community is pinned.
    rules : list[:class:`CommunityRule`]
        The rules of the community.
    """

    def __init__(self, client: Client, data: dict) -> None:
        self._client = client
        # X answers an unknown community id with an empty result, so this
        # raised KeyError('rest_id') rather than telling the caller anything.
        if not data.get('rest_id'):
            raise NotFound('The community does not exist.')
        self.id: str = data['rest_id']

        self.name: str = data.get('name')
        self.member_count: int = data.get('member_count', 0)
        self.is_nsfw: bool = data.get('is_nsfw', False)

        self.members_facepile_results: list[str] = [
            subobject(i['result'], 'avatar').get('image_url')
            or subobject(i['result'], 'legacy').get('profile_image_url_https')
            for i in data['members_facepile_results']
        ]
        self.banner: dict = data['default_banner_media']['media_info']

        self.is_member: bool = data.get('is_member')
        self.role: str = data.get('role')
        self.description: str = data.get('description')

        if 'creator_results' in data:
            creator = data['creator_results']['result']
            if 'rest_id' in creator:
                self.creator = User(client, creator)
            else:
                self.creator = CommunityCreator(
                    b64_to_str(creator['id']).removeprefix('User:'),
                    subobject(creator, 'core').get('screen_name')
                    or subobject(creator, 'legacy').get('screen_name'),
                    subobject(creator, 'verification').get(
                        'verified',
                        subobject(creator, 'legacy').get('verified', False))
                )
        else:
            self.creator = None

        if 'admin_results' in data:
            admin = data['admin_results']['result']
            self.admin = User(client, admin)
        else:
            self.admin = None

        self.join_policy: str = data.get('join_policy')
        self.created_at: int = data.get('created_at')
        self.invites_policy: str = data.get('invites_policy')
        self.is_pinned: bool = data.get('is_pinned')

        if 'rules' in data:
            self.rules: list = [
                CommunityRule(i['rest_id'], i['name']) for i in data['rules']
            ]
        else:
            self.rules = None

    async def get_tweets(
        self,
        tweet_type: Literal['Top', 'Latest', 'Media'],
        count: int = 40,
        cursor: str | None = None
    ) -> Result[Tweet]:
        """
        Retrieves tweets from the community.

        Parameters
        ----------
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
        >>> tweets = await community.get_tweets('Latest')
        >>> for tweet in tweets:
        ...     print(tweet)
        <Tweet id="...">
        <Tweet id="...">
        ...
        >>> more_tweets = await tweets.next()  # Retrieve more tweets
        """
        return await self._client.get_community_tweets(
            self.id,
            tweet_type,
            count,
            cursor
        )

    async def join(self) -> Community:
        """
        Join the community.
        """
        return await self._client.join_community(self.id)

    async def leave(self) -> Community:
        """
        Leave the community.
        """
        return await self._client.leave_community(self.id)

    async def request_to_join(self, answer: str | None = None) -> Community:
        """
        Request to join the community.
        """
        return await self._client.request_to_join_community(self.id, answer)

    async def get_members(
        self, count: int = 20, cursor: str | None = None
    ) -> Result[CommunityMember]:
        """
        Retrieves members of the community.

        Parameters
        ----------
        count : :class:`int`, default=20
            The number of members to retrieve.

        Returns
        -------
        Result[:class:`CommunityMember`]
            List of retrieved members.
        """
        return await self._client.get_community_members(
            self.id,
            count,
            cursor
        )

    async def get_moderators(
        self, count: int = 20, cursor: str | None = None
    ) -> Result[CommunityMember]:
        """
        Retrieves moderators of the community.

        Parameters
        ----------
        count : :class:`int`, default=20
            The number of moderators to retrieve.

        Returns
        -------
        Result[:class:`CommunityMember`]
            List of retrieved moderators.
        """
        return await self._client.get_community_moderators(
            self.id,
            count,
            cursor
        )

    async def search_tweet(
        self,
        query: str,
        count: int = 20,
        cursor: str | None = None
    )-> Result[Tweet]:
        """Searchs tweets in the community.

        Parameters
        ----------
        query : :class:`str`
            The search query.
        count : :class:`int`, default=20
            The number of tweets to retrieve.

        Returns
        -------
        Result[:class:`Tweet`]
            List of retrieved tweets.
        """
        return await self._client.search_community_tweet(
            self.id,
            query,
            count,
            cursor
        )

    async def update(self) -> None:
        new = await self._client.get_community(self.id)
        self.__dict__.update(new.__dict__)

    def __eq__(self, __value: object) -> bool:
        return isinstance(__value, Community) and self.id == __value.id

    def __ne__(self, __value: object) -> bool:
        return not self == __value

    def __repr__(self) -> str:
        return f'<Community id="{self.id}">'
