#!/usr/bin/env python
# coding: utf-8
"""
instaLooter - Another API-less Instagram pictures and videos downloader

Usage:
    instaLooter <profile> <directory> [options]
    instaLooter hashtag <hashtag> <directory> [options]
    instaLooter (--help | --version)

Options:
    -n NUM, --num-to-dl NUM      Maximum number of new files to download
    -j JOBS, --jobs JOBS         Number of parallel threads to use to
                                 download files [default: 16]
    -v, --get-videos             Get videos as well as photos
    -m, --add-metadata           Add date and caption metadata to downloaded
                                 pictures (requires PIL/Pillow and piexif)
    -q, --quiet                  Do not produce any output
    -h, --help                   Display this message and quit
    -c CRED, --credentials CRED  Credentials to login to Instagram with if
                                 needed (format is login:password)
    --version                    Show program version and quit
"""

__author__ = "althonos"
__author_email__ = "martin.larralde@ens-cachan.fr"
__version__ = "0.2.3"

import docopt
import argparse
import copy
import datetime
import docopt
import gzip
import json
import os
import progressbar
import random
import re
import requests
import six
import sys
import threading
import time

from contextlib import closing
from bs4 import BeautifulSoup

try:
    import PIL.Image
    import piexif
except ImportError:
    PIL = None

try:
    import lxml
    PARSER = 'lxml'
except ImportError:
    PARSER = 'html.parser'


class InstaDownloader(threading.Thread):

    def __init__(self, owner):
        super(InstaDownloader, self).__init__()
        self.medias = owner._medias_queue
        self.directory = owner.directory
        self.add_metadata = owner.add_metadata
        self.owner = owner

        self.session = requests.Session()
        self.session.cookies = self.owner.session.cookies

        self._killed = False

    def run(self):

        while not self._killed:
            media = self.medias.get()
            if media is None:
                break
            if not media['is_video']:
                self._download_photo(media)
            else:
                self._download_video(media)

            self.owner.dl_count += 1

    def _add_metadata(self, path, metadata):
        """
        """

        if PIL is not None:

            img = PIL.Image.open(path)

            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st":{}, "thumbnail": None}

            exif_dict['0th'] = {
                piexif.ImageIFD.Artist: "Image creator, {}".format(self.owner.metadata['full_name']),
            }

            exif_dict['1st'] = {
                piexif.ImageIFD.Artist: "Image creator, {}".format(self.owner.metadata['full_name']),
            }

            exif_dict['Exif'] = {
                piexif.ExifIFD.DateTimeOriginal: datetime.datetime.fromtimestamp(metadata['date']).isoformat(),
                piexif.ExifIFD.UserComment: metadata.get('caption', '').encode('utf-8'),
            }

            img.save(path, exif=piexif.dump(exif_dict))

    def _download_photo(self, media):

        photo_url = media.get('display_src')
        photo_basename = os.path.basename(photo_url.split('?')[0])
        photo_name = os.path.join(self.directory, photo_basename)

        # save full-resolution photo
        self._dl(photo_url, photo_name)

        # put info from Instagram post into image metadata
        if self.add_metadata:
            self._add_metadata(photo_name, media)

    def _download_video(self, media):
        """
        """

        url = "https://www.instagram.com/p/{}/".format(media['code'])
        res = self.session.get(url)
        data = self.owner._get_shared_data(res)

        video_url = data["entry_data"]["PostPage"][0]["media"]["video_url"]
        video_basename = os.path.basename(video_url.split('?')[0])
        video_name = os.path.join(self.directory, video_basename)

        # save full-resolution photo
        self._dl(video_url, video_name)

    def _dl(self, source, dest):
        self.session.headers['Accept'] = '*/*'
        res = self.session.get(source)
        with open(dest, 'wb') as dest_file:
            for block in res.iter_content(1024):
                if block:
                    dest_file.write(block)

    def kill(self):
        self._killed = True


class InstaLooter(object):

    _RX_SHARED_DATA = re.compile(r'window._sharedData = ({[^\n]*});')

    URL_HOME = "https://www.instagram.com/"
    URL_LOGIN = "https://www.instagram.com/accounts/login/ajax/"
    URL_LOGOUT = "https://www.instagram.com/accounts/logout/"

    def __init__(self, directory, profile=None, hashtag=None, add_metadata=False, get_videos=False, jobs=16):

        if profile is not None:
            self.target = profile
            self._page_name = 'ProfilePage'
            self._section_name = 'user'
            self.base_url = "https://www.instagram.com/{}/"
        elif hashtag is not None:
            self.target = hashtag
            self._page_name = 'TagPage'
            self._section_name = 'tag'
            self.base_url = "https://www.instagram.com/explore/tags/{}/"
        else:
            raise ValueError("Either a tag or a profile name is required to create an InstaLooter instance")

        self.directory = directory
        self.add_metadata = add_metadata
        self.get_videos = get_videos
        self.jobs = jobs

        self.session = requests.Session()
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; WOW64; rv:50.0) Gecko/20100101 Firefox/50.0"
        self.csrftoken = None

        self.dl_count = 0
        self.metadata = {}

        self._workers = []

        self.session.headers.update({
            'User-Agent': self.user_agent,
            'Accept': 'text/html',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Host': 'www.instagram.com',
            'DNT': '1',
            'Upgrade-Insecure-Requests': '1',
        })

        # Create self.directory if it doesn't exist.
        if not os.path.exists(self.directory):
            os.makedirs(self.directory)

    def __del__(self):
        if hasattr(self, 'session'):
            try:
                self.session.close()
            except ReferenceError:
                pass
        if hasattr(self, "_workers"):
            for worker in self._workers:
                worker.kill()
        if hasattr(self, '_pbar'):
            self._pbar.finish()

    def login(self, username, password):
        """Log in with provided credentials.

        Code taken from LevPasha/instabot.py
        """
        self.session.cookies.update({
            'sessionid': '',
            'mid': '',
            'ig_pr': '1',
            'ig_vw': '1920',
            'csrftoken': '',
            's_network': '',
            'ds_user_id': ''
        })

        login_post = {'username': username,
                      'password': password}

        self.session.headers.update({
            'Origin': self.URL_HOME,
            'Referer': self.URL_HOME,
            'X-Instragram-AJAX': '1',
            'X-Requested-With': 'XMLHttpRequest',
        })

        res = self.session.get(self.URL_HOME)
        self.session.headers.update({'X-CSRFToken': res.cookies['csrftoken']})
        time.sleep(5 * random.random())

        login = self.session.post(self.URL_LOGIN, data=login_post, allow_redirects=True)
        self.session.headers.update({'X-CSRFToken': login.cookies['csrftoken']})
        self.csrftoken = login.cookies['csrftoken']

        if login.status_code != 200:
            self.csrftoken = None
            raise SystemError("Login error: check your connection")
        else:
            r = self.session.get(self.URL_HOME)
            if r.text.find(username) == -1:
                raise ValueError('Login error: check your login data')

    def logout(self):
        """Log out from current session

        Code taken from LevPasha/instabot.py
        """
        logout_post = {'csrfmiddlewaretoken': self.csrftoken}
        logout = self.session.post(self.URL_LOGOUT, data=logout_post)
        self.csrftoken = None

    def _init_workers(self):
        """Initialize a pool of workers to download files
        """
        self._shared_map = {}
        self._workers = []
        self._medias_queue = six.moves.queue.Queue()
        for _ in six.moves.range(self.jobs):
            worker = InstaDownloader(self)
            worker.start()
            self._workers.append(worker)

    def pages(self, media_count=None, with_pbar=False):
        """An iterator over the shared data of an instagram profile

        Create a connection to www.instagram.com and use successive
        GET requests to load all pages of a profile.
        Each page contains 12 media nodes, as well as metadata associated
        to the account.

        Arguments:
            - media_count (int, optional): how many media to show before
                stopping [default: None]
            - with_pbar (bool, optional): display a progress bar [default: False]

        Yields:
            dict: an dictionnary containing
        """

        url = self.base_url.format(self.target)
        while True:
            res = self.session.get(url)
            data = self._get_shared_data(res)

            if media_count is None:
                media_count = data['entry_data'][self._page_name][0][self._section_name]['media']['count']

            if with_pbar:
                if not 'max_id' in url:  # First page: init pbar
                    self._init_pbar(1, media_count//12 + 1, 'Loading pages |')
                else:  # Other pages: update pbar
                    if self._pbar.value  == self._pbar.max_value:
                        self._pbar.max_value += 1
                    self._pbar.update(self._pbar.value+1)



            if not 'max_id' in url and self._section_name=="user":
                self._parse_metadata_from_profile_page(data)

            yield data

            page_info = data['entry_data'][self._page_name][0][self._section_name]['media']['page_info']
            if not page_info['has_next_page']:
                break
            else:
                url = '{}?max_id={}'.format(self.base_url.format(self.target), page_info["end_cursor"])


    def medias(self, media_count=None, with_pbar=False):
        """
        """
        for page in self.pages(media_count=media_count, with_pbar=with_pbar):
            for media in page['entry_data'][self._page_name][0][self._section_name]['media']['nodes']:
                yield media

    def download_photos(self, media_count=None, with_pbar=False):
        self.download(media_count=media_count, with_pbar=with_pbar,
                      condition=lambda media: not media['is_video'])

    def download_videos(self, media_count=None, with_pbar=False):
        self.download(media_count=media_count, with_pbar=with_pbar,
                      condition=lambda media: media['is_video'])

    def download(self, media_count=None, with_pbar=False, condition=None):
        self._init_workers()
        if condition is None:
            condition = lambda media: (not media['is_video'] or self.get_videos)
        medias_queued = self._fill_media_queue(media_count=media_count, with_pbar=with_pbar,
                                               condition=condition)
        if with_pbar:
            self._init_pbar(self.dl_count, medias_queued, 'Downloading |')
        self._poison_workers()
        self._join_workers(with_pbar=with_pbar)

    def _get_shared_data(self, res):
        soup = BeautifulSoup(res.text, PARSER)
        script = soup.find('body').find('script', {'type': 'text/javascript'})
        return json.loads(self._RX_SHARED_DATA.match(script.text).group(1))

    def _fill_media_queue(self, media_count=None, with_pbar=False, condition=None):
        medias_queued = 0
        for media in self.medias(media_count=media_count, with_pbar=with_pbar):
            if condition(media):
                media_url = media.get('display_src')
                media_basename = os.path.basename(media_url.split('?')[0])
                if not os.path.exists(os.path.join(self.directory, media_basename)):
                    self._medias_queue.put(media)
                    medias_queued += 1
            if media_count is not None and medias_queued >= media_count:
                break
        return medias_queued

    def _join_workers(self, with_pbar=False):
        while any(w.is_alive() for w in self._workers):
            if with_pbar and hasattr(self, '_pbar'):
                self._pbar.update(self.dl_count)
        if with_pbar and hasattr(self, '_pbar'):
            self._pbar.update(self.dl_count)

    def _init_pbar(self, ini_val, max_val, label):
        self._pbar = progressbar.ProgressBar(
            min_value=0,
            max_value=max_val,
            initial_value=ini_val,
            widgets=[
                label,
                progressbar.Percentage(),
                '(', progressbar.SimpleProgress(), ')',
                progressbar.Bar(),
                progressbar.Timer(), ' ',
                '|', progressbar.ETA(),
            ]
        )
        self._pbar.start()

    def _poison_workers(self):
        for worker in self._workers:
            self._medias_queue.put(None)

    def _parse_metadata_from_profile_page(self, data):
        user = data["entry_data"][self._page_name][0]["user"]
        for k, v in six.iteritems(user):
            self.metadata[k] = copy.copy(v)
        self.metadata['follows'] = self.metadata['follows']['count']
        self.metadata['followed_by'] = self.metadata['followed_by']['count']
        del self.metadata['media']['nodes']

    def is_logged_in(self):
        return self.csrftoken is not None


def main(argv=sys.argv[1:]):
    # parse arguments
    args = docopt.docopt(__doc__, argv, version='instaLooter {}'.format(__version__))

    looter = InstaLooter(
        directory=os.path.expanduser(args['<directory>']),
        profile=args['<profile>'],hashtag=args['<hashtag>'],
        add_metadata=args['--add-metadata'], get_videos=args['--get-videos'],
        jobs=int(args['--jobs']))

    if args['--credentials']:
        login, password = args['--credentials'].split(':')
        looter.login(login, password)

    try:
        looter.download(
            media_count=int(args['--num-to-dl']) if args['--num-to-dl'] else None,
            with_pbar=not args['--quiet']
        )
    except KeyboardInterrupt:
        looter.__del__()

if __name__ == "__main__":
    main()
