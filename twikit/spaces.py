"""
X Spaces support for twifork.

This module implements the full X Spaces stack on top of the undocumented
web-client APIs, reverse-engineered from the current web bundle (2026-08):

* Metadata / discovery      — GraphQL (AudioSpaceById, AudioSpaceSearch,
                               BrowseSpaceTopics, ...)
* Broadcast lifecycle       — proxsee.pscp.tv (Periscope) v2 API
                               (createBroadcast, publishBroadcast, endBroadcast)
* Control plane (mute,      — guest-cf.pscp.tv /api/v1 (chatman): join,
  settings, end, approve…)    audiospace/* endpoints
* Chat                      — proxsee accessChat(Public) → chatapi v1
                               (HTTP history + WebSocket)
* Audio (speak & listen)    — Janus WebRTC videoroom (janus.plugin.videoroom),
                               TURN servers from proxsee turnServers,
                               aiortc for the peer connection (optional)

None of this requires an API key; it uses the same cookie-authenticated
session as the rest of twifork.
"""

from __future__ import annotations

import asyncio
import json
import random
import string
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

if TYPE_CHECKING:
    from .client.client import Client

# ---------------------------------------------------------------------------
# Constants (from the web bundle)
# ---------------------------------------------------------------------------

PROXSEE_HOST = 'https://proxsee-cf.pscp.tv'
CHATMAN_HOST = 'https://guest-cf.pscp.tv'
PERISCOPE_VENDOR_ID = 'm5-proxsee-login-a2011357b73e'

#: Default metadata for a new Space (web client's `createBroadcast` payload).
DEFAULT_SPACE_METADATA = {
    'app_component': 'audio-room',
    'content_type': 'visual_audio',
    'conversation_controls': 0,   # 0=request/approval needed to speak, 2=everyone (measured)
    'description': '',
    'height': 1080,
    'is_360': False,
    'is_space_available_for_clipping': False,
    'is_space_available_for_replay': False,
    'is_webrtc': True,
    'languages': [],
    'narrow_cast_space_type': 0,  # 0=all, 1=employees, 2=subscribers
    'region': 'us-west-1',
    'replaykit_app_bundle': '',
    'replaykit_app_name': '',
    'requires_psp_version': [],
    'scheduled_start_time': 0,
    'source': '',
    'ticket_group_id': '',
    'tickets_total': 0,
    'topics': [],
    'width': 1920,
}

#: Default payload proxsee expects on publishBroadcast.
DEFAULT_PUBLISH_PAYLOAD = {
    'accept_guests': True,
    'bit_rate': 0,
    'camera_rotation': 0,
    'has_location': False,
    'invitees_twitter': [],
    'locale': 'en-us',
    'lock': [],
    'lock_private_channels': [],
    'topics': [],
    'lat': 0,
    'lng': 0,
    'friend_chat': False,
    'private_chat': False,
    'enable_sparkles': False,
    'hidden': False,
}


class SpaceState:
    RUNNING = 'Running'
    SCHEDULED = 'Scheduled'
    ENDED = 'Ended'
    TIMED_OUT = 'TimedOut'


class SpaceRole:
    HOST = 'host'
    COHOST = 'cohost'
    SPEAKER = 'speaker'
    LISTENER = 'listener'


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Space:
    """A single X Space (wraps the AudioSpaceById `audioSpace` payload)."""

    def __init__(self, data: dict):
        self.data = data
        self.metadata = data.get('metadata') or {}
        self.participants = data.get('participants') or {}
        self.host = data.get('host') or {}
        self.chat = data.get('chat') or {}
        self.tweet = data.get('tweet') or {}
        self.broadcast = data.get('broadcast') or {}
        self.sharings = (data.get('sharings') or {}).get('items') or []

    @property
    def sharing_ids(self) -> list[str]:
        """IDs of the tweet shares on this Space (for delete_sharing)."""
        return [s.get('sharing_id') for s in self.sharings if s.get('sharing_id')]

    @property
    def id(self) -> str | None:
        return self.metadata.get('rest_id')

    @property
    def state(self) -> str | None:
        return self.metadata.get('state')

    @property
    def title(self) -> str | None:
        return self.metadata.get('title')

    @property
    def media_key(self) -> str | None:
        return self.metadata.get('media_key')

    @property
    def is_live(self) -> bool:
        return self.state == SpaceState.RUNNING

    @property
    def is_scheduled(self) -> bool:
        return self.state == SpaceState.SCHEDULED

    @property
    def is_available_for_replay(self) -> bool:
        return bool(self.metadata.get('is_space_available_for_replay'))

    @property
    def created_at(self) -> int | None:
        return self.metadata.get('created_at')

    @property
    def started_at(self) -> int | None:
        return self.metadata.get('started_at')

    @property
    def scheduled_start(self) -> int | None:
        return self.metadata.get('scheduled_start')

    @property
    def speaker_ids(self) -> list[str]:
        return [
            s.get('user_results', {}).get('result', {}).get('rest_id')
            for s in (self.participants.get('speakers') or [])
            if s.get('user_results', {}).get('result', {}).get('rest_id')
        ]

    @property
    def host_user_id(self) -> str | None:
        return (
            self.host.get('user_results', {}).get('result', {}).get('rest_id')
            or (self.host or {}).get('rest_id')
        )

    def __repr__(self):
        return (
            f'Space(id={self.id!r}, state={self.state!r}, '
            f'title={self.title!r})'
        )


@dataclass
class SpaceStream:
    """Parsed `live_video_stream/status/{media_key}` response."""

    raw: dict
    session_id: str | None = None
    source_location: str | None = None
    no_redirect_playback_url: str | None = None
    chat_token: str | None = None
    stream_type: str | None = None

    @property
    def hls_url(self) -> str | None:
        """Best playable HLS URL (m3u8), if the stream is HLS-based."""
        return (
            self.no_redirect_playback_url
            or self.source_location
        )

    @classmethod
    def from_response(cls, data: dict) -> 'SpaceStream':
        source = data.get('source') or {}
        return cls(
            raw=data,
            session_id=data.get('session_id'),
            source_location=source.get('location'),
            no_redirect_playback_url=source.get('noRedirectPlaybackUrl'),
            chat_token=data.get('chatToken'),
            stream_type=source.get('stream_type'),
        )


@dataclass
class ChatMessage:
    """A parsed chatapi v1 message."""

    raw: dict
    type: str
    sender: dict | None = None
    body: str | None = None
    timestamp: int | None = None
    session_uuid: str | None = None
    kind: str | None = None

    @classmethod
    def from_payload(cls, raw: dict) -> 'ChatMessage':
        payload = raw.get('payload')
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        body = payload.get('body')
        text = None
        kind = None
        parsed_body = None
        if isinstance(body, str):
            try:
                parsed_body = json.loads(body)
            except json.JSONDecodeError:
                parsed_body = None
            if isinstance(parsed_body, dict):
                # Normal chat messages also encode their body as a JSON
                # object.  Only objects without a textual payload are
                # control/system events.
                text = (
                    parsed_body.get('body')
                    or parsed_body.get('text')
                    or parsed_body.get('message')
                )
                if text is not None:
                    kind = parsed_body.get('type') or 'text'
                else:
                    kind = parsed_body.get('type') or 'event'
            else:
                text = body
                kind = 'text'
        elif isinstance(body, dict):
            text = (
                body.get('text')
                or body.get('body')
                or body.get('message')
            )
            kind = body.get('kind') or body.get('type') or 'text'
        return cls(
            raw=raw,
            type=raw.get('type')
            or payload.get('type')
            or ('message' if raw.get('kind') in (1, 2) else 'message'),
            sender=payload.get('sender') or raw.get('sender'),
            body=str(text) if text is not None else None,
            timestamp=raw.get('timestamp') or payload.get('timestamp'),
            session_uuid=(
                payload.get('session_uuid')
                or (
                    parsed_body.get('session_uuid')
                    if isinstance(parsed_body, dict) else None
                )
            ),
            kind=kind,
        )


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

def _idempotence_header() -> dict:
    """X-Periscope-User-Agent / X-Idempotence headers used by the web client."""
    return {
        'X-Periscope-User-Agent': 'Twitter/m5',
        'X-Attempt': '1',
        'X-Idempotence': ''.join(
            random.choices(string.ascii_letters + string.digits, k=64)
        ),
        'Content-Type': 'application/json',
    }


def _ntp_metadata() -> dict:
    # The web client builds these from the UTC day-of-month; replicated as-is.
    day = time.gmtime().tm_mday
    return {
        'ntpForBroadcasterFrame': int(1e9 * day),
        'ntpForLiveFrame': int(1e9 * day),
    }


class _Http:
    """Shared async HTTP client for proxsee / chatman / janus calls."""

    def __init__(self, timeout: float = 30.0):
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def post(
        self,
        url: str,
        json_data: dict | None = None,
        headers: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        client = await self._get_client()
        resp = await client.post(
            url, json=json_data or {}, headers=headers, params=params
        )
        return self._check(resp, url)

    async def get(
        self,
        url: str,
        headers: dict | None = None,
        params: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        client = await self._get_client()
        resp = await client.get(
            url, headers=headers, params=params,
            timeout=timeout or self._timeout
        )
        return self._check(resp, url)

    @staticmethod
    def _check(resp: httpx.Response, url: str) -> dict:
        if resp.status_code >= 400:
            raise SpaceError(
                f'{url} returned HTTP {resp.status_code}: '
                f'{resp.text[:300]}'
            )
        try:
            return resp.json()
        except json.JSONDecodeError:
            raise SpaceError(
                f'{url} returned non-JSON body: {resp.text[:300]}'
            )

    async def aclose(self):
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


class SpaceError(Exception):
    """Raised for any Spaces API failure."""


class ProxseeAuthError(SpaceError):
    """Raised when the Periscope login flow fails."""


# ---------------------------------------------------------------------------
# Proxsee API (broadcast lifecycle + chat + TURN)
# ---------------------------------------------------------------------------

class ProxseeApi:
    """
    Client for the Periscope (proxsee.pscp.tv) v2 API.

    Authentication is two-step: ``authenticatePeriscope`` (X GraphQL) gives a
    JWT, ``loginTwitterToken`` exchanges it for a proxsee session cookie.
    """

    def __init__(self, client: 'Client'):
        self._client = client
        self._http = _Http()
        self._token: str | None = None
        self._user: dict | None = None
        self._user_type: str | None = None

    # -- auth --------------------------------------------------------------

    async def login(self, create_user: bool = False) -> None:
        """Exchange an AuthenticatePeriscope JWT for a proxsee session."""
        if self._token:
            return
        data, _ = await self._client.gql.authenticate_periscope()
        data = data.get('data') or {}
        # The web client returns the JWT directly under the snake_case field.
        token = data.get('authenticate_periscope')
        if isinstance(token, dict):
            token = token.get('token')
        if not token:
            raise ProxseeAuthError(
                f'authenticatePeriscope returned no token: {data}'
            )
        payload = {
            'jwt': token,
            'vendor_id': PERISCOPE_VENDOR_ID,
            'create_user': create_user,
        }
        # The bare path (no `twitter/` prefix) is what both the web client and
        # the current proxsee API expect. Browser-ish headers are required;
        # without them proxsee answers 400. Do NOT send `direct` — its
        # presence breaks the request (400) even though the web bundle still
        # carries it.
        headers = self._headers()
        resp = await self._http.post(
            f'{PROXSEE_HOST}/api/v2/loginTwitterToken',
            json_data=payload,
            headers=headers,
        )
        cookie = resp.get('cookie')
        if not cookie:
            raise ProxseeAuthError(
                f'loginTwitterToken returned no cookie: {resp}'
            )
        self._token = cookie
        self._user_type = resp.get('type')
        self._user = resp.get('user') or {}

    def _body(self, payload: dict) -> dict:
        if self._token is None:
            raise ProxseeAuthError('not logged in; call login() first')
        return {**payload, 'cookie': self._token}

    def _headers(self) -> dict:
        """proxsee requires browser-ish headers on every call, not just the
        login one (plain headers get 401/400 on turnServers etc.)."""
        return {
            **_idempotence_header(),
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/131.0.0.0 Safari/537.36'
            ),
            'Referer': 'https://x.com/',
            'sec-ch-ua': (
                '"Google Chrome";v="131", "Chromium";v="131", '
                '"Not_A Brand";v="24"'
            ),
            'sec-ch-ua-platform': '"Windows"',
            'sec-ch-ua-mobile': '?0',
        }

    async def post(self, endpoint: str, payload: dict) -> dict:
        await self.login()
        # The proxsee API serves every endpoint under the bare `/api/v2/`
        # prefix. The web bundle builds a `twitter/` prefix from the stored
        # userType, but current responses carry no `type` field, so userType
        # stays unset and every call goes to the bare path — the prefixed
        # variants 404/401 (turnServers is the measured example).
        return await self._http.post(
            f'{PROXSEE_HOST}/api/v2/{endpoint}',
            json_data=self._body(payload),
            headers=self._headers(),
        )

    @property
    def periscope_user_id(self) -> str | None:
        return (self._user or {}).get('id')

    @property
    def twitter_screen_name(self) -> str | None:
        return (self._user or {}).get('twitter_screen_name')

    @property
    def twitter_id(self) -> str | None:
        return (self._user or {}).get('twitter_id')

    @property
    def display_name(self) -> str | None:
        return (self._user or {}).get('display_name')

    # -- broadcast lifecycle -----------------------------------------------

    async def create_broadcast(self, metadata: dict) -> dict:
        """Create a broadcast. Returns the full response (broadcast,
        access_token, room_id, stream_name, credential, webrtc_gw_url, …)."""
        return await self.post('createBroadcast', metadata)

    async def publish_broadcast(self, extra: dict | None = None) -> dict:
        return await self.post(
            'publishBroadcast', {**DEFAULT_PUBLISH_PAYLOAD, **(extra or {})}
        )

    async def end_broadcast(self, broadcast_id: str) -> dict:
        return await self.post('endBroadcast', {'broadcast_id': broadcast_id})

    async def pre_publish_scheduled_audio_broadcast(
        self, broadcast_id: str
    ) -> dict:
        return await self.post('prePublishScheduledAudioBroadcast', {
            'broadcast_id': broadcast_id
        })

    async def cancel_scheduled_space(self, broadcast_id: str) -> dict:
        return await self.post('cancelScheduledAudioBroadcast', {
            'broadcast_id': broadcast_id
        })

    async def get_scheduled_spaces(self) -> dict:
        return await self.post('getScheduledAudioBroadcasts', {})

    async def reconnect_host(self, broadcast_id: str) -> dict:
        return await self.post('reconnectHost', {'broadcast_id': broadcast_id})

    async def associate_tweet_with_broadcast(
        self, broadcast_id: str, tweet_id: str, tweet_external: bool = False
    ) -> dict:
        return await self.post('associateTweetWithBroadcast', {
            'broadcast_id': broadcast_id,
            'tweet_id': tweet_id,
            'tweet_external': tweet_external,
        })

    # -- chat / media ------------------------------------------------------

    async def access_chat(self, chat_token: str) -> dict:
        """Exchange a chat token for {endpoint, room_id, access_token}."""
        return await self.post('accessChat', {'chat_token': chat_token})

    async def access_chat_public(self, chat_token: str) -> dict:
        return await self.post('accessChatPublic', {'chat_token': chat_token})

    async def get_chat_history(
        self,
        endpoint: str,
        access_token: str,
        cursor: str | None = None,
        limit: int = 1000,
        since: Any = None,
        quick_get: bool = True,
    ) -> dict:
        payload = {
            'access_token': access_token,
            'cursor': cursor,
            'limit': limit,
            'since': since,
            'quick_get': quick_get,
        }
        return await self._http.post(
            f'{endpoint}/chatapi/v1/history', json_data=payload
        )

    async def get_turn_servers(self) -> dict:
        """Returns {uris, username, password} (TURN servers for WebRTC)."""
        return await self.post('turnServers', {})

    async def get_token_for_service(self, service: str) -> dict:
        return await self.post('authorizeToken', {'service': service})

    async def webrtc_broadcast_meta(self, payload: dict) -> dict:
        return await self.post('webrtcBroadcastMeta', payload)

    async def webrtc_playback_meta(self, payload: dict) -> dict:
        return await self.post('webrtcPlaybackMeta', payload)


# ---------------------------------------------------------------------------
# Chatman API (control plane on guest-cf.pscp.tv)
# ---------------------------------------------------------------------------

class ChatmanApi:
    """
    Client for the chatman control plane (``/api/v1`` on guest-cf.pscp.tv).

    Every request is authenticated with a guest-service token obtained from
    proxsee ``authorizeToken`` (service=guest) and carries the chat token of
    the Space in the body.
    """

    def __init__(self, client: 'Client'):
        self._client = client
        self._http = _Http()
        self._proxsee = ProxseeApi(client)
        self._guest_service_token: str | None = None
        self._chat_access_token: str | None = None
        self._initialized = False

    async def initialize(self, chat_access_token: str | None = None) -> None:
        if self._initialized:
            # The guest-service authorization is account-scoped, but the
            # chat access token changes when entering/reconnecting a Space.
            if chat_access_token is not None:
                self._chat_access_token = chat_access_token
            return
        resp = await self._proxsee.get_token_for_service('guest')
        token = resp.get('authorization_token')
        if not token:
            raise SpaceError(
                f'authorizeToken returned no token: {resp}'
            )
        self._guest_service_token = token
        if chat_access_token is not None:
            self._chat_access_token = chat_access_token
        self._initialized = True

    async def _post(self, endpoint: str, payload: dict) -> dict:
        if not self._initialized:
            raise SpaceError(
                'ChatmanApi not initialized; call initialize() first'
            )
        body = {**payload, 'chat_token': self._chat_access_token}
        return await self._http.post(
            f'{CHATMAN_HOST}/api/v1/{endpoint}',
            json_data=body,
            headers={'Authorization': self._guest_service_token},
        )

    # -- session -----------------------------------------------------------

    async def join_as_speaker(
        self,
        broadcast_id: str,
        join_as_admin: bool = False,
        should_auto_join: bool = True,
    ) -> dict:
        """Join as speaker. Returns {can_auto_join, session_uuid, …}."""
        return await self._post('audiospace/join', {
            **_ntp_metadata(),
            'broadcast_id': broadcast_id,
            'join_as_admin': join_as_admin,
            'should_auto_join': should_auto_join,
        })

    async def negotiate_stream(self, session_uuid: str) -> dict:
        """Returns Janus credentials: {janus_jwt, webrtc_gw_url, …}."""
        return await self._post('audiospace/stream/negotiate', {
            'session_uuid': session_uuid
        })

    async def publish_stream(self, session_uuid: str, **extra) -> dict:
        return await self._post('audiospace/stream/publish', {
            **_ntp_metadata(),
            'session_uuid': session_uuid,
            **extra,
        })

    async def end_stream(self, session_uuid: str) -> dict:
        return await self._post('audiospace/stream/end', {
            **_ntp_metadata(),
            'session_uuid': session_uuid,
        })

    # -- moderation --------------------------------------------------------

    async def end_audio_space(self, broadcast_id: str) -> dict:
        """End the whole Space (host/admin only)."""
        return await self._post('audiospace/admin/endAudiospace', {
            'broadcast_id': broadcast_id
        })

    async def set_space_settings(
        self,
        broadcast_id: str,
        conversation_controls: int = 0,
        topics: list | None = None,
        mentioned_twitter_user_ids: list | None = None,
    ) -> dict:
        return await self._post(
            'audiospace/admin/setAudiospaceSettings', {
                'broadcast_id': broadcast_id,
                'conversation_controls': conversation_controls,
                'topics': topics or [],
                'mentioned_twitter_user_ids': mentioned_twitter_user_ids or [],
            }
        )

    async def mute_speaker(self, session_uuid: str, broadcast_id: str) -> dict:
        return await self._post('audiospace/muteSpeaker', {
            **_ntp_metadata(),
            'session_uuid': session_uuid,
            'broadcast_id': broadcast_id,
        })

    async def unmute_speaker(
        self, session_uuid: str, broadcast_id: str
    ) -> dict:
        return await self._post('audiospace/unmuteSpeaker', {
            **_ntp_metadata(),
            'session_uuid': session_uuid,
            'broadcast_id': broadcast_id,
        })

    async def mute_space(self, broadcast_id: str) -> dict:
        return await self._post('audiospace/admin/muteSpace', {
            **_ntp_metadata(),
            'broadcast_id': broadcast_id,
        })

    async def unmute_space(self, broadcast_id: str) -> dict:
        return await self._post('audiospace/admin/unmuteSpace', {
            **_ntp_metadata(),
            'broadcast_id': broadcast_id,
        })

    async def raise_hand(
        self, session_uuid: str, broadcast_id: str, emoji: str = '✋'
    ) -> dict:
        return await self._post('audiospace/raiseHand', {
            'session_uuid': session_uuid,
            'broadcast_id': broadcast_id,
            'emoji': emoji,
        })

    async def lower_hand(self, session_uuid: str, broadcast_id: str) -> dict:
        return await self._post('audiospace/lowerHand', {
            'session_uuid': session_uuid,
            'broadcast_id': broadcast_id,
        })

    async def approve_request(self, session_uuid: str) -> dict:
        return await self._post('audiospace/request/approve', {
            **_ntp_metadata(),
            'session_uuid': session_uuid,
        })

    async def reject_request(self, session_uuid: str) -> dict:
        return await self._post('audiospace/request/reject', {
            'session_uuid': session_uuid,
        })

    async def submit_speaker_request(self, broadcast_id: str) -> dict:
        return await self._post('audiospace/request/submit', {
            **_ntp_metadata(),
            'broadcast_id': broadcast_id,
        })

    async def cancel_speaker_request(
        self, broadcast_id: str, session_uuid: str
    ) -> dict:
        return await self._post('audiospace/request/cancel', {
            **_ntp_metadata(),
            'broadcast_id': broadcast_id,
            'session_uuid': session_uuid,
        })

    async def remove_participant(
        self, broadcast_id: str, twitter_user_ids: list[str]
    ) -> dict:
        return await self._post('audiospace/admin/removeParticipant', {
            **_ntp_metadata(),
            'broadcast_id': broadcast_id,
            'twitter_user_ids': twitter_user_ids,
        })

    async def add_admin(
        self, broadcast_id: str, twitter_user_id: str
    ) -> dict:
        return await self._post('audiospace/admin/addAdmin', {
            **_ntp_metadata(),
            'broadcast_id': broadcast_id,
            'twitter_user_id': twitter_user_id,
        })

    async def remove_admin(self, broadcast_id: str, session_uuid: str,
                           twitter_user_id: str) -> dict:
        return await self._post('audiospace/removeAdmin', {
            **_ntp_metadata(),
            'broadcast_id': broadcast_id,
            'session_uuid': session_uuid,
            'twitter_user_id': twitter_user_id,
        })

    async def admin_invite(
        self,
        broadcast_id: str,
        twitter_user_ids: list[str],
        session_uuid: str = '',
    ) -> dict:
        """Register admins (host/cohosts) in the chatman room.

        The web client calls this right after publishBroadcast with the
        host's own twitter id — it is what makes the host show up as the
        space admin (and unmuted) for listeners.
        """
        return await self._post('audiospace/admin/invite', {
            'broadcast_id': broadcast_id,
            'twitter_user_ids': twitter_user_ids,
            'session_uuid': session_uuid,
        })

    async def stream_eject(
        self,
        session_uuid: str,
        *,
        webrtc_handle_id: int,
        webrtc_session_id: int,
        janus_room_id: str,
        janus_participant_id: int,
    ) -> dict:
        """Eject an active publisher from the Janus audio stream.

        X requires both Chatman and Janus identifiers. Sending only the
        session UUID is rejected with HTTP 400.
        """
        return await self._post('audiospace/stream/eject', {
            **_ntp_metadata(),
            'session_uuid': session_uuid,
            'webrtc_handle_id': webrtc_handle_id,
            'webrtc_session_id': webrtc_session_id,
            'janus_room_id': janus_room_id,
            'janus_participant_id': janus_participant_id,
        })

    async def get_call_status(
        self, broadcast_id: str, include_non_active_sessions: bool = True
    ) -> dict:
        return await self._post('audiospace/call/status', {
            'broadcast_id': broadcast_id,
            'include_non_active_sessions': include_non_active_sessions,
        })


# ---------------------------------------------------------------------------
# Janus WebRTC
# ---------------------------------------------------------------------------

def _random_transaction() -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))


class JanusClient:
    """
    Minimal Janus gateway client (janus.plugin.videoroom).

    Mirrors the web client: HTTP long-poll for events, transactions to match
    responses, ``Authorization: <vidManToken>`` on every request. Session and
    handle ids come from ``data.id`` (X's gateway wrapper), plugin messages
    go to ``{url}/{session}/{handle}`` wrapped in
    ``{janus: "message", body: {room, periscope_user_id, ...}}``.
    """

    def __init__(self, janus_url: str, vid_man_token: str, http: _Http,
                 periscope_user_id: str = '', room_id: str = ''):
        self.janus_url = janus_url.rstrip('/')
        self.vid_man_token = vid_man_token
        self._http = http
        self.periscope_user_id = periscope_user_id
        self.room_id = room_id
        self.session_id: int | None = None
        self.handler_id: int | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._poll_task: asyncio.Task | None = None
        # handlers: on_jsep(sdp, event), on_publisher_id(id),
        # on_publishers(list), on_attached(streams), on_raw_event(event)
        self.on_jsep = None
        self.on_publisher_id = None
        self.on_publishers = None
        self.on_attached = None
        self.on_raw_event = None

    def _headers(self) -> dict:
        return {
            'Authorization': self.vid_man_token,
            'Content-Type': 'application/json',
        }

    async def _dispatch(self, payload: dict, suffix: str = '') -> dict:
        transaction = payload.setdefault('transaction', _random_transaction())
        url = f'{self.janus_url}/{suffix}' if suffix else self.janus_url
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[transaction] = future
        try:
            resp = await self._http.post(url, json_data=payload, headers=self._headers())
            if resp.get('janus') == 'error':
                raise SpaceError(
                    f'Janus error [{resp.get("error", {}).get("code")}]: '
                    f'{resp.get("error", {}).get("reason")}'
                )
            # The long-poll loop may already have resolved this transaction
            # (the ack arrives both as the HTTP response and as a poll event).
            if not future.done():
                future.set_result(resp)
            return resp
        except Exception as e:
            if not future.done():
                # No caller awaits this bookkeeping future when the HTTP
                # request itself fails; cancelling avoids an unhandled-future
                # warning while the original exception is re-raised below.
                future.cancel()
            raise
        finally:
            self._pending.pop(transaction, None)

    async def _message(self, payload: dict, jsep: dict | None = None) -> dict:
        """Send a videoroom plugin message through the attached handle."""
        if self.session_id is None or self.handler_id is None:
            raise SpaceError('Janus session/handle not created')
        body = {
            'room': self.room_id,
            'periscope_user_id': self.periscope_user_id,
            **payload,
        }
        message = {'janus': 'message', 'body': body}
        if jsep is not None:
            message['jsep'] = jsep
        resp = await self._dispatch(
            message,
            suffix=f'{self.session_id}/{self.handler_id}',
        )
        plugin = resp.get('plugindata', {}).get('data') or {}
        if plugin.get('error'):
            raise SpaceError(
                f'Janus videoroom error [{plugin.get("error_code")}]: '
                f'{plugin.get("error")}'
            )
        # Depending on gateway timing, the JSEP can be returned directly by
        # this HTTP request instead of arriving through the long poll.
        response_jsep = resp.get('jsep')
        if response_jsep and self.on_jsep:
            self.on_jsep(response_jsep, resp)
        return resp

    async def create(self) -> int:
        resp = await self._dispatch({'janus': 'create'})
        self.session_id = resp['data']['id']
        return self.session_id

    async def attach(self) -> int:
        resp = await self._dispatch({
            'janus': 'attach',
            'plugin': 'janus.plugin.videoroom',
        }, suffix=str(self.session_id))
        self.handler_id = resp['data']['id']
        return self.handler_id

    async def create_room(self, with_dummy_publisher: bool = False) -> None:
        await self._message({
            'request': 'create',
            'audiocodec': 'opus',
            'videocodec': 'h264',
            'transport_wide_cc_ext': True,
            'app_component': 'audio-room',
            'h264_profile': '42e01f',
            'dummy_publisher': with_dummy_publisher,
        })

    async def destroy_room(self) -> None:
        await self._message({'request': 'destroy'})

    async def join_as_publisher(self, display: str = '') -> None:
        await self._message({
            'request': 'join',
            'ptype': 'publisher',
            'display': display or self.periscope_user_id,
        })

    async def join_as_subscriber(self, streams: list) -> None:
        await self._message({
            'request': 'join',
            'ptype': 'subscriber',
            'streams': streams,
        })

    async def list_participants(self) -> list[dict]:
        """Return the current Janus room participants."""
        resp = await self._message({'request': 'listparticipants'})
        data = (resp.get('plugindata') or {}).get('data') or {}
        return data.get('participants') or []

    async def subscribe(self, streams: list) -> None:
        await self._message({'request': 'subscribe', 'streams': streams})

    async def update_streams(self, subscribe=None, unsubscribe=None) -> None:
        await self._message({
            'request': 'update',
            'subscribe': subscribe or [],
            'unsubscribe': unsubscribe or [],
        })

    async def switch_streams(self, streams: list) -> None:
        await self._message({'request': 'switch', 'streams': streams})

    async def send_sdp_offer(
        self,
        sdp: str,
        descriptions: Any = None,
        stream_name: str = '',
        session_uuid: str = '',
        vid_man_token: str = '',
        ice_restart: bool = False,
    ) -> None:
        payload = {
            'request': 'configure',
            'session_uuid': session_uuid,
            'stream_name': stream_name,
            'vidman_token': vid_man_token,
        }
        # NOTE: do NOT include `descriptions` — the gateway answers 429
        # ("Error processing SDP") when it is present. The web client sends
        # it as `undefined` (dropped by JSON.stringify).
        if ice_restart:
            payload['restart'] = True
        await self._message(payload, jsep={'type': 'offer', 'sdp': sdp})

    async def send_sdp_answer(self, sdp: str) -> dict:
        """Answer the server's SDP offer (subscriber side, `start`)."""
        return await self._message(
            {'request': 'start'},
            jsep={'type': 'answer', 'sdp': sdp},
        )

    async def unpublish(self) -> None:
        await self._message({'request': 'unpublish'})

    async def leave(self) -> None:
        await self._message({'request': 'leave'})

    async def detach(self) -> None:
        if self.session_id is not None and self.handler_id is not None:
            try:
                await self._dispatch(
                    {'janus': 'detach'},
                    suffix=f'{self.session_id}/{self.handler_id}'
                )
            except Exception:
                pass

    async def destroy(self) -> None:
        if self.session_id is not None:
            try:
                await self._dispatch({'janus': 'destroy'}, suffix=str(self.session_id))
            except Exception:
                pass
            self.session_id = None

    # -- long poll ---------------------------------------------------------

    def start_polling(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._long_poll_loop())

    async def _long_poll_loop(self) -> None:
        while self.session_id is not None:
            try:
                event = await self._http.get(
                    f'{self.janus_url}/{self.session_id}',
                    headers=self._headers(),
                    params={'maxev': 1},
                    timeout=35.0,
                )
            except SpaceError:
                await asyncio.sleep(1)
                continue
            except Exception:
                await asyncio.sleep(1)
                continue
            if not event:
                continue
            transaction = event.get('transaction')
            if transaction and transaction in self._pending:
                future = self._pending.pop(transaction)
                if not future.done():
                    future.set_result(event)
                # The event may carry the async result of the request (e.g.
                # the SDP answer to a configure) even though the caller
                # already received its (ack) HTTP response. Surface it to
                # the handlers so media negotiation never gets dropped.
                self._handle_event(event)
                continue
            self._handle_event(event)

    def _handle_event(self, event: dict) -> None:
        if self.on_raw_event:
            self.on_raw_event(event)
        jsep = event.get('jsep')
        if jsep and self.on_jsep:
            self.on_jsep(jsep, event)
        plugin = event.get('plugindata', {}).get('data') or {}
        videoroom = plugin.get('videoroom')
        if videoroom == 'joined' and self.on_publisher_id:
            pid = plugin.get('id')
            if pid is not None:
                self.on_publisher_id(pid)
        if plugin.get('publishers') and self.on_publishers:
            self.on_publishers(plugin.get('publishers'))
        if videoroom in ('attached', 'updated') and self.on_attached:
            streams = plugin.get('streams')
            if streams:
                self.on_attached(streams)

    async def wait_for_event(self, timeout: float = 15.0):
        """Long-poll once (or until a joined/event arrives) and return it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = await self._http.get(
                f'{self.janus_url}/{self.session_id}',
                headers=self._headers(),
                params={'maxev': 1},
                timeout=min(35.0, deadline - time.monotonic() + 1),
            )
            if not event:
                continue
            self._handle_event(event)
            return event
        return None

    async def close(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        await self.detach()
        await self.destroy()


class SpaceVoiceSession:
    """
    High-level WebRTC session for a Space (speak or listen).

    Requires the optional ``aiortc`` package. If it is not installed, the
    metadata/chat/control APIs still work; only voice fails with a clear
    error.
    """

    def __init__(
        self,
        *,
        janus_url: str,
        vid_man_token: str,
        stream_name: str = '',
        session_uuid: str = '',
        display: str = '',
        periscope_user_id: str = '',
        room_id: str = '',
        http: _Http | None = None,
        on_audio_track=None,
    ):
        self._http = http or _Http()
        self.periscope_user_id = periscope_user_id
        self.room_id = room_id
        self.janus = JanusClient(
            janus_url, vid_man_token, self._http,
            periscope_user_id=periscope_user_id,
            room_id=room_id,
        )
        self.stream_name = stream_name
        self.session_uuid = session_uuid
        self.display = display
        self.pc = None
        self.publisher_id: int | None = None
        self._publisher_future: asyncio.Future | None = None
        self._streams: list[dict] = []
        self._connected = asyncio.Event()
        self._negotiation_failed = asyncio.Event()
        self._negotiation_error: Exception | None = None
        self._jsep_tasks: set[asyncio.Task] = set()
        self._seen_jsep: set[tuple[str, str]] = set()
        self.remote_audio = None  # aiortc AudioStreamTrack when listening
        self.on_audio_track = on_audio_track

    def _require_aiortc(self):
        try:
            import aiortc  # noqa: F401
        except ImportError:
            raise SpaceError(
                'WebRTC voice requires the optional `aiortc` package. '
                'Install it with: pip install aiortc'
            ) from None
        return __import__('aiortc')

    @staticmethod
    def _make_peer_connection(aiortc, ice_servers: list[dict] | None):
        """Build an RTCPeerConnection, honouring TURN servers via
        RTCConfiguration (aiortc does not accept `iceServers=` directly)."""
        if not ice_servers:
            return aiortc.RTCPeerConnection()
        try:
            servers = [
                aiortc.RTCIceServer(
                    urls=srv.get('urls') or [],
                    username=srv.get('username'),
                    credential=srv.get('credential'),
                )
                for srv in ice_servers
            ]
            return aiortc.RTCPeerConnection(
                configuration=aiortc.RTCConfiguration(iceServers=servers)
            )
        except Exception:
            # Fall back to host-only ICE if the config path is unavailable.
            return aiortc.RTCPeerConnection()

    async def connect(
        self,
        ice_servers: list[dict],
        as_publisher: bool = True,
        create_room: bool = False,
        streams: list | None = None,
        with_dummy_publisher: bool = False,
        audio_track=None,
    ) -> None:
        aiortc = self._require_aiortc()
        self._publisher_future = asyncio.get_running_loop().create_future()
        self.janus.on_publisher_id = lambda pid: self._resolve_publisher(pid)
        self.janus.on_publishers = self._on_publishers
        self.janus.on_jsep = self._on_jsep

        await self.janus.create()
        await self.janus.attach()

        # Hosts must also attach a SECOND videoroom handle on the SAME
        # session and subscribe to their own feed (the web client's t8
        # handle). Without it the backend marks the space TimedOut after
        # ~2 minutes even while the media is flowing. NOTE: only the main
        # handle long-polls the session — a second poller on the same
        # session steals events (the SDP answer gets lost and ICE stays
        # "new"). The main poller dispatches by `sender` handle id.
        self._subscriber_handle: JanusClient | None = None
        if as_publisher:
            janus2 = JanusClient(
                self.janus.janus_url,
                self.janus.vid_man_token,
                self._http,
                periscope_user_id=self.periscope_user_id,
                room_id=self.room_id,
            )
            janus2.session_id = self.janus.session_id
            await janus2.attach()
            self._subscriber_handle = janus2

            def dispatch_raw(evt: dict, _j2=janus2):
                if evt.get('sender') != _j2.handler_id:
                    return
                jsep = evt.get('jsep')
                if jsep and _j2.on_jsep:
                    _j2.on_jsep(jsep, evt)
                plugin = evt.get('plugindata', {}).get('data') or {}
                if plugin.get('videoroom') == 'attached' and _j2.on_attached:
                    _j2.on_attached(plugin.get('streams'))
            self.janus.on_raw_event = dispatch_raw

        self.pc = self._make_peer_connection(aiortc, ice_servers)
        self.pc.on('track', self._on_track)
        self.pc.on('iceconnectionstatechange', self._on_ice_state)
        self.pc.on('connectionstatechange', self._on_connection_state)
        # Janus delivers videoroom events and SDP exclusively through the
        # long poll.  It must be running before join/configure messages.
        self.janus.start_polling()

        if as_publisher:
            if create_room:
                try:
                    await self.janus.create_room(
                        with_dummy_publisher=with_dummy_publisher
                    )
                except SpaceError as e:
                    # The room already exists when the host re-joins their
                    # own space or when joining an existing one — that is
                    # fine, proceed to join as publisher.
                    msg = str(e).lower()
                    if 'already exists' not in msg and '427' not in msg:
                        raise
            await self.janus.join_as_publisher(self.display or '')
            try:
                publisher_id = await self.wait_for_publisher_id(timeout=15)
            except asyncio.TimeoutError:
                publisher_id = None
            # Subscribe to our own feed on the second handle (keeps the
            # space from being timed out by the backend).
            if self._subscriber_handle is not None and publisher_id is not None:
                try:
                    await self._subscriber_handle.join_as_subscriber(
                        [{'feed': publisher_id, 'mid': '0'}]
                    )
                except SpaceError:
                    pass
            if audio_track is not None:
                # The web client adds its audio track with an explicit
                # 'sendonly' transceiver direction; aiortc's addTrack()
                # defaults to 'sendrecv', which makes X's SFU classify the
                # session as a listener and tear it down after ~60s.
                self.pc.addTransceiver(audio_track, direction='sendonly')
            # Give the track time to be ready before the offer.
            await asyncio.sleep(0.2)
            offer = await self.pc.createOffer()
            await self.pc.setLocalDescription(offer)
            await self.janus.send_sdp_offer(
                offer.sdp,
                stream_name=self.stream_name,
                session_uuid=self.session_uuid,
                vid_man_token=self.janus.vid_man_token,
            )
        else:
            self.pc.addTransceiver('audio', direction='recvonly')
            normalized = _normalize_audio_streams(streams or [])
            if not normalized:
                participants = await self.janus.list_participants()
                normalized = [
                    {'feed': p['id'], 'mid': '0'}
                    for p in participants
                    if p.get('publisher') and p.get('id') is not None
                ]
            if not normalized:
                raise SpaceError('no active audio publisher in the Space')
            # Subscriber negotiation is server-driven: join makes Janus send
            # an SDP offer through long polling; _apply_remote_jsep answers it
            # and completes the videoroom `start` request.
            await self.janus.join_as_subscriber(normalized)

        # ICE completion alone is not enough: DTLS may remain stuck in
        # `connecting`, yielding a track object but never any RTP frames.
        connected = asyncio.create_task(self._connected.wait())
        failed = asyncio.create_task(self._negotiation_failed.wait())
        done, pending = await asyncio.wait(
            {connected, failed}, timeout=25,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if failed in done and self._negotiation_error is not None:
            error = self._negotiation_error
            await self.close()
            raise SpaceError(
                f'WebRTC SDP negotiation failed: {error}'
            ) from error
        if connected not in done:
            role = 'publisher' if as_publisher else 'listener'
            await self.close()
            raise SpaceError(
                f'{role} WebRTC/DTLS negotiation timed out'
            )

    def _resolve_publisher(self, pid: int):
        self.publisher_id = pid
        if self._publisher_future is not None and not self._publisher_future.done():
            self._publisher_future.set_result(pid)

    async def wait_for_publisher_id(self, timeout: float = 15.0) -> int:
        if self.publisher_id is not None:
            return self.publisher_id
        return await asyncio.wait_for(self._publisher_future, timeout=timeout)

    def _on_publishers(self, publishers: list):
        self._streams = publishers

    def _on_jsep(self, jsep: dict, event: dict = None):
        if self.pc is None:
            return
        task = asyncio.create_task(self._apply_remote_jsep(jsep))
        self._jsep_tasks.add(task)
        task.add_done_callback(self._finish_jsep_task)

    def _finish_jsep_task(self, task: asyncio.Task) -> None:
        self._jsep_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if self._negotiation_error is None:
                self._negotiation_error = exc
                self._negotiation_failed.set()

    async def _apply_remote_jsep(self, jsep: dict) -> None:
        sdp = jsep.get('sdp')
        if not sdp or self.pc is None:
            return
        aiortc = self._require_aiortc()
        jsep_type = jsep.get('type', 'answer')
        key = (jsep_type, sdp)
        if key in self._seen_jsep:
            return
        self._seen_jsep.add(key)
        await self.pc.setRemoteDescription(
            aiortc.RTCSessionDescription(sdp=sdp, type=jsep_type)
        )
        if jsep_type == 'offer':
            answer = await self.pc.createAnswer()
            await self.pc.setLocalDescription(answer)
            await self.janus.send_sdp_answer(self.pc.localDescription.sdp)

    def _on_track(self, track):
        self.remote_audio = track
        if self.on_audio_track is not None:
            self.on_audio_track(track)
        if self.pc is not None:
            self.pc.iceConnectionState  # touch

    def _on_ice_state(self):
        # Kept as a hook for callers/debugging. Readiness is signalled by
        # the aggregate PeerConnection state below, after DTLS is ready.
        return None

    def _on_connection_state(self):
        try:
            if self.pc.connectionState == 'connected':
                self._connected.set()
        except Exception:
            pass

    async def close(self):
        tasks = list(self._jsep_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._jsep_tasks.clear()
        if self.pc is not None:
            await self.pc.close()
            self.pc = None
        await self.janus.close()


# ---------------------------------------------------------------------------
# Chat client (WebSocket)
# ---------------------------------------------------------------------------

class SpaceChat:
    """
    Chat for a Space. History works over plain HTTP (no extra deps); live
    receive + send use a WebSocket (requires the optional `websockets`
    package).
    """

    def __init__(
        self,
        proxsee: ProxseeApi,
        chat_token: str,
        endpoint: str | None = None,
        room_id: str | None = None,
        access_token: str | None = None,
    ):
        self._proxsee = proxsee
        self.chat_token = chat_token
        self.endpoint = endpoint
        self.room_id = room_id
        self.access_token = access_token
        self.read_only = False
        self._ws = None

    async def connect(self) -> 'SpaceChat':
        """Resolve chat access via proxsee and (if websockets is available
        and the Space is live) open the live WebSocket. Replays are
        read-only: history works, the WS is not offered."""
        # The web client uses accessChat (NOT accessChatPublic) — public
        # access comes back read_only and cannot send messages.
        data = await self._proxsee.access_chat(self.chat_token)
        self.endpoint = data.get('endpoint') or self.endpoint
        self.room_id = data.get('room_id') or self.room_id
        self.access_token = data.get('access_token') or self.access_token
        self.read_only = bool(data.get('read_only'))
        if self.endpoint and self.access_token:
            try:
                import websockets  # noqa: F401
                ws_url = (
                    self.endpoint.replace('https://', 'wss://')
                    .replace('http://', 'ws://')
                    + '/chatapi/v1/chatnow'
                )
                self._ws = await websockets.connect(ws_url)
                # The chatman protocol sends two control frames on open:
                # an auth frame (kind 3) and a join frame (kind 2/Control
                # wrapping a Join body).
                await self._ws.send(json.dumps({
                    'payload': json.dumps({'access_token': self.access_token}),
                    'kind': 3,
                }))
                await self._ws.send(json.dumps({
                    'payload': json.dumps({
                        'body': json.dumps({'room': self.room_id}),
                        'kind': 1,
                    }),
                    'kind': 2,
                }))
            except ImportError:
                pass  # history-only mode
            except Exception:
                pass  # WS not offered (replay) — history still works
        return self

    async def history(
        self, cursor: str | None = None, limit: int = 1000
    ) -> list[ChatMessage]:
        if not self.endpoint or not self.access_token:
            raise SpaceError('chat not connected; call connect() first')
        data = await self._proxsee.get_chat_history(
            self.endpoint, self.access_token, cursor=cursor, limit=limit
        )
        messages = data.get('messages') or []
        return [ChatMessage.from_payload(m) for m in messages]

    async def listen(self) -> AsyncIterator[ChatMessage]:
        """Yield chat messages as they arrive (requires `websockets`)."""
        if self._ws is None:
            raise SpaceError(
                'live chat requires the optional `websockets` package; '
                'install it with: pip install websockets'
            )
        async for raw in self._ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # kind 1 = Chat (with payload+signature), 2 = Control,
            # 3 = Auth. Only chat frames carry user messages.
            if data.get('kind') in (1, 2):
                yield ChatMessage.from_payload(data)

    async def send(self, text: str) -> None:
        """Send a chat message (requires `websockets`). Public chat access is
        read-only for non-participants; sending raises SpaceError then."""
        if self._ws is None:
            raise SpaceError(
                'sending chat requires the optional `websockets` package; '
                'install it with: pip install websockets'
            )
        if self.read_only:
            raise SpaceError(
                'chat is read-only for this access level (replay or '
                'non-participant)'
            )
        # Shape used by the web client chatman (Periscope chat protocol):
        # an outer {payload, kind: 1 Chat} frame whose payload is
        # {kind: 1, room, lang: 'en', body, sender, timestamp}.
        import time as _time
        import uuid as _uuid
        from datetime import datetime, timezone
        now_ms = int(_time.time() * 1000)
        ntp = 1e9 * ((now_ms / 1000) + 2208988800)
        sender = {
            'user_id': self._proxsee.periscope_user_id or '',
            'twitter_id': self._proxsee.twitter_id or '',
            'username': self._proxsee.twitter_screen_name or '',
            'display_name': self._proxsee.display_name or '',
            'participant_index': 0,
        }
        body = {
            'body': text,
            'displayName': sender['display_name'],
            'ntpForBroadcasterFrame': ntp,
            'ntpForLiveFrame': ntp,
            'participant_index': 0,
            'programDateTime': datetime.now(timezone.utc).isoformat(),
            'remoteID': sender['user_id'],
            'timestamp': now_ms,
            'type': 1,  # Chat (X8 message type enum — numeric, not string)
            'username': sender['username'] or '',
            'uuid': str(_uuid.uuid4()),
            'v': 2,
        }
        frame = {
            'payload': json.dumps({
                'kind': 1,
                'room': self.room_id,
                'lang': 'en',
                'body': json.dumps(body),
                'sender': sender,
                'timestamp': now_ms,
            }),
            'kind': 1,
        }
        await self._ws.send(json.dumps(frame))

    async def close(self):
        if self._ws is not None:
            await self._ws.close()
            self._ws = None


# ---------------------------------------------------------------------------
# High-level facade
# ---------------------------------------------------------------------------

class Spaces:
    """Entry point for all Space operations on a twifork Client."""

    def __init__(self, client: 'Client'):
        self._client = client
        self.proxsee = ProxseeApi(client)
        self.chatman = ChatmanApi(client)
        self._http = _Http()
        self._host_sessions: dict[str, str] = {}

    # -- metadata / discovery ----------------------------------------------

    async def get_space(self, space_id: str, **kwargs) -> Space:
        """Fetch Space metadata by its 13-character id (or full URL)."""
        space_id = _extract_space_id(space_id)
        data, _ = await self._client.gql.audio_space_by_id(space_id, **kwargs)
        audio_space = (data.get('data') or {}).get('audioSpace')
        if audio_space is None:
            raise SpaceError(f'Space not found: {space_id}')
        return Space(audio_space)

    async def get_space_by_url(self, url: str, **kwargs) -> Space:
        return await self.get_space(_extract_space_id(url), **kwargs)

    async def search(
        self, query: str, filter: str = 'Live', *, hydrate: bool = True
    ) -> list[Space]:
        """Search Spaces. ``filter`` is ``Top``, ``Live`` or ``Upcoming``.

        X's current search payload contains only each Space's ``rest_id``.
        By default the ids are hydrated concurrently through
        :meth:`get_space`, so fields such as ``title`` and ``state`` are
        populated. Pass ``hydrate=False`` for the cheaper id-only response.
        """
        data, _ = await self._client.gql.audio_space_search(
            query, filter=filter
        )
        audio_space = (data.get('data') or {}).get('search_by_raw_query')
        if audio_space is None:
            return []
        sections = (
            (audio_space.get('audio_spaces_grouped_by_section') or {})
            .get('sections') or []
        )
        spaces = []
        for section in sections:
            for item in section.get('items') or []:
                space_data = item.get('space')
                if space_data:
                    metadata = space_data.get('metadata') or space_data
                    spaces.append(Space({'metadata': metadata}))
        if not hydrate:
            return spaces

        async def hydrate_one(space: Space) -> Space:
            if not space.id:
                return space
            try:
                return await self.get_space(space.id)
            except Exception:
                # A live result can end/disappear between the grouped search
                # and AudioSpaceById; retain its id rather than dropping it.
                return space

        return list(await asyncio.gather(*(hydrate_one(s) for s in spaces)))

    async def topics(self) -> list[dict]:
        data, _ = await self._client.gql.browse_space_topics()
        return (
            (data.get('data') or {})
            .get('browse_space_topics') or {}
        ).get('categories') or []

    # -- stream (HLS listening / replay) -----------------------------------

    async def get_stream(self, media_key: str) -> SpaceStream:
        """Resolve a media_key to its stream info (HLS url + chat token)."""
        data, _ = await self._client.v11.live_video_stream_status(media_key)
        return SpaceStream.from_response(data)

    async def stream_url(self, space: Space) -> str | None:
        """Convenience: HLS m3u8 url for a live/replay Space."""
        if not space.media_key:
            return None
        stream = await self.get_stream(space.media_key)
        return stream.hls_url

    # -- create / end ------------------------------------------------------

    async def create_space(
        self,
        title: str = '',
        *,
        description: str = '',
        topics: list | None = None,
        conversation_controls: int = 2,
        narrow_cast_space_type: int = 0,
        is_space_available_for_replay: bool = False,
        is_space_available_for_clipping: bool = False,
        scheduled_start_time: int = 0,
        languages: list | None = None,
        region: str = 'us-west-1',
        extra_metadata: dict | None = None,
        auto_publish: bool = True,
        publish_extra: dict | None = None,
    ) -> dict:
        """
        Create a Space. Returns the proxsee createBroadcast response:
        {broadcast: {id, ...}, access_token, room_id, stream_name,
         credential, webrtc_gw_url, ...}.

        ``scheduled_start_time`` is epoch **milliseconds** (as the web client
        sends it); values below 10^12 are treated as seconds and converted
        automatically.

        With ``auto_publish=True`` (default) the broadcast is immediately
        published — pass the title via ``publish_extra`` if the title should
        ride the publish (the web client sends it there as ``status``).
        """
        sst = scheduled_start_time
        if sst and sst < 10**12:
            sst *= 1000
        metadata = {
            **DEFAULT_SPACE_METADATA,
            'title': title,
            'description': description,
            'topics': topics or [],
            'conversation_controls': conversation_controls,
            'narrow_cast_space_type': narrow_cast_space_type,
            'is_space_available_for_replay': is_space_available_for_replay,
            'is_space_available_for_clipping': is_space_available_for_clipping,
            'scheduled_start_time': sst,
            'languages': languages or [],
            'region': region,
            **(extra_metadata or {}),
        }
        await self.proxsee.login(create_user=True)
        resp = await self.proxsee.create_broadcast(metadata)
        # Scheduled spaces have no Janus room yet — skip the live-publish
        # dance (webrtc_gw_url is absent and Janus create() would crash).
        if auto_publish and not sst:
            broadcast = resp.get('broadcast') or {}
            broadcast_id = broadcast.get('id') or resp.get('broadcast_id')
            # A live publish needs a real Janus room + publisher registration
            # (measured: publishBroadcast 500s without them). The web client
            # does exactly this before publishing.
            janus = JanusClient(
                resp.get('webrtc_gw_url') or '',
                resp.get('credential') or '',
                self._http,
                periscope_user_id=self.proxsee.periscope_user_id or '',
                room_id=broadcast_id or '',
            )
            await janus.create()
            await janus.attach()
            await janus.create_room()
            await janus.join_as_publisher()
            publisher_id = None
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                event = await janus.wait_for_event(timeout=10)
                if event is None:
                    break
                plugin = (event.get('plugindata') or {}).get('data') or {}
                if plugin.get('videoroom') == 'joined':
                    publisher_id = plugin.get('id')
                    break
                if publisher_id is not None:
                    break
            if publisher_id is None:
                await janus.close()
                # Clean up the broadcast so we never leave a zombie space.
                try:
                    await self.proxsee.end_broadcast(broadcast_id)
                except SpaceError:
                    pass
                raise SpaceError(
                    'could not obtain Janus publisher id; '
                    'space creation aborted'
                )
            payload = {
                'broadcast_id': broadcast_id,
                'status': title,
                'topics': topics or [],
                'conversation_controls': conversation_controls,
                'mentioned_twitter_user_ids': [],
                'janus_publisher_id': publisher_id,
                'janus_room_id': broadcast_id,
                'webrtc_handle_id': janus.handler_id,
                'webrtc_session_id': janus.session_id,
                **(publish_extra or {}),
            }
            await self.proxsee.publish_broadcast(payload)
            # Register the host as the space admin in chatman (the web
            # client's `adminInvite` right after publishBroadcast). Without
            # this the host shows up muted and listeners hear nothing.
            try:
                await self.chatman.initialize(resp.get('access_token'))
                await self.chatman.admin_invite(
                    broadcast_id, [str(broadcast.get('twitter_id') or '')]
                )
            except SpaceError:
                pass
            # The room/publisher registration has served its purpose; tear
            # the Janus session down. The space stays live (host reconnects
            # with reconnectHost when it starts sending audio).
            await janus.close()
        return resp

    async def end_space(self, space_id: str) -> None:
        """
        End a Space. The chatman call ends the audiospace; the proxsee call
        tears down the broadcast. `space_id` here is the proxsee broadcast id
        (from create_space) or the X space id.
        """
        # Chat shutdown is useful, but must never prevent the authoritative
        # proxsee endBroadcast call. Metadata/stream resolution can fail
        # transiently while a newly-created Space is still propagating.
        try:
            space = await self.get_space(space_id)
            if space.media_key:
                stream = await self.get_stream(space.media_key)
                if stream.chat_token:
                    await self.chatman.initialize(stream.chat_token)
                    await self.chatman.end_audio_space(space_id)
        except Exception:
            pass

        # A dropped connection here otherwise leaves a real Space Running.
        # Retry only transport errors; API errors retain the old best-effort
        # behaviour (an already-ended broadcast commonly rejects repeats).
        for attempt in range(3):
            try:
                await self.proxsee.end_broadcast(space_id)
                break
            except SpaceError:
                break
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(1 + attempt)

    async def cancel_scheduled_space(self, broadcast_id: str) -> None:
        await self.proxsee.login()
        await self.proxsee.cancel_scheduled_space(broadcast_id)

    async def get_scheduled_spaces(self) -> list[dict]:
        """List your scheduled Spaces. Each item is the broadcast info
        dict (id, state='NOT_STARTED', scheduled_start, media_key, ...)."""
        await self.proxsee.login(create_user=True)
        resp = await self.proxsee.get_scheduled_spaces()
        broadcasts = (resp.get('broadcasts') or []) if isinstance(resp, dict) else []
        out = []
        for b in broadcasts:
            if isinstance(b, dict):
                out.append(b.get('broadcast') or b)
        return out

    # -- join / speak ------------------------------------------------------

    async def _ensure_chatman(self, space: Space | str) -> str:
        """Resolve the Space's chat token and make sure chatman is
        initialized. Returns the Space id."""
        if not isinstance(space, Space):
            space = await self.get_space(space)
        space_id = space.id
        if not space_id:
            raise SpaceError('Space has no id')
        chat_token = None
        if space.media_key:
            try:
                stream = await self.get_stream(space.media_key)
                chat_token = stream.chat_token
            except Exception:
                chat_token = None
        chat_token = chat_token or space.chat.get('chat_token') or None
        if not chat_token:
            raise SpaceError('could not resolve chat token for space')
        # The raw chat token from live_video_stream/status must be
        # exchanged via accessChat for the chatman access token (sending
        # the raw token makes chatman reply `invalid secretvalue`).
        try:
            exchanged = await self.proxsee.access_chat(chat_token)
            chat_token = exchanged.get('access_token') or chat_token
        except SpaceError:
            pass
        await self.chatman.initialize(chat_token)
        return space_id

    async def join(
        self,
        space: Space | str,
        *,
        as_speaker: bool = False,
        join_as_admin: bool = False,
        should_auto_join: bool = True,
    ) -> dict:
        """
        Join a Space as speaker or listener. Returns
        {session_uuid, can_auto_join, ...} from chatman audiospace/join.

        The broadcast_id used here is the X space id (AudioSpaceById
        `rest_id`); the chatman endpoint maps it to the proxsee broadcast.
        """
        space_id = await self._ensure_chatman(space)
        return await self.chatman.join_as_speaker(
            space_id,
            join_as_admin=join_as_admin,
            should_auto_join=should_auto_join,
        )

    async def speak(
        self,
        space: Space | str,
        *,
        on_audio_track=None,
        audio_track=None,
        ice_servers: list[dict] | None = None,
        session_uuid: str | None = None,
        **join_kwargs,
    ) -> SpaceVoiceSession:
        """
        Join as speaker and open the WebRTC publish session (requires
        aiortc). Returns a SpaceVoiceSession with `.pc` (RTCPeerConnection)
        and `.publisher_id`.

        ``audio_track`` is an aiortc AudioStreamTrack whose
        ``recv()`` returns 48 kHz stereo AudioFrames; it is added before the
        initial SDP offer. ``on_audio_track`` (legacy) is a callback receiving
        the session and returning that track before the first offer.

        Pass ``session_uuid`` to reuse an existing chatman session (e.g.
        after the host approved your raise-hand request — joining again
        with a fresh session is not needed and may be rejected).
        """
        if session_uuid is None:
            joined = await self.join(space, as_speaker=True, **join_kwargs)
            session_uuid = joined.get('session_uuid')
        if not session_uuid:
            raise SpaceError(f'no session_uuid for speak() on {space}')
        neg = await self.chatman.negotiate_stream(session_uuid)
        janus_jwt = neg.get('janus_jwt') or neg.get('janusJwt')
        webrtc_gw_url = neg.get('webrtc_gw_url') or neg.get('webrtcGwUrl')
        if not janus_jwt or not webrtc_gw_url:
            raise SpaceError(f'negotiate returned no janus info: {neg}')

        if ice_servers is None:
            # Default to DIRECT connection (no TURN). X's TURN server
            # (turns:turn.pscp.tv:443) tears the TLS connection down after
            # ~60s, killing the media path and making the SFU drop the
            # publisher. Pass explicit ice_servers to enable TURN/relay
            # when direct connectivity is unavailable.
            ice_servers = []

        sp = isinstance(space, Space) and space or await self.get_space(space)
        session = SpaceVoiceSession(
            janus_url=webrtc_gw_url,
            vid_man_token=janus_jwt,
            stream_name=sp.id or '',
            session_uuid=session_uuid,
            display=self.proxsee.periscope_user_id or '',
            periscope_user_id=self.proxsee.periscope_user_id or '',
            room_id=sp.id or '',
            http=self._http,
        )
        if audio_track is None and on_audio_track is not None:
            audio_track = on_audio_track(session)
        if audio_track is None:
            raise SpaceError(
                'speak() requires audio_track or an on_audio_track callback'
            )
        await session.connect(
            ice_servers,
            as_publisher=True,
            create_room=False,
            audio_track=audio_track,
        )
        # Host/speaker bookkeeping that the web client performs right after
        # the SDP offer: unmute the speaker (X starts hosts auto-muted —
        # without this the backend treats the publisher as muted) and
        # announce the published stream (audiospace/stream/publish).
        try:
            await self.chatman.unmute_speaker(session_uuid, sp.id or '')
        except SpaceError:
            pass
        try:
            await self.chatman.publish_stream(session_uuid)
        except SpaceError:
            pass
        return session

    async def host(
        self,
        created: dict,
        *,
        audio_track=None,
        ice_servers: list[dict] | None = None,
        publish_extra: dict | None = None,
    ) -> SpaceVoiceSession:
        """
        Open the host voice session for a Space created by
        :meth:`create_space` (the creator's own WebRTC publish path).

        ``created`` is the return value of ``create_space()``. Its broadcast
        id is passed to ``reconnectHost`` to obtain a fresh host session UUID
        and Janus credentials (``audiospace/join`` is 403 for the host).
        ``audio_track`` is required; use ``create_space()`` alone when no
        live audio is needed.

        The full web-client host flow is applied: attach a second
        videoroom handle and subscribe to the host's own feed (prevents
        the backend from timing the space out), TURN-less direct ICE by
        default (X's TURN tears down after ~60s), then best-effort
        ``unmuteSpeaker`` + ``stream/publish`` after the SDP offer.
        """
        resp = created
        broadcast = resp.get('broadcast') or {}
        broadcast_id = broadcast.get('id') or resp.get('broadcast_id')
        if not broadcast_id:
            raise SpaceError('create_space response has no broadcast id')
        if audio_track is None:
            raise SpaceError('host() requires an audio_track')
        if ice_servers is None:
            # Direct connection by default (see speak() for the TURN note).
            ice_servers = []
        # The current web client reconnects the host before opening media.
        # Besides fresh Janus credentials this supplies the host's real
        # Chatman session UUID, required by moderation and stream bookkeeping.
        try:
            reconnected = await self.proxsee.reconnect_host(broadcast_id)
        except SpaceError:
            reconnected = {}
        host_details = {**resp, **reconnected}
        host_broadcast = reconnected.get('broadcast') or broadcast
        session_uuid = host_details.get('session_uuid') or ''
        if session_uuid:
            self._host_sessions[broadcast_id] = session_uuid
        await self.chatman.initialize(host_details.get('access_token') or '')

        session = SpaceVoiceSession(
            janus_url=host_details.get('webrtc_gw_url') or '',
            vid_man_token=host_details.get('credential') or '',
            stream_name=host_details.get('stream_name') or broadcast_id,
            session_uuid=session_uuid,
            display=self.proxsee.periscope_user_id or '',
            periscope_user_id=self.proxsee.periscope_user_id or '',
            room_id=broadcast_id,
            http=self._http,
        )
        await session.connect(
            ice_servers,
            as_publisher=True,
            create_room=True,
            audio_track=audio_track,
        )
        # The SFU must know the room's active publisher: re-publish with
        # the new session's publisher id (the create_space() registration
        # belonged to the temporary session).
        if session.publisher_id:
            try:
                payload = {
                    'broadcast_id': broadcast_id,
                    'status': host_broadcast.get('title') or '',
                    'topics': [],
                    'conversation_controls': 0,
                    'mentioned_twitter_user_ids': [],
                    'janus_publisher_id': session.publisher_id,
                    'janus_room_id': broadcast_id,
                    'webrtc_handle_id': session.janus.handler_id,
                    'webrtc_session_id': session.janus.session_id,
                    **(publish_extra or {}),
                }
                await self.proxsee.publish_broadcast(payload)
            except SpaceError:
                pass
        # Host bookkeeping right after the SDP offer (web client does the
        # same): unmute (hosts start auto-muted) + stream/publish.
        try:
            await self.chatman.unmute_speaker(session_uuid, broadcast_id)
        except SpaceError:
            pass
        try:
            await self.chatman.publish_stream(session_uuid)
        except SpaceError:
            pass
        return session

    async def listen(
        self,
        space: Space | str,
        *,
        on_audio_track=None,
        ice_servers: list[dict] | None = None,
        attempts: int = 3,
    ) -> SpaceVoiceSession:
        """
        Join as listener and open a WebRTC receive session (requires
        aiortc). Simpler listening can be done with HLS: see `stream_url()`
        + ffmpeg.
        """
        if ice_servers is None:
            # Direct connection by default (see speak() for the TURN note).
            ice_servers = []
        sid = space.id if isinstance(space, Space) else space
        attempts = max(1, attempts)
        last_error = None
        for attempt in range(attempts):
            session = None
            try:
                joined = await self.join(
                    space, as_speaker=False, should_auto_join=True
                )
                session_uuid = joined.get('session_uuid')
                if not session_uuid:
                    raise SpaceError(
                        f'join returned no session_uuid: {joined}'
                    )
                neg = await self.chatman.negotiate_stream(session_uuid)
                janus_jwt = neg.get('janus_jwt') or neg.get('janusJwt')
                webrtc_gw_url = (
                    neg.get('webrtc_gw_url') or neg.get('webrtcGwUrl')
                )
                session = SpaceVoiceSession(
                    janus_url=webrtc_gw_url,
                    vid_man_token=janus_jwt,
                    stream_name=sid or '',
                    session_uuid=session_uuid,
                    periscope_user_id=self.proxsee.periscope_user_id or '',
                    room_id=sid or '',
                    http=self._http,
                )
                await session.connect(ice_servers, as_publisher=False)
                if on_audio_track is not None and \
                        session.remote_audio is not None:
                    on_audio_track(session.remote_audio)
                return session
            except Exception as exc:
                last_error = exc
                if session is not None:
                    await session.close()
                if attempt + 1 < attempts:
                    await asyncio.sleep(1 + attempt)
        raise last_error

    # -- chat --------------------------------------------------------------

    async def chat(self, space: Space | str) -> SpaceChat:
        """Get a chat session for the Space (history + optional WS)."""
        if not isinstance(space, Space):
            space = await self.get_space(space)
        chat_token = None
        if space.media_key:
            try:
                stream = await self.get_stream(space.media_key)
                chat_token = stream.chat_token
            except Exception:
                # ended spaces 404 on live_video_stream/status; fall back
                # to the chat token embedded in the AudioSpaceById payload
                chat_token = None
        chat_token = chat_token or space.chat.get('chat_token')
        if not chat_token:
            raise SpaceError('could not resolve chat token for space')
        await self.proxsee.login()
        return await SpaceChat(
            self.proxsee, chat_token
        ).connect()

    async def stream_live_chat(
        self,
        space: Space | str,
        session_id: str | None = None,
        cursor: str | None = None,
        replay: bool = False,
    ) -> AsyncIterator[dict]:
        """
        Live chat via the web client's HTTP stream (/live-chat). Yields
        parsed JSON events (userId, controlType, …). Does not require the
        `websockets` package.

        With ``replay=True`` the endpoint returns the chat log of an
        ended Space (``replay=1``), paged by ``cursor`` — no chat token
        needed, works for ended spaces too.
        """
        if not isinstance(space, Space):
            space = await self.get_space(space)
        space_id = space.id
        if not space_id:
            raise SpaceError('Space has no id')
        params = {
            'broadcastId': space_id,
            'sessionId': session_id or '',
        }
        if cursor is not None:
            params['cursor'] = cursor
        if replay:
            params['replay'] = 1
        headers = dict(self._client._base_headers)
        # /live-chat demands the same client-transaction header as the rest
        # of the API (401 without it) plus the CSRF token.
        headers['X-Client-Transaction-Id'] = (
            self._client.client_transaction.generate_transaction_id(
                method='GET', path='/live-chat'
            )
        )
        headers['X-Csrf-Token'] = self._client._get_csrf_token() or ''
        async with self._client.http.stream(
            'GET',
            'https://x.com/live-chat',
            params=params,
            headers=headers,
        ) as response:
            if response.status_code != 200:
                raise SpaceError(
                    f'/live-chat returned HTTP {response.status_code}'
                )
            buffer = ''
            async for chunk in response.aiter_text():
                buffer += chunk
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    line = line.strip().strip('\r')
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event:
                        yield event

    # -- moderation shortcuts ----------------------------------------------

    async def mute_space(self, space_id: str) -> dict:
        """Mute the whole Space (only the host can)."""
        await self._ensure_chatman(space_id)
        return await self.chatman.mute_space(space_id)

    async def unmute_space(self, space_id: str) -> dict:
        """Unmute the whole Space (only the host can)."""
        await self._ensure_chatman(space_id)
        return await self.chatman.unmute_space(space_id)

    async def set_space_settings(
        self, space_id: str, conversation_controls: int
    ) -> dict:
        """Change who can speak: 0=request/approval, 1=followed, 2=everyone."""
        await self._ensure_chatman(space_id)
        return await self.chatman.set_space_settings(
            space_id, conversation_controls
        )

    async def add_admin(
        self, space_id: str, user_id: str, session_uuid: str = ''
    ) -> dict:
        """Promote a participant to co-host."""
        await self._ensure_chatman(space_id)
        return await self.chatman.add_admin(space_id, user_id)

    async def remove_admin(
        self, space_id: str, user_id: str, session_uuid: str = ''
    ) -> dict:
        """Demote a co-host back to a regular participant."""
        await self._ensure_chatman(space_id)
        host_session = session_uuid or self._host_sessions.get(space_id, '')
        return await self.chatman.remove_admin(
            space_id, host_session, user_id
        )

    async def mute_speaker(self, space_id: str, session_uuid: str) -> dict:
        return await self.chatman.mute_speaker(session_uuid, space_id)

    async def unmute_speaker(self, space_id: str, session_uuid: str) -> dict:
        return await self.chatman.unmute_speaker(session_uuid, space_id)

    async def approve(
        self, session_uuid: str, space_id: str | None = None
    ) -> None:
        """Approve a speaker request. Pass ``space_id`` to let the facade
        resolve and initialize chatman itself."""
        if space_id is not None:
            await self._ensure_chatman(space_id)
        await self.chatman.approve_request(session_uuid)

    async def reject(
        self, session_uuid: str, space_id: str | None = None
    ) -> None:
        """Reject a speaker request. Pass ``space_id`` to let the facade
        resolve and initialize chatman itself."""
        if space_id is not None:
            await self._ensure_chatman(space_id)
        await self.chatman.reject_request(session_uuid)

    async def request_to_speak(self, space: Space | str) -> str:
        """
        Ask to become a speaker in a Space you joined as a listener.
        Returns the session_uuid (the chatman response carries it; it is
        what the host approves via :meth:`approve`). Initializes chatman
        itself, so it can be used standalone after join().
        """
        space_id = await self._ensure_chatman(space)
        resp = await self.chatman.submit_speaker_request(space_id)
        suuid = resp.get('session_uuid')
        if not suuid:
            raise SpaceError(f'submit speaker request returned no session_uuid: {resp}')
        return suuid

    async def wait_for_speaker(
        self,
        space_id: str,
        session_uuid: str,
        timeout: float = 120.0,
        interval: float = 3.0,
    ) -> dict | None:
        """
        Poll call/status until the session becomes a speaker (the host
        approved the request). Returns the guest session dict, or None
        on timeout. The web client uses the same call/status endpoint.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status = await self.get_call_status(space_id)
            except SpaceError:
                status = {}
            for guest in status.get('guest_sessions') or []:
                if guest.get('session_uuid') == session_uuid and \
                        guest.get('session_state') == 4:
                    return guest
            await asyncio.sleep(interval)
        return None

    async def get_call_status(self, space_id: str) -> dict:
        """Fetch the call/status snapshot (guest sessions, states, ...)."""
        await self._ensure_chatman(space_id)
        return await self.chatman.get_call_status(space_id)

    async def remove_participant(
        self, space_id: str, user_ids: list[str]
    ) -> dict:
        return await self.chatman.remove_participant(space_id, user_ids)

    async def raise_hand(self, space_id: str, session_uuid: str) -> dict:
        return await self.chatman.raise_hand(session_uuid, space_id)

    async def lower_hand(self, space_id: str, session_uuid: str) -> dict:
        return await self.chatman.lower_hand(session_uuid, space_id)

    async def cancel_speaker_request(
        self, space_id: str, session_uuid: str
    ) -> dict:
        """Withdraw a pending speaker request."""
        return await self.chatman.cancel_speaker_request(space_id, session_uuid)

    async def add_sharing(self, space_id: str, tweet_id: str) -> None:
        """Share a Space with a tweet (adds the tweet to the Space)."""
        await self._client.gql.audio_space_add_sharing(space_id, tweet_id)

    async def delete_sharing(self, space_id: str, sharing_id: str) -> None:
        """Remove a tweet sharing from the Space (see add_sharing)."""
        await self._client.gql.audio_space_delete_sharing(space_id, sharing_id)

    async def subscribe_scheduled(self, space_id: str) -> None:
        await self._client.gql.subscribe_to_scheduled_space(space_id)

    async def unsubscribe_scheduled(self, space_id: str) -> None:
        await self._client.gql.unsubscribe_from_scheduled_space(space_id)

    async def associate_tweet_with_broadcast(
        self, space_id: str, tweet_id: str, tweet_external: bool = False
    ) -> dict:
        """Link a tweet to the broadcast (proxsee associateTweetWithBroadcast)."""
        return await self.proxsee.associate_tweet_with_broadcast(
            space_id, tweet_id, tweet_external
        )

    async def aclose(self) -> None:
        await asyncio.gather(
            self._http.aclose(),
            self.proxsee._http.aclose(),
            self.chatman._http.aclose(),
        )


def _extract_space_id(value: str) -> str:
    """Accept a bare 13-char id or a full x.com/i/spaces/<id> URL."""
    value = value.strip().rstrip('/')
    if '/' in value:
        value = value.rsplit('/', 1)[-1]
    return value


def _normalize_audio_streams(streams: list[dict]) -> list[dict]:
    """Convert API/Janus stream records to the subscriber join shape."""
    normalized = []
    for stream in streams:
        feed = stream.get('feed') or stream.get('feed_id') or stream.get('id')
        if feed is None:
            continue
        normalized.append({
            'feed': feed,
            'mid': str(stream.get('mid') or stream.get('feed_mid') or '0'),
        })
    return normalized
