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
        'httpx[socks]',
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
    },
    python_requires='>=3.8',
    description='A maintained fork of twikit — Twitter/X API scraper for Python, no API key required.',
    keywords='twitter, x, twitter-api, x-api, scraper, twikit, fork, bot, no-api-key, internal-api, async, python',
    long_description=long_description,
    long_description_content_type='text/markdown',
    license='MIT',
    url='https://github.com/PawiX25/twifork',
    package_data={'twikit': ['py.typed']}
)
