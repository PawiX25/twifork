import re

from setuptools import setup, find_packages

with open('README.md', encoding='utf-8') as f:
    long_description = f.read()

with open('./twikit/__init__.py') as f:
    version = re.findall(r"__version__ = '(.+)'", f.read())[0]


setup(
    name='twifork',
    version=version,
    author='PawiX25',
    packages=find_packages(include=['twikit', 'twikit.*']),
    install_requires=[
        # The client passes `proxy=` to httpx.AsyncClient and builds
        # AsyncHTTPTransport(proxy=...); both arrived in httpx 0.26, where
        # they replaced `proxies=`. Without a floor pip could resolve an older
        # httpx and `Client()` died on an unexpected keyword argument.
        'httpx[socks]>=0.26',
        'filetype',
        'beautifulsoup4',
        'pyotp',
        'lxml',
        'webvtt-py',
        'm3u8',
        'Js2Py-3.13'
    ],
    extras_require={
        'impersonate': ['curl_cffi>=0.7,<0.8'],
        # Spaces voice (WebRTC) + live chat WebSocket. Everything else in the
        # Spaces API (metadata, create/end, HLS listening, chat history)
        # works without these.
        'spaces': ['aiortc', 'websockets'],
    },
    # The floor is 3.10, not 3.8: `anext()` is a 3.10 builtin and the header
    # merges use the 3.9 dict `|` operator. Declaring 3.8 let pip install on
    # interpreters where the package cannot even be imported.
    python_requires='>=3.10',
    description='A maintained fork of twikit — Twitter/X API scraper for Python, no API key required.',
    keywords='twitter, x, twitter-api, x-api, scraper, twikit, fork, bot, no-api-key, internal-api, async, python',
    long_description=long_description,
    long_description_content_type='text/markdown',
    license='MIT',
    url='https://github.com/PawiX25/twifork',
    package_data={'twikit': ['py.typed']},
    classifiers=[
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Programming Language :: Python :: 3.13',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent'
    ]
)
