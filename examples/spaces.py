"""
X Spaces demo for twifork.

Requires a logged-in Client (auth_token + ct0 cookies), same as the rest of
twifork.

Read-only operations (get_space, search, stream url, chat history) need no
extra packages. Creating/ending a Space works without extras too — it drives
the same proxsee + Janus HTTP flow the web client uses. Only the WebRTC
voice paths (speak/listen over audio) require the optional deps:

    pip install twifork[spaces]

Run:

    python examples/spaces.py

This demo:
  1. fetches a Space by id (or searches for live ones),
  2. prints its stream URL (HLS) and latest chat messages,
  3. with SPACES_CREATE=1: creates a live Space, confirms it is Running,
     then ends it (a few seconds of visibility).
"""

import asyncio
import os

import twikit

# Any public Space id (the 13-char id or full URL).
SPACE_ID = os.environ.get('SPACE_ID', '1DXGydznBYWKM')


async def main() -> None:
    client = twikit.Client(language='ja')
    # Set cookies from your browser (auth_token + ct0). See the README for
    # cookie extraction.
    client.set_cookies({
        'auth_token': os.environ['AUTH_TOKEN'],
        'ct0': os.environ['CT0'],
    })
    user_id = await client.user_id()
    print(f'logged in as {user_id}')

    # 1. metadata
    space = await client.spaces.get_space(SPACE_ID)
    print(f'space: {space.state} | {space.title!r}')
    print(f'  host: {space.host_user_id} | speakers: {len(space.speaker_ids)}')

    # 2. search for live spaces
    live = await client.spaces.search('music', filter='Live')
    print(f'live search results: {len(live)}')
    for s in live[:3]:
        print('  -', s.id)

    # 3. HLS stream url (listen with ffmpeg: ffmpeg -i <url> out.m4a)
    if space.media_key:
        stream = await client.spaces.get_stream(space.media_key)
        print(f'HLS url: {stream.hls_url}')

    # 4. chat history (works for live and replay)
    try:
        chat = await client.spaces.chat(space)
        print(f'chat read_only={chat.read_only} ws={chat._ws is not None}')
        for msg in (await chat.history(limit=5))[:3]:
            print('  msg:', (msg.body or '')[:60])
    except Exception as e:
        print('chat unavailable:', e)

    # 5. create -> confirm live -> end (opt-in, visible action)
    if os.environ.get('SPACES_CREATE'):
        created = await client.spaces.create_space(
            title='twifork spaces demo',
            conversation_controls=2,
        )
        space_id = (created.get('broadcast') or {}).get('id')
        print(f'created {space_id}')
        await asyncio.sleep(3)
        live_space = await client.spaces.get_space(space_id)
        print(f'state: {live_space.state} (expect Running)')
        await client.spaces.end_space(space_id)
        print(f'ended {space_id}')

    await client.http.aclose()


if __name__ == '__main__':
    asyncio.run(main())
