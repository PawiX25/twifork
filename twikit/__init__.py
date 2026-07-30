"""
==========================
twifork — Twitter/X API Wrapper
==========================

https://github.com/PawiX25/twifork
A maintained fork of twikit. A Python library for interacting with the Twitter/X API.
"""

__version__ = '2.3.5'

import asyncio
import os

# Importing a library must not reconfigure the whole process. Forcing the
# selector loop on Windows broke every importer that also needed subprocesses
# (playwright, adspower) with NotImplementedError. Opt in when you actually
# want it: TWIKIT_WINDOWS_SELECTOR_LOOP=1
if os.name == 'nt' and os.environ.get('TWIKIT_WINDOWS_SELECTOR_LOOP') == '1':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from ._captcha import Capsolver
from .bookmark import BookmarkFolder
from .errors import *
from .utils import SearchOptions, build_query
from .client.client import Client
from .community import Community, CommunityCreator, CommunityMember, CommunityRule
from .geo import Place
from .group import Group, GroupMessage
from .list import List
from .message import Conversation, Message
from .notification import Notification
from .trend import Trend
from .tweet import Article, CommunityNote, Poll, ScheduledTweet, Tweet
from .user import User
