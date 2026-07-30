from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from httpx import Response

    from .client.client import Client


class Conversation:
    """
    Represents a direct message conversation in the inbox.

    Attributes
    ----------
    id : :class:`str`
        The conversation ID. For a one-to-one conversation this is the two
        participant ids sorted and joined with a dash - not
        ``partner-you``, so the partner cannot be read off its position; use
        :attr:`partner_id`. For a group it is a plain ID.
    type : :class:`str`
        ``ONE_TO_ONE`` for a one-to-one conversation and ``GROUP_DM`` for a
        group - X does not use the bare word ``GROUP``. Test with
        :attr:`is_group` rather than comparing this string.
    name : :class:`str` | None
        The group name, or None for a one-to-one conversation.
    participant_ids : list[:class:`str`]
        IDs of everyone in the conversation. A conversation with yourself
        lists you once.
    partner_id : :class:`str` | None
        The other participant of a one-to-one conversation, or your own id
        for a conversation with yourself. None for a group.
    last_read_event_id : :class:`str` | None
        The ID of the last event marked as read.
    trusted : :class:`bool`
        Whether the conversation sits in the trusted inbox rather than in
        message requests.
    muted : :class:`bool`
        Whether the conversation is muted.
    is_group : :class:`bool`
        Whether this is a group conversation.

    See Also
    --------
    .Client.get_dm_inbox
    """
    def __init__(
        self, client: Client, data: dict, my_id: str | None = None
    ) -> None:
        self._client = client
        self._data = data

        self.id: str = data['conversation_id']
        self.type: str = data.get('type')
        self.name: str | None = data.get('name')
        self.participant_ids: list[str] = [
            p.get('user_id') for p in data.get('participants', [])
        ]
        self.last_read_event_id: str | None = data.get('last_read_event_id')
        self.partner_id: str | None = None
        self.trusted: bool = data.get('trusted', False)
        self.muted: bool = data.get('muted', False)
        # X labels group conversations GROUP_DM, not GROUP - matching the
        # bare word silently treats every group as one-to-one.
        self.is_group: bool = str(self.type).startswith('GROUP')
        # The conversation id is the two ids sorted, not "partner-you", so
        # the partner cannot be read off its position - compare against our
        # own id instead.
        if my_id is not None and not self.is_group:
            # A conversation with yourself lists exactly one participant - you -
            # so there is no "other" id to find and the lookup came back None,
            # which then addressed the conversation "None-<your id>".
            self.partner_id = next(
                (p for p in self.participant_ids if p != my_id), my_id
            )

    async def get_history(self, max_id: str | None = None):
        """
        Retrieves the message history of this conversation.

        See Also
        --------
        .Client.get_dm_history
        .Client.get_group_dm_history
        """
        # get_dm_history takes the *partner's* user id and assembles the
        # conversation id itself, so handing it the id we already have would
        # build a nonexistent one. Route by conversation type instead.
        if self.is_group:
            return await self._client.get_group_dm_history(self.id, max_id)
        return await self._client.get_dm_history(self.partner_id, max_id)

    def __repr__(self) -> str:
        return f'<Conversation id="{self.id}">'

    def __eq__(self, other) -> bool:
        return isinstance(other, Conversation) and self.id == other.id

    def __ne__(self, other) -> bool:
        return not self == other


class Message:
    """
    Represents a direct message.

    Attributes
    ----------
    id : :class:`str`
        The ID of the message.
    time : :class:`str`
        The timestamp of the message.
    text : :class:`str`
        The text content of the message.
    attachment : :class:`dict`
        Attachment Information.
    attachment_url : :class:`str` | None
        Direct media URL of the attachment. It is not publicly fetchable -
        use :func:`download_attachment`.
    reply_data : :class:`dict` | None
        The message this one replies to, or None for a new message.
    replied_to_id : :class:`str` | None
        The ID of the message this one replies to.
    replied_to_text : :class:`str` | None
        The text of the message this one replies to.
    """
    def __init__(
        self,
        client: Client,
        data: dict,
        sender_id: str,
        recipient_id: str
    ) -> None:
        self._client = client
        self.sender_id = sender_id
        self.recipient_id = recipient_id

        self.id: str = data['id']
        self.time: str = data['time']
        self.text: str = data['text']
        self.attachment: dict | None = data.get('attachment')
        # X carries the message being replied to inline as `reply_data`;
        # nothing read it, so replies were indistinguishable from new messages.
        self.reply_data: dict | None = data.get('reply_data')

    async def download_attachment(self) -> bytes:
        """
        Downloads the media attached to this message.

        The URL X puts in :attr:`attachment` points at ``ton.twitter.com``,
        which is not a public CDN - fetching it without the session cookies
        answers 302 and no bytes. This goes through the logged in client
        instead.

        Returns
        -------
        :class:`bytes`
            The raw media.

        Raises
        ------
        ValueError
            If the message has no attachment.

        Examples
        --------
        >>> data = await message.download_attachment()
        >>> open('image.png', 'wb').write(data)
        """
        url = self.attachment_url
        if url is None:
            raise ValueError('This message has no attachment.')
        # Two things are needed and neither is obvious: the bearer headers
        # (without them the redirect target answers 404) and following the
        # redirect at all (httpx does not by default, so the call would
        # succeed and hand back zero bytes).
        headers = self._client._base_headers
        headers.pop('content-type', None)
        response = await self._client._send(
            'GET', url, headers=headers, follow_redirects=True
        )
        return response.content

    @property
    def attachment_url(self) -> str | None:
        """The direct media URL of the attachment, or None."""
        attachment = self.attachment
        if not isinstance(attachment, dict):
            return None
        for value in attachment.values():
            if isinstance(value, dict):
                url = value.get('media_url_https') or value.get('media_url')
                if url:
                    return url
        return None

    @property
    def replied_to_id(self) -> str | None:
        return (self.reply_data or {}).get('id')

    @property
    def replied_to_text(self) -> str | None:
        return (self.reply_data or {}).get('text')

    async def reply(self, text: str, media_id: str | None = None) -> Message:
        """Replies to the message.

        Parameters
        ----------
        text : :class:`str`
            The text content of the direct message.
        media_id : :class:`str`, default=None
            The media ID associated with any media content
            to be included in the message.
            Media ID can be received by using the :func:`.upload_media` method.

        Returns
        -------
        :class:`Message`
            `Message` object containing information about the message sent.

        See Also
        --------
        Client.send_dm
        """
        user_id = await self._client.user_id()
        send_to = (
            self.recipient_id
            if user_id == self.sender_id else
            self.sender_id
        )
        return await self._client.send_dm(send_to, text, media_id, self.id)

    async def add_reaction(self, emoji: str) -> Response:
        """
        Adds a reaction to the message.

        Parameters
        ----------
        emoji : :class:`str`
            The emoji to be added as a reaction.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.
        """
        user_id = await self._client.user_id()
        partner_id = (
            self.recipient_id
            if user_id == self.sender_id else
            self.sender_id
        )
        conversation_id = f'{partner_id}-{user_id}'
        return await self._client.add_reaction_to_message(
            self.id, conversation_id, emoji
        )

    async def remove_reaction(self, emoji: str) -> Response:
        """
        Removes a reaction from the message.

        Parameters
        ----------
        emoji : :class:`str`
            The emoji to be removed.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.
        """
        user_id = await self._client.user_id()
        partner_id = (
            self.recipient_id
            if user_id == self.sender_id else
            self.sender_id
        )
        conversation_id = f'{partner_id}-{user_id}'
        return await self._client.remove_reaction_from_message(
            self.id, conversation_id, emoji
        )

    async def delete(self) -> Response:
        """
        Deletes the message.

        Returns
        -------
        :class:`httpx.Response`
            Response returned from twitter api.

        See Also
        --------
        Client.delete_dm
        """
        return await self._client.delete_dm(self.id)

    def __eq__(self, __value: object) -> bool:
        return isinstance(__value, Message) and self.id == __value.id

    def __ne__(self, __value: object) -> bool:
        return not self == __value

    def __repr__(self) -> str:
        return f'<Message id="{self.id}">'
