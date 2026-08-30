X Spaces
========

twifork can read, create, moderate and end X Spaces (audio rooms) using the
same cookie-authenticated session as the rest of the library — no API key
needed.

All Space operations live on ``client.spaces`` (a :class:`twikit.spaces.Spaces`
instance). Reading (metadata, search, HLS stream url, chat history) requires
nothing extra. Creating and ending a Space works too, driven by the same
proxsee + Janus HTTP flow the web client uses. Only the WebRTC **voice**
paths (speaking into / listening to the live audio) need the optional
packages:

.. code-block:: bash

    pip install twifork[spaces]   # aiortc + websockets

Quick tour
----------

.. code-block:: python

    import twikit

    client = twikit.Client(language='ja')
    client.set_cookies({'auth_token': '...', 'ct0': '...'})

    # metadata
    space = await client.spaces.get_space('1DXGydznBYWKM')
    print(space.state, space.title, space.media_key)

    # search live spaces
    live = await client.spaces.search('vtuber', filter='Live')

    # HLS stream url (listen/replay with ffmpeg: ffmpeg -i <url> out.m4a)
    stream = await client.spaces.get_stream(space.media_key)
    print(stream.hls_url)

    # chat history (works for live and replay)
    chat = await client.spaces.chat(space)
    for msg in await chat.history(limit=10):
        print(msg.body)

    # create a live space, confirm it, end it
    created = await client.spaces.create_space(
        title='hello from twifork',
        conversation_controls=2,          # 0=invited, 1=followed, 2=everyone
    )
    space_id = (created['broadcast'])['id']
    await client.spaces.end_space(space_id)

Reference
---------

Metadata / discovery
    - :meth:`twikit.spaces.Spaces.get_space` / ``get_space_by_url``
    - :meth:`twikit.spaces.Spaces.search` — hydrates the id-only search
      results by default; use ``hydrate=False`` to avoid the additional
      ``AudioSpaceById`` requests when only ids are needed.
    - :meth:`twikit.spaces.Spaces.topics`

Broadcast lifecycle
    - :meth:`twikit.spaces.Spaces.create_space` — creates and (by default)
      publishes the Space. ``scheduled_start_time`` is epoch milliseconds
      (seconds are converted automatically). Live publishing drives the Janus
      room flow internally; no extra packages needed.
    - :meth:`twikit.spaces.Spaces.end_space`
    - :meth:`twikit.spaces.Spaces.cancel_scheduled_space` /
      ``get_scheduled_spaces``

Streaming
    - :meth:`twikit.spaces.Spaces.get_stream` — HLS url + chat token from a
      media key.
    - :meth:`twikit.spaces.Spaces.host` — publish the creator's audio after
      ``create_space``; pass that method's response plus an audio track.
    - :meth:`twikit.spaces.Spaces.speak` / ``listen`` — guest publisher and
      listener WebRTC paths (requires ``aiortc``; experimental). ``listen``
      retries a failed Janus/DTLS negotiation up to three times by default.

Chat
    - :meth:`twikit.spaces.Spaces.chat` — history over HTTP plus a live
      WebSocket when the Space is running (requires ``websockets``).
    - :meth:`twikit.spaces.Spaces.stream_live_chat` — live chat via the web
      client's HTTP stream, no extra deps.

Moderation (host/admin)
    - mute/unmute speaker, mute/unmute space, raise/lower hand,
      approve/reject speaker requests, remove participant, add/remove admin,
      invite to speak, set space settings — all under
      :meth:`twikit.spaces.Spaces` shortcuts or :class:`twikit.spaces.ChatmanApi`.

Under the hood
--------------

Spaces are built on three services:

* **X GraphQL** — ``AudioSpaceById``, ``AudioSpaceSearch``,
  ``BrowseSpaceTopics``, ``AuthenticatePeriscope`` (current query ids are
  pinned in ``twikit/client/gql.py``).
* **proxsee** (``proxsee-cf.pscp.tv``) — the Periscope v2 API:
  ``loginTwitterToken`` (exchanges the AuthenticatePeriscope JWT for a
  session cookie), ``createBroadcast`` / ``publishBroadcast`` /
  ``endBroadcast``, ``accessChat(Public)``, ``turnServers``,
  ``getScheduledAudioBroadcasts``, …
* **Janus WebRTC gateway** (``gw-prod-*.pscp.tv``) — the audio room:
  create session → attach ``janus.plugin.videoroom`` → create room → join as
  publisher/subscriber → SDP exchange. TURN servers come from proxsee.

Chat history messages are parsed into :class:`twikit.spaces.ChatMessage`.
The live WebSocket speaks the chatman protocol (``/chatapi/v1/chatnow``,
auth + join control frames).

Measured chat behaviour against the production chatman:

* **History works for live and recorded replay** — ``chat.history()``
  returns both join/presence events and messages sent while the Space was
  live (message bodies come back with ``type`` 1 = Chat). An ended Space
  needs an actual recording: setting replay availability without publishing
  audio does not make X retain a replay chat token.
* **The history endpoint 404s for the first ~60 seconds** after a Space
  is created. Wait a bit (or retry) before calling ``history()``.
* **Sending is two-way verified.** ``chat.send()`` broadcasts over the
  chatnow WebSocket and every participant receives it; sent messages also
  end up in history. The message body ``type`` is the numeric X8 enum
  (1 = Chat) — the string ``"Chat"`` is rejected.
* **Use ``accessChat``, not ``accessChatPublic``.** Public access comes
  back ``read_only: true`` and ``send()`` refuses to work.
* **Ended Spaces 404 on ``live_video_stream/status``** — ``chat()`` falls
  back to the chat token embedded in the ``AudioSpaceById`` payload.
* ``stream_live_chat`` (the web client's ``/live-chat`` HTTP stream) is
  gated behind X feature flags and is not reliably reachable; prefer the
  WebSocket (``chat.listen()``) for live chat.

Tweet sharing (verified live):

* ``add_sharing(space_id, tweet_id)`` attaches a tweet to the Space
  (GraphQL ``audio_space_add_sharing`` with
  ``{audio_space_id, sharing: {shared_tweet: {tweet_id}}}`` — the old
  ``{id, twitterUserIds}`` shape fails with GRAPHQL_VALIDATION_FAILED).
* The sharing id comes back in ``AudioSpaceById`` as
  ``data.sharings.items[].sharing_id``; ``Space.sharing_ids`` lists them.
* ``delete_sharing(space_id, sharing_id)`` removes it again (verified:
  sharing_ids returns to ``[]``). The follow-up ``AudioSpaceById`` read is
  eventually consistent and can show the removed id for a few seconds.
* ``associate_tweet_with_broadcast`` (proxsee) links a tweet to the
  broadcast — returns ``{"success": true}``.

Speaking flow (measured):

* ``request_to_speak``/``cancel_speaker_request`` work standalone
  (chatman is initialized internally) on Spaces that require approval
  (``conversation_controls=0``); on ``=2`` (everyone) ``request_to_speak``
  403s because it is unnecessary.
* ``raise_hand``/``lower_hand`` (emoji reaction endpoints) return 403 for
  regular accounts — the endpoints appear restricted/deprecated on X's
  side; the speaker-request flow above is the supported path.
* A host can mute an active speaker. X deliberately refuses a host-side
  ``unmute_speaker`` with 403; the speaker must call it on their own session
  to turn their microphone back on. Both directions were verified live.
* ``stream_eject`` requires the speaker's Chatman session UUID plus their
  Janus handle, session, room and participant ids. The ids are available on
  the active :class:`twikit.spaces.SpaceVoiceSession`; a UUID-only payload is
  rejected by X with HTTP 400.

A full working example lives in ``examples/spaces.py``.

Multi-space operation (verified live):

* One account can participate in several Spaces at once, even from a
  single ``Client`` instance: ``join()`` and ``request_to_speak()``
  return a distinct ``session_uuid`` per Space, and the host sees the
  guest request on each Space's ``get_call_status()``.
* Simultaneous *speaking* works too: after the host approves both
  requests, ``speak()`` can publish to two Spaces at the same time (two
  independent Janus rooms); both sessions stay at ICE ``completed``.
  Measured with two Spaces, one account, ~10s+ hold.
* ``get_space().participants`` can lag behind real membership — use
  ``get_call_status()`` for authoritative participant/request state.

WebRTC voice notes (learned the hard way against the production SFU):

* **No TURN by default.** ``speak()`` / ``listen()`` connect directly
  (host candidates only) unless you pass ``ice_servers`` explicitly.
  X's TURN server (``turns:turn.pscp.tv:443``) tears the TLS connection
  down after ~60s, which silently kills the media path and makes the
  SFU drop the publisher. Direct connectivity avoids this entirely.
* **Publishers subscribe to their own feed.** ``host()`` and ``speak()``
  attach a second videoroom handle on the same Janus session and join as a
  subscriber of their own stream (mirroring the web client's ``t8``
  handle). Without this the backend marks the space
  ``TimedOut`` after about two minutes even while media flows.
* **Pace your audio track.** aiortc sends RTP as fast as
  ``AudioStreamTrack.recv()`` yields frames — it does not pace in real
  time. A naive track that returns 20ms frames back-to-back plays back
  at ~15x speed. Sleep ``0.02`` seconds per 20ms frame in ``recv()``.
* **Unmute + publishStream.** After the SDP offer, ``speak()`` calls
  ``audiospace/unmuteSpeaker`` (X starts hosts auto-muted) and
  ``audiospace/stream/publish`` (announces the published stream to the
  backend). Both are best-effort.
* **One poller per Janus session.** A second long-poll task on the same
  session steals events (the SDP answer gets lost and ICE stays
  ``new``). The session's main handle long-polls and dispatches events
  by ``sender`` handle id.
* **Wait for DTLS, not only ICE.** ICE can be ``completed`` while the peer
  connection remains ``connecting`` and receives no RTP. Voice methods only
  return after ``connectionState == 'connected'``; failed SDP tasks and DTLS
  timeouts are surfaced as :class:`twikit.spaces.SpaceError`.
