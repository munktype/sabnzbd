#!/usr/bin/python -OO
# Copyright 2007-2018 The SABnzbd-Team <team@sabnzbd.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

"""
sabnzbd.nzbstuff - misc
"""

# Standard Library
import os
import time
import re
import logging
import datetime
import threading
import xml.sax
import xml.sax.handler
import xml.sax.xmlreader
import hashlib
import difflib

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

# SABnzbd modules
import sabnzbd
from sabnzbd.constants import GIGI, ATTRIB_FILE, JOB_ADMIN, \
    REPAIR_PRIORITY, TOP_PRIORITY, HIGH_PRIORITY, NORMAL_PRIORITY, \
    LOW_PRIORITY, DEFAULT_PRIORITY, PAUSED_PRIORITY, DUP_PRIORITY, STOP_PRIORITY, \
    RENAMES_FILE, MAX_BAD_ARTICLES, Status, PNFO
from sabnzbd.misc import to_units, cat_to_opts, cat_convert, sanitize_foldername, \
    get_unique_path, get_admin_path, remove_all, sanitize_filename, globber_full, \
    int_conv, set_permissions, format_time_string, long_path, trim_win_path, \
    fix_unix_encoding, calc_age, is_obfuscated_filename, get_ext, get_filename, \
    get_unique_filename, renamer, remove_file, remove_dir, caller_name
from sabnzbd.decorators import synchronized
import sabnzbd.config as config
import sabnzbd.cfg as cfg
from sabnzbd.encoding import unicoder, platform_encode
from sabnzbd.database import HistoryDB
from sabnzbd.rating import Rating

__all__ = ['Article', 'NzbFile', 'NzbObject']

# Name patterns
SUBJECT_FN_MATCHER = re.compile(r'"([^"]*)"')
RE_NORMAL_NAME = re.compile(r'\.\w{1,5}$')  # Test reasonably sized extension at the end
RE_QUICK_PAR2_CHECK = re.compile(r'\.par2\W*', re.I)
RE_RAR = re.compile(r'(\.rar|\.r\d\d|\.s\d\d|\.t\d\d|\.u\d\d|\.v\d\d)$', re.I)
RE_PROPER = re.compile(r'(^|[\. _-])(PROPER|REAL|REPACK)([\. _-]|$)')


##############################################################################
# Trylist
##############################################################################

TRYLIST_LOCK = threading.Lock()

class TryList(object):
    """ TryList keeps track of which servers have been tried for a specific article
    """
    # Pre-define attributes to save memory
    __slots__ = ('try_list', 'fetcher_priority')

    def __init__(self):
        self.try_list = []
        self.fetcher_priority = 0

    def server_in_try_list(self, server):
        """ Return whether specified server has been tried """
        with TRYLIST_LOCK:
            return server in self.try_list

    def add_to_try_list(self, server):
        """ Register server as having been tried already """
        with TRYLIST_LOCK:
            if server not in self.try_list:
                self.try_list.append(server)

    def reset_try_list(self):
        """ Clean the list """
        with TRYLIST_LOCK:
            self.try_list = []

    def __getstate__(self):
        """ Save the servers """
        return [server.id for server in self.try_list]

    def __setstate__(self, servers_ids):
        self.try_list = []
        for server_id in servers_ids:
            if server_id in sabnzbd.downloader.Downloader.do.server_dict:
                self.add_to_try_list(sabnzbd.downloader.Downloader.do.server_dict[server_id])


##############################################################################
# Article
##############################################################################
ArticleSaver = (
    'article', 'art_id', 'bytes', 'partnum', 'lowest_partnum', 'nzf'
)


class Article(TryList):
    """ Representation of one article """
    # Pre-define attributes to save memory
    __slots__ = ArticleSaver + ('fetcher', 'fetcher_priority', 'tries')

    def __init__(self, article, bytes, partnum, nzf):
        TryList.__init__(self)
        self.fetcher = None
        self.article = article
        self.art_id = None
        self.bytes = bytes
        self.partnum = partnum
        self.lowest_partnum = False
        self.tries = 0  # Try count
        self.nzf = nzf

    def get_article(self, server, servers):
        """ Return article when appropriate for specified server """
        log = sabnzbd.LOG_ALL
        if not self.fetcher and not self.server_in_try_list(server):
            if log:
                logging.debug('Article %s | Server: %s | in second if', self.article, server.host)
            # Is the current selected server of the same priority as this article?
            if log:
                logging.debug('Article %s | Server: %s | Article priority: %s', self.article, server.host, self.fetcher_priority)
            if log:
                logging.debug('Article %s | Server: %s | Server priority: %s', self.article, server.host, server.priority)
            if server.priority == self.fetcher_priority:
                if log:
                    logging.debug('Article %s | Server: %s | same priority, use it', self.article, server.host)
                self.fetcher = server
                self.tries += 1
                if log:
                    logging.debug('Article %s | Server: %s | Article-try: %s', self.article, server.host, self.tries)
                return self
            else:
                if log:
                    logging.debug('Article %s | Server: %s | not the same priority', self.article, server.host)
                # No, so is it a lower priority?
                if server.priority > self.fetcher_priority:
                    if log:
                        logging.debug('Article %s | Server: %s | lower priority', self.article, server.host)
                    # Is there an available server that is a higher priority?
                    found_priority = 1000
                    # for server_check in config.get_servers():
                    for server_check in servers:
                        if log:
                            logging.debug('Article %s | Server: %s | checking', self.article, server.host)
                        # if (server_check.priority() < found_priority and server_check.priority() < server.priority and not self.server_in_try_list(server_check)):
                        if server_check.active and (server_check.priority < found_priority):
                            if server_check.priority < server.priority:
                                if (not self.server_in_try_list(server_check)):
                                    if log:
                                        logging.debug('Article %s | Server: %s | setting found priority to %s', self.article, server.host, server_check.priority)
                                    found_priority = server_check.priority
                    if found_priority == 1000:
                        # If no higher priority servers, use this server
                        self.fetcher_priority = server.priority
                        self.fetcher = server
                        self.tries += 1
                        if log:
                            logging.debug('Article %s | Server: %s | Article-try: %s', self.article, server.host, self.tries)
                        return self
                    else:
                        # There is a higher priority server, so set article priority
                        if log:
                            logging.debug('Article %s | Server: %s | setting self priority', self.article, server.host)
                        self.fetcher_priority = found_priority
        if log:
            logging.debug('Article %s | Server: %s | Returning None', self.article, server.host)
        return None

    def get_art_id(self):
        """ Return unique article storage name, create if needed """
        if not self.art_id:
            self.art_id = sabnzbd.get_new_id("article", self.nzf.nzo.workpath)
        return self.art_id

    def __getstate__(self):
        """ Save to pickle file, selecting attributes """
        dict_ = {}
        for item in ArticleSaver:
            dict_[item] = getattr(self, item)
        dict_['try_list'] = TryList.__getstate__(self)
        return dict_

    def __setstate__(self, dict_):
        """ Load from pickle file, selecting attributes """
        for item in ArticleSaver:
            try:
                setattr(self, item, dict_[item])
            except KeyError:
                # Handle new attributes
                setattr(self, item, None)
        TryList.__setstate__(self, dict_.get('try_list', []))
        self.fetcher_priority = 0
        self.fetcher = None
        self.tries = 0

    def __repr__(self):
        return "<Article: article=%s, bytes=%s, partnum=%s, art_id=%s>" % \
               (self.article, self.bytes, self.partnum, self.art_id)


##############################################################################
# NzbFile
##############################################################################
NzbFileSaver = (
    'date', 'subject', 'filename', 'filename_checked', 'type', 'is_par2', 'vol',
    'blocks', 'setname', 'articles', 'decodetable', 'bytes', 'bytes_left',
    'article_count', 'nzo', 'nzf_id', 'deleted', 'valid', 'import_finished',
    'md5sum', 'md5of16k'
)


class NzbFile(TryList):
    """ Representation of one file consisting of multiple articles """
     # Pre-define attributes to save memory
    __slots__ = NzbFileSaver

    def __init__(self, date, subject, article_db, bytes, nzo):
        """ Setup object """
        TryList.__init__(self)

        self.date = date
        self.subject = subject
        self.type = None
        self.filename = name_extractor(subject)
        self.filename_checked = False

        self.is_par2 = False
        self.vol = None
        self.blocks = None
        self.setname = None

        self.articles = []
        self.decodetable = {}

        self.bytes = bytes
        self.bytes_left = bytes
        self.article_count = 0

        self.nzo = nzo
        self.nzf_id = sabnzbd.get_new_id("nzf", nzo.workpath)
        self.deleted = False
        self.valid = False
        self.import_finished = False

        self.md5sum = None
        self.md5of16k = None

        self.valid = bool(article_db)

        if self.valid and self.nzf_id:
            # Save first article seperate, but not for all but first par2 file
            # Non-par2 files and the first par2 will have no volume and block number
            # When DirectUnpack is disabled, do not do any of this to also preserve disk IO
            setname, vol, block = sabnzbd.par2file.analyse_par2(self.filename)
            if cfg.direct_unpack() and not vol and not block:
                first_num = min(article_db.keys())
                first_article = self.add_article(article_db.pop(first_num), first_num)
                first_article.lowest_partnum = True
                self.nzo.first_articles.append(first_article)

            # Any articles left?
            if article_db:
                # Save the rest
                sabnzbd.save_data(article_db, self.nzf_id, nzo.workpath)
            else:
                # All imported
                self.import_finished = True

    def finish_import(self):
        """ Load the article objects from disk """
        logging.debug("Finishing import on %s", self.subject)
        article_db = sabnzbd.load_data(self.nzf_id, self.nzo.workpath, remove=False)
        if article_db:
            for partnum in article_db:
                self.add_article(article_db[partnum], partnum)

            # Make sure we have labeled the lowest part number
            # Also when DirectUnpack is disabled we need to know
            self.decodetable[min(self.decodetable)].lowest_partnum = True

            # Mark safe to continue
            self.import_finished = True

    def add_article(self, article_info, partnum):
        """ Add article to object database and return article object """
        article = Article(article_info[0], article_info[1], partnum, self)
        self.articles.append(article)
        self.decodetable[partnum] = article
        return article

    def remove_article(self, article, found):
        """ Handle completed article, possibly end of file """
        if article in self.articles:
            self.articles.remove(article)
            if found:
                self.bytes_left -= article.bytes

        return (not self.articles)

    def set_par2(self, setname, vol, blocks):
        """ Designate this this file as a par2 file """
        self.is_par2 = True
        self.setname = setname
        self.vol = vol
        self.blocks = int(blocks)

    def get_article(self, server, servers):
        """ Get next article to be downloaded """
        for article in self.articles:
            article = article.get_article(server, servers)
            if article:
                return article
        self.add_to_try_list(server)

    def reset_all_try_lists(self):
        """ Clear all lists of visited servers """
        for art in self.articles:
            art.reset_try_list()
        self.reset_try_list()

    @property
    def completed(self):
        """ Is this file completed? """
        return self.import_finished and not bool(self.articles)

    def remove_admin(self):
        """ Remove article database from disk (sabnzbd_nzf_<id>)"""
        try:
            logging.debug('Removing article database for %s', self.nzf_id)
            remove_file(os.path.join(self.nzo.workpath, self.nzf_id))
        except:
            pass

    def __getstate__(self):
        """ Save to pickle file, selecting attributes """
        dict_ = {}
        for item in NzbFileSaver:
            dict_[item] = getattr(self, item)
        dict_['try_list'] = TryList.__getstate__(self)
        return dict_

    def __setstate__(self, dict_):
        """ Load from pickle file, selecting attributes """
        for item in NzbFileSaver:
            try:
                setattr(self, item, dict_[item])
            except KeyError:
                # Handle new attributes
                setattr(self, item, None)
        TryList.__setstate__(self, dict_.get('try_list', []))

    def __repr__(self):
        return "<NzbFile: filename=%s, type=%s>" % (self.filename, self.type)


##############################################################################
# NzbParser
##############################################################################
class NzbParser(xml.sax.handler.ContentHandler):
    """ Forgiving parser for NZB's """

    def __init__(self, nzo):
        self.nzo = nzo
        self.in_nzb = False
        self.in_file = False
        self.in_groups = False
        self.in_group = False
        self.in_segments = False
        self.in_segment = False
        self.in_head = False
        self.in_meta = False
        self.meta_type = ''
        self.meta_types = {}
        self.meta_content = []
        self.filename = ''
        self.avg_age = 0
        self.valids = 0
        self.skipped_files = 0
        self.nzf_list = []
        self.groups = []
        self.md5 = hashlib.md5()
        self.now = time.time()

    def startDocument(self):
        pass

    def startElement(self, name, attrs):
        if name == 'segment' and self.in_nzb and self.in_file and self.in_segments:
            try:
                self.seg_bytes = int(attrs.get('bytes'))
                self.article_nr = int(attrs.get('number'))
            except ValueError:
                return
            self.article_id = []
            self.in_segment = True

        elif name == 'segments' and self.in_nzb and self.in_file:
            self.in_segments = True

        elif name == 'file' and self.in_nzb:
            subject = attrs.get('subject', '').strip()
            self.filename = subject
            self.in_file = True
            self.fileSubject = subject
            try:
                self.file_date = int(attrs.get('date'))
            except:
                # NZB has non-standard timestamp, assume now
                self.file_date = self.now
            self.article_db = {}
            self.file_bytes = 0

        elif name == 'group' and self.in_nzb and self.in_file and self.in_groups:
            self.in_group = True
            self.group_name = []

        elif name == 'groups' and self.in_nzb and self.in_file:
            self.in_groups = True

        elif name == 'head' and self.in_nzb:
            self.in_head = True

        elif name == 'meta' and self.in_nzb and self.in_head:
            self.in_meta = True
            meta_type = attrs.get('type')
            if meta_type:
                self.meta_type = meta_type.lower()
            self.meta_content = []

        elif name == 'nzb':
            self.in_nzb = True

    def characters(self, content):
        if self.in_group:
            self.group_name.append(content)
        elif self.in_segment:
            self.article_id.append(content)
        elif self.in_meta:
            self.meta_content.append(content)

    def endElement(self, name):
        if name == 'group' and self.in_group:
            group = str(''.join(self.group_name))
            if group not in self.groups:
                self.groups.append(group)
            self.in_group = False

        elif name == 'segment' and self.in_segment:
            partnum = self.article_nr
            segm = str(''.join(self.article_id))
            self.md5.update(segm)
            if partnum in self.article_db:
                if segm != self.article_db[partnum][0]:
                    msg = 'Duplicate part %s, but different ID-s (%s // %s)' % (partnum, self.article_db[partnum][0], segm)
                    logging.info(msg)
                    self.nzo.increase_bad_articles_counter('duplicate_articles')
                else:
                    logging.info("Skipping duplicate article (%s)", segm)
            else:
                self.article_db[partnum] = (segm, self.seg_bytes)
                self.file_bytes += self.seg_bytes
            self.in_segment = False

        elif name == 'groups' and self.in_groups:
            self.in_groups = False

        elif name == 'segments' and self.in_segments:
            self.in_segments = False

        elif name == 'file' and self.in_file:
            # Create an NZF
            self.in_file = False
            if not self.article_db:
                msg = T('File %s is empty, skipping')
                if cfg.ignore_empty_files():
                    logging.info(msg, self.filename)
                else:
                    logging.warning(msg, self.filename)
                return
            try:
                tm = datetime.datetime.fromtimestamp(self.file_date)
            except:
                tm = datetime.datetime.fromtimestamp(self.now)
                self.file_date = self.now

            nzf = NzbFile(tm, self.filename, self.article_db, self.file_bytes, self.nzo)

            # Check if file was added with same name
            if cfg.reject_duplicate_files():
                nzo_matches = filter(lambda x: (x.filename == nzf.filename), self.nzo.files)
                if nzo_matches:
                    logging.info('File %s occured twice in NZB, discarding smaller file', nzf.filename)

                    # Which is smaller? Current or old one
                    if nzo_matches[0].bytes >= nzf.bytes:
                        # Skip this new one
                        return
                    else:
                        # Have to remove the old one but still add the new one
                        self.nzo.files.remove(nzo_matches[0])
                        del self.nzo.files_table[nzo_matches[0].nzf_id]
                        self.nzo.bytes -= nzo_matches[0].bytes
                        self.avg_age -= time.mktime(nzo_matches[0].date.timetuple())
                        self.valids -= 1
                        self.nzf_list.remove(nzo_matches[0])

            if nzf.valid and nzf.nzf_id:
                logging.info('File %s - %s added to queue', nzf.filename, nzf.nzf_id)
                self.nzo.files.append(nzf)
                self.nzo.files_table[nzf.nzf_id] = nzf
                self.nzo.bytes += nzf.bytes
                self.avg_age += self.file_date
                self.valids += 1
                self.nzf_list.append(nzf)
            else:
                logging.info('Error importing %s, skipping', self.filename)
                if nzf.nzf_id:
                    sabnzbd.remove_data(nzf.nzf_id, self.nzo.workpath)
                self.skipped_files += 1

        elif name == 'head':
            self.in_head = False

        elif name == 'meta':
            self.in_meta = False
            if self.meta_type:
                if self.meta_type not in self.meta_types:
                    self.meta_types[self.meta_type] = []
                self.meta_types[self.meta_type].append(''.join(self.meta_content))

        elif name == 'nzb':
            self.in_nzb = False

    def endDocument(self):
        """ End of the file """
        self.nzo.groups = self.groups
        self.nzo.meta = self.meta_types
        logging.debug('META-DATA = %s', self.nzo.meta)
        files = max(1, self.valids)
        self.nzo.avg_stamp = self.avg_age / files
        self.nzo.avg_date = datetime.datetime.fromtimestamp(self.avg_age / files)
        self.nzo.md5sum = self.md5.hexdigest()
        if self.skipped_files:
            logging.warning(T('Failed to import %s files from %s'),
                            self.skipped_files, self.nzo.filename)


##############################################################################
# NzbObject
##############################################################################
NzbObjectSaver = (
    'filename', 'work_name', 'final_name', 'created', 'bytes', 'bytes_downloaded', 'bytes_tried',
    'bytes_missing', 'repair', 'unpack', 'delete', 'script', 'cat', 'url', 'groups', 'avg_date',
    'md5of16k', 'partable', 'extrapars', 'md5packs', 'files', 'files_table', 'finished_files',
    'status', 'avg_bps_freq', 'avg_bps_total', 'priority', 'saved_articles', 'nzo_id',
    'futuretype', 'deleted', 'parsed', 'action_line', 'unpack_info', 'fail_msg', 'nzo_info',
    'custom_name', 'password', 'next_save', 'save_timeout', 'encrypted', 'bad_articles',
    'duplicate', 'oversized', 'precheck', 'incomplete', 'reuse', 'meta', 'first_articles',
    'md5sum', 'servercount', 'unwanted_ext', 'renames', 'rating_filtered'
)

# Lock to prevent errors when saving the NZO data
NZO_LOCK = threading.RLock()


class NzbObject(TryList):

    def __init__(self, filename, pp, script, nzb=None,
                 futuretype=False, cat=None, url=None,
                 priority=NORMAL_PRIORITY, nzbname=None, status="Queued", nzo_info=None,
                 reuse=False, dup_check=True):
        TryList.__init__(self)

        filename = platform_encode(filename)
        nzbname = platform_encode(nzbname)

        self.filename = filename    # Original filename
        if nzbname and nzb:
            work_name = nzbname     # Use nzbname if set and only for non-future slot
        else:
            work_name = filename

        # If non-future: create safe folder name stripped from ".nzb" and junk
        if nzb and work_name and work_name.lower().endswith('.nzb'):
            dname, ext = os.path.splitext(work_name)  # Used for folder name for final unpack
            if ext.lower() == '.nzb':
                work_name = dname
        work_name, password = scan_password(work_name)
        if not work_name:
            work_name = filename
        if nzb and work_name and not reuse:
            work_name = sanitize_foldername(work_name)
        if not work_name:
            # In case only /password was entered for nzbname
            work_name = filename

        # Check for password also in filename
        if not password:
            dummy, password = scan_password(os.path.splitext(filename)[0])

        # Remove trailing .nzb and .par(2)
        self.work_name = create_work_name(work_name)
        self.final_name = create_work_name(work_name)

        # Determine category and find pp/script values based on input
        # Later will be re-evaluated based on import steps
        if pp is None:
            r = u = d = None
        else:
            r, u, d = sabnzbd.pp_to_opts(pp)
        self.set_priority(priority) # Parse priority of input
        self.repair = r             # True if we want to repair this set
        self.unpack = u             # True if we want to unpack this set
        self.delete = d             # True if we want to delete this set
        self.script = script        # External script for this set
        self.cat = cat              # User-set category

        # Information fields
        self.url = url or filename
        self.groups = []
        self.avg_date = datetime.datetime.fromtimestamp(0.0)
        self.avg_stamp = 0.0        # Avg age in seconds (calculated from avg_age)

        # Bookkeeping values
        self.meta = {}
        self.servercount = {}       # Dict to keep bytes per server
        self.created = False        # dirprefixes + work_name created
        self.direct_unpacker = None # Holds the DirectUnpacker instance
        self.bytes = 0              # Original bytesize
        self.bytes_downloaded = 0   # Downloaded byte
        self.bytes_tried = 0        # Which bytes did we try
        self.bytes_missing = 0      # Bytes missing
        self.bad_articles = 0       # How many bad (non-recoverable) articles

        self.partable = {}          # Holds one parfile-name for each set
        self.extrapars = {}         # Holds the extra parfile names for all sets
        self.md5packs = {}          # Holds the md5pack for each set (name: hash)
        self.md5of16k = {}          # Holds the md5s of the first-16k of all files in the NZB (hash: name)

        self.files = []             # List of all NZFs
        self.files_table = {}       # Dictionary of NZFs indexed using NZF_ID
        self.renames = {}           # Dictionary of all renamed files

        self.finished_files = []    # List of all finished NZFs

        # The current status of the nzo eg:
        # Queued, Downloading, Repairing, Unpacking, Failed, Complete
        self.status = status
        self.avg_bps_freq = 0
        self.avg_bps_total = 0

        self.first_articles = []
        self.saved_articles = []

        self.nzo_id = None

        self.futuretype = futuretype
        self.deleted = False
        self.to_be_removed = False
        self.parsed = False
        self.duplicate = False
        self.oversized = False
        self.precheck = False
        self.incomplete = False
        self.unwanted_ext = 0
        self.rating_filtered = 0
        self.reuse = reuse
        if self.status == Status.QUEUED and not reuse:
            self.precheck = cfg.pre_check()
            if self.precheck:
                self.status = Status.CHECKING

        # Store one line responses for filejoin/par2/unrar/unzip here for history display
        self.action_line = ''
        # Store the results from various filejoin/par2/unrar/unzip stages
        self.unpack_info = {}
        # Stores one line containing the last failure
        self.fail_msg = ''
        # Stores various info about the nzo to be
        self.nzo_info = nzo_info or {}

        # Temporary store for custom foldername - needs to be stored because of url fetching
        self.custom_name = nzbname

        self.password = password
        self.next_save = None
        self.save_timeout = None
        self.encrypted = 0
        self.url_wait = None
        self.url_tries = 0
        self.pp_active = False  # Signals active post-processing (not saved)
        self.md5sum = None

        if nzb is None:
            # This is a slot for a future NZB, ready now
            return

        # Apply conversion option to final folder
        if cfg.replace_spaces():
            logging.info('Replacing spaces with underscores in %s', self.final_name)
            self.final_name = self.final_name.replace(' ', '_')
        if cfg.replace_dots():
            logging.info('Replacing dots with spaces in %s', self.final_name)
            self.final_name = self.final_name.replace('.', ' ')

        # Determine "incomplete" folder
        wdir = long_path(os.path.join(cfg.download_dir.get_path(), self.work_name))
        adir = os.path.join(wdir, JOB_ADMIN)

        # Check against identical checksum or series/season/episode
        if (not reuse) and nzb and dup_check and priority != REPAIR_PRIORITY:
            duplicate, series = self.has_duplicates()
        else:
            duplicate = series = 0

        if reuse:
            remove_all(adir, 'SABnzbd_nz?_*', True)
            remove_all(adir, 'SABnzbd_article_*', True)
        else:
            wdir = trim_win_path(wdir)
            wdir = get_unique_path(wdir, create_dir=True)
            set_permissions(wdir)
            adir = os.path.join(wdir, JOB_ADMIN)

        if not os.path.exists(adir):
            os.mkdir(adir)
        dummy, self.work_name = os.path.split(wdir)
        self.created = True

        # Must create a lower level XML parser because we must
        # disable the reading of the DTD file from an external website
        # by setting "feature_external_ges" to 0.

        if nzb and '<nzb' in nzb:
            handler = NzbParser(self)
            parser = xml.sax.make_parser()
            parser.setFeature(xml.sax.handler.feature_external_ges, 0)
            parser.setContentHandler(handler)
            parser.setErrorHandler(xml.sax.handler.ErrorHandler())
            inpsrc = xml.sax.xmlreader.InputSource()
            inpsrc.setByteStream(StringIO(nzb))
            try:
                parser.parse(inpsrc)
            except xml.sax.SAXParseException, err:
                self.incomplete = True
                if '</nzb>' not in nzb:
                    logging.warning(T('Incomplete NZB file %s'), filename)
                else:
                    logging.warning(T('Invalid NZB file %s, skipping (reason=%s, line=%s)'),
                                    filename, err.getMessage(), err.getLineNumber())
            except Exception, err:
                self.incomplete = True
                logging.warning(T('Invalid NZB file %s, skipping (reason=%s, line=%s)'), filename, err, 0)
                logging.info("Traceback: ", exc_info=True)

            if self.incomplete:
                if cfg.allow_incomplete_nzb():
                    self.pause()
                else:
                    self.purge_data()
                    raise ValueError

            sabnzbd.backup_nzb(filename, nzb)
            sabnzbd.save_compressed(adir, filename, nzb)

        if not self.files and not reuse:
            self.purge_data(keep_basic=False)
            if cfg.warn_empty_nzb():
                mylog = logging.warning
            else:
                mylog = logging.info
            if self.url:
                mylog(T('Empty NZB file %s') + ' [%s]', filename, self.url)
            else:
                mylog(T('Empty NZB file %s'), filename)
            raise ValueError

        if cat is None:
            for metacat in self.meta.get('category', ()):
                metacat = cat_convert(metacat)
                if metacat:
                    cat = metacat
                    break

        if cat is None:
            for grp in self.groups:
                cat = cat_convert(grp)
                if cat:
                    break

        # Pickup backed-up attributes when re-using
        if reuse:
            cat, pp, script, priority, name, password, self.url = get_attrib_file(self.workpath, 7)
            cat = unicoder(cat, True)
            script = unicoder(script, True)
            if name:
                self.final_name = unicoder(name, True)
            if password:
                self.password = unicoder(password, True)

        # Determine category and find pp/script values
        self.cat, pp_tmp, self.script, priority = cat_to_opts(cat, pp, script, priority)
        self.set_priority(priority)
        self.repair, self.unpack, self.delete = sabnzbd.pp_to_opts(pp_tmp)

        # Run user pre-queue script if needed
        if not reuse and cfg.pre_script():
            accept, name, pp, cat_pp, script_pp, priority, group = \
                sabnzbd.newsunpack.pre_queue(self.final_name_pw_clean, pp, cat, script,
                                             priority, self.bytes, self.groups)
            # Accept or reject
            accept = int_conv(accept)
            if accept < 1:
                self.purge_data()
                raise TypeError
            if accept == 2:
                self.fail_msg = T('Pre-queue script marked job as failed')

            # Process all options, only over-write if set by script
            # Beware that cannot do "if priority/pp", because those can
            # also have a valid value of 0, which shouldn't be ignored
            if name:
                self.set_final_name_pw(name)
            try:
                pp = int(pp)
            except:
                pp = None
            if cat_pp:
                cat = cat_pp
            try:
                priority = int(priority)
            except:
                priority = DEFAULT_PRIORITY
            if script_pp:
                script = script_pp
            if group:
                self.groups = [str(group)]

            # Re-evaluate results from pre-queue script
            self.cat, pp, self.script, priority = cat_to_opts(cat, pp, script, priority)
            self.set_priority(priority)
            self.repair, self.unpack, self.delete = sabnzbd.pp_to_opts(pp)
        else:
            accept = 1

        # Pause job when above size limit
        limit = cfg.size_limit.get_int()
        if not reuse and abs(limit) > 0.5 and self.bytes > limit:
            logging.info('Job too large, forcing low prio and paused (%s)', self.work_name)
            self.pause()
            self.oversized = True
            self.priority = LOW_PRIORITY

        if duplicate and ((not series and cfg.no_dupes() == 1) or (series and cfg.no_series_dupes() == 1)):
            if cfg.warn_dupl_jobs():
                logging.warning(T('Ignoring duplicate NZB "%s"'), filename)
            self.purge_data(keep_basic=False)
            raise TypeError

        if duplicate and ((not series and cfg.no_dupes() == 3) or (series and cfg.no_series_dupes() == 3)):
            if cfg.warn_dupl_jobs():
                logging.warning(T('Failing duplicate NZB "%s"'), filename)
            # Move to history, utilizing the same code as accept&fail from pre-queue script
            self.fail_msg = T('Duplicate NZB')
            accept = 2
            duplicate = False

        if duplicate or self.priority == DUP_PRIORITY:
            if cfg.no_dupes() == 4 or cfg.no_series_dupes() == 4:
                if cfg.warn_dupl_jobs():
                    logging.warning('%s: "%s"', T('Duplicate NZB'), filename)
                self.duplicate = True
                self.priority = NORMAL_PRIORITY
            else:
                if cfg.warn_dupl_jobs():
                    logging.warning(T('Pausing duplicate NZB "%s"'), filename)
                self.duplicate = True
                self.pause()
                self.priority = NORMAL_PRIORITY

        if self.priority == PAUSED_PRIORITY:
            self.pause()
            self.priority = NORMAL_PRIORITY

        if reuse:
            self.check_existing_files(wdir)

        # Perform sorting
        self.sort_nzfs()

        # Copy meta fields to nzo_info, if not already set
        for kw in self.meta:
            if not self.nzo_info.get(kw):
                self.nzo_info[kw] = self.meta[kw][0]

        # Show first meta-password (if any), when there's no explicit password
        if not self.password and self.meta.get('password'):
            self.password = self.meta.get('password', [None])[0]

        # Set nzo save-delay to minimum 120 seconds
        self.save_timeout = max(120, min(6.0 * float(self.bytes) / GIGI, 300.0))

        # In case pre-queue script or duplicate check want to move
        # to history we first need an nzo_id by entering the NzbQueue
        if accept == 2:
            self.deleted = True
            self.status = Status.FAILED
            sabnzbd.NzbQueue.do.add(self, quiet=True)
            sabnzbd.NzbQueue.do.end_job(self)
            # Raise error, so it's not added
            raise TypeError

    @synchronized(NZO_LOCK)
    def update_download_stats(self, bps, serverid, bytes):
        if bps:
            self.avg_bps_total += bps / 1024
            self.avg_bps_freq += 1
        if serverid in self.servercount:
            self.servercount[serverid] += bytes
        else:
            self.servercount[serverid] = bytes

    @synchronized(NZO_LOCK)
    def remove_nzf(self, nzf):
        if nzf in self.files:
            self.files.remove(nzf)
        if nzf not in self.finished_files:
            self.finished_files.append(nzf)
        nzf.import_finished = True
        nzf.deleted = True
        return not bool(self.files)

    def sort_nzfs(self):
        """ Sort the files in the NZO, respecting
            date sorting and unwanted extensions
        """
        if cfg.auto_sort():
            self.files.sort(cmp=nzf_cmp_date)
        else:
            self.files.sort(cmp=nzf_cmp_name)

        # In the hunt for Unwanted Extensions:
        # The file with the unwanted extension often is in the first or the last rar file
        # So put the last rar immediately after the first rar file so that it gets detected early
        if cfg.unwanted_extensions() and not cfg.auto_sort():
            # ... only useful if there are unwanted extensions defined and there is no sorting on date
            logging.debug('Unwanted Extension: putting last rar after first rar')
            nzfposcounter = firstrarpos = lastrarpos = 0
            for nzf in self.files:
                nzfposcounter += 1
                if '.rar' in str(nzf):
                    # a NZF found with '.rar' in the name
                    if firstrarpos == 0:
                        # this is the first .rar found, so remember this position
                        firstrarpos = nzfposcounter
                    lastrarpos = nzfposcounter
                    lastrarnzf = nzf    # The NZF itself

            if firstrarpos != lastrarpos:
                # at least two different .rar's found
                logging.debug('Unwanted Extension: First rar at %s, Last rar at %s', firstrarpos, lastrarpos)
                logging.debug('Unwanted Extension: Last rar is %s', str(lastrarnzf))
                try:
                    self.files.remove(lastrarnzf)        # first remove. NB: remove() does searches for lastrarnzf
                    self.files.insert(firstrarpos, lastrarnzf)    # ... and only then add after position firstrarpos
                except:
                    logging.debug('The lastrar swap did not go well')

    def reset_all_try_lists(self):
        for nzf in self.files:
            nzf.reset_all_try_lists()
        self.reset_try_list()

    @synchronized(NZO_LOCK)
    def postpone_pars(self, nzf, parset):
        """ Move all vol-par files matching 'parset' to the extrapars table """
        # Create new extrapars if it didn't already exist
        # For example if created when the first par2 file was missing
        if parset not in self.extrapars:
            self.extrapars[parset] = []

        # Set this one as the main one
        self.partable[parset] = nzf

        lparset = parset.lower()
        for xnzf in self.files[:]:
            name = xnzf.filename or platform_encode(xnzf.subject)
            # Move only when not current NZF and filename was extractable from subject
            if name:
                setname, vol, block = sabnzbd.par2file.analyse_par2(name)
                # Don't postpone header-only-files, to extract all possible md5of16k
                if setname and block and matcher(lparset, setname.lower()):
                    xnzf.set_par2(parset, vol, block)
                    # Don't postpone if all par2 are desired and should be kept or not repairing
                    if self.repair and not(cfg.enable_all_par() and not cfg.enable_par_cleanup()):
                        self.extrapars[parset].append(xnzf)
                        self.files.remove(xnzf)
                        # Already count these bytes as done
                        self.bytes_tried += xnzf.bytes_left

        # Sort the sets
        for setname in self.extrapars:
            self.extrapars[parset].sort(key=lambda x: x.blocks)

        # Also re-parse all filenames in case par2 came after first articles
        self.verify_all_filenames_and_resort()

    @synchronized(NZO_LOCK)
    def handle_par2(self, nzf, filepath):
        """ Check if file is a par2 and build up par2 collection """
        # Need to remove it from the other set it might be in
        self.remove_extrapar(nzf)

        # Reparse
        setname, vol, block = sabnzbd.par2file.analyse_par2(nzf.filename, filepath)
        nzf.set_par2(setname, vol, block)

        # Parse the file contents for hashes
        pack = sabnzbd.par2file.parse_par2_file(nzf, filepath)

        # If we couldn't parse it, we ignore it
        if pack:
            if pack not in self.md5packs.values():
                logging.debug('Got md5pack for set %s', nzf.setname)
                self.md5packs[setname] = pack
                # See if we need to postpone some pars
                self.postpone_pars(nzf, setname)
            else:
                # Need to add this to the set, first need setname
                for setname in self.md5packs:
                    if self.md5packs[setname] == pack:
                        break

                # Change the properties
                nzf.set_par2(setname, vol, block)
                logging.debug('Got additional md5pack for set %s', nzf.setname)

                # Make sure it exists, could be removed by newsunpack
                if setname not in self.extrapars:
                    self.extrapars[setname] = []
                self.extrapars[setname].append(nzf)

        elif self.repair:
            # For some reason this par2 file is broken but we still want repair
            self.promote_par2(nzf)

        # Is it an obfuscated file?
        if get_ext(nzf.filename) != '.par2':
            # Do cheap renaming so it gets better picked up by par2
            # Only basename has to be the same
            new_fname = get_unique_filename(os.path.join(self.downpath, '%s.par2' % setname))
            renamer(filepath, new_fname)
            self.renamed_file(get_filename(new_fname), nzf.filename)
            nzf.filename = get_filename(new_fname)

    @synchronized(NZO_LOCK)
    def promote_par2(self, nzf):
        """ In case of a broken par2 or missing par2, move another
            of the same set to the top (if we can find it)
        """
        setname, vol, block = sabnzbd.par2file.analyse_par2(nzf.filename)
        # Now we need to identify if we have more in this set
        if setname and self.repair:
            # Maybe it was the first one
            if setname not in self.extrapars:
                self.postpone_pars(nzf, setname)
            # Get the next one
            for new_nzf in self.extrapars[setname]:
                if not new_nzf.completed:
                    self.add_parfile(new_nzf)
                    # Add it to the top
                    self.files.remove(new_nzf)
                    self.files.insert(0, new_nzf)
                    break

    def get_extra_blocks(self, setname, needed_blocks):
        """ We want par2-files of all sets that are similar to this one
            So that we also can handle multi-sets with duplicate filenames
            Block-table has as keys the nr-blocks
            Returns number of added blocks in case they are available
        """
        logging.info('Need %s more blocks, checking blocks', needed_blocks)
        avail_blocks = 0
        block_table = {}
        for setname_search in self.extrapars:
            # Do it for our set, or highlight matching one
            # We might catch to many par2's, but that's okay
            if setname_search == setname or difflib.SequenceMatcher(None, setname, setname_search).ratio() > 0.85:
                for nzf in self.extrapars[setname_search]:
                    # Don't count extrapars that are completed already
                    if nzf.completed:
                        continue
                    blocks = int_conv(nzf.blocks)
                    if blocks not in block_table:
                        block_table[blocks] = []
                        # We assume same block-vol-naming for each set
                        avail_blocks += blocks
                    block_table[blocks].append(nzf)

        logging.info('%s blocks available', avail_blocks)

        # Enough?
        if avail_blocks >= needed_blocks:
            added_blocks = 0
            while added_blocks < needed_blocks:
                block_size = min(block_table.keys())
                for new_nzf in block_table[block_size]:
                    self.add_parfile(new_nzf)
                added_blocks += block_size
                block_table.pop(block_size)
            logging.info('Added %s blocks to %s', added_blocks, self.final_name)
            return added_blocks
        else:
            # Not enough
            return False

    @synchronized(NZO_LOCK)
    def remove_article(self, article, found):
        nzf = article.nzf

        # First or regular article?
        if article.lowest_partnum and self.first_articles and article in self.first_articles:
            self.first_articles.remove(article)

            # All first articles done?
            if not self.first_articles and self.md5of16k:
                self.verify_all_filenames_and_resort()

        # Remove from file-tracking
        file_done = nzf.remove_article(article, found)

        # Only on fully loaded files we can say if it's really done
        if not nzf.import_finished:
            file_done = False

        # File completed, remove and do checks
        if file_done:
            self.remove_nzf(nzf)
            if not self.reuse and cfg.fail_hopeless_jobs() and not self.check_quality(99)[0]:
                # set the nzo status to return "Queued"
                self.status = Status.QUEUED
                self.set_download_report()
                self.fail_msg = T('Aborted, cannot be completed') + ' - https://sabnzbd.org/not-complete'
                self.set_unpack_info('Download', self.fail_msg, unique=False)
                logging.debug('Abort job "%s", due to impossibility to complete it', self.final_name_pw_clean)
                return True, True

        if not found:
            # Add extra parfiles when there was a damaged article and not pre-checking
            if self.extrapars and not self.precheck:
                self.prospective_add(nzf)

            # Sometimes a few CRC errors are still fine, so we continue
            if self.bad_articles > MAX_BAD_ARTICLES:
                self.abort_direct_unpacker()

            # Increase missing bytes counter
            self.bytes_missing += article.bytes
        else:
            # Increase counter of actually finished bytes
            self.bytes_downloaded += article.bytes
        # All the bytes that were tried
        self.bytes_tried += article.bytes

        post_done = False
        if not self.files:
            post_done = True
            # set the nzo status to return "Queued"
            self.status = Status.QUEUED
            self.set_download_report()

        return (file_done, post_done)

    @synchronized(NZO_LOCK)
    def remove_saved_article(self, article):
        self.saved_articles.remove(article)

    def check_existing_files(self, wdir):
        """ Check if downloaded files already exits, for these set NZF to complete """
        fix_unix_encoding(wdir)

        # Get a list of already present files
        files = [os.path.basename(f) for f in globber_full(wdir) if os.path.isfile(f)]

        # Substitute renamed files
        renames = sabnzbd.load_data(RENAMES_FILE, self.workpath, remove=True)
        if renames:
            for name in renames:
                if name in files or renames[name] in files:
                    if name in files:
                        files.remove(name)
                    files.append(renames[name])
            self.renames = renames

        # Looking for the longest name first, minimizes the chance on a mismatch
        files.sort(lambda x, y: len(y) - len(x))

        # The NZFs should be tried shortest first, to improve the chance on a proper match
        nzfs = self.files[:]
        nzfs.sort(lambda x, y: len(x.subject) - len(y.subject))

        # Flag files from NZB that already exist as finished
        for filename in files[:]:
            for nzf in nzfs:
                subject = sanitize_filename(name_extractor(nzf.subject))
                if (nzf.filename == filename) or (subject == filename) or (filename in subject):
                    nzf.filename = filename
                    nzf.bytes_left = 0
                    self.remove_nzf(nzf)
                    nzfs.remove(nzf)
                    files.remove(filename)

                    # Set bytes correctly
                    self.bytes_tried += nzf.bytes
                    self.bytes_downloaded += nzf.bytes

                    # Process par2 files
                    filepath = os.path.join(wdir, filename)
                    if sabnzbd.par2file.is_parfile(filepath):
                        self.handle_par2(nzf, filepath)
                    break

        # Create an NZF for each remaining existing file
        try:
            for filename in files:
                # Create NZB's using basic information
                filepath = os.path.join(wdir, filename)
                if os.path.exists(filepath):
                    tup = os.stat(filepath)
                    tm = datetime.datetime.fromtimestamp(tup.st_mtime)
                    nzf = NzbFile(tm, filename, [], tup.st_size, self)
                    self.files.append(nzf)
                    self.files_table[nzf.nzf_id] = nzf
                    nzf.filename = filename
                    self.remove_nzf(nzf)

                    # Set bytes correctly
                    self.bytes += nzf.bytes
                    self.bytes_tried += nzf.bytes
                    self.bytes_downloaded += nzf.bytes

                    # Process par2 files
                    if sabnzbd.par2file.is_parfile(filepath):
                        self.handle_par2(nzf, filepath)
                    logging.info('Existing file %s added to job', filename)
        except:
            logging.debug('Bad NZB handling')
            logging.info("Traceback: ", exc_info=True)

    @property
    def pp(self):
        if self.repair is None:
            return None
        else:
            return sabnzbd.opts_to_pp(self.repair, self.unpack, self.delete)

    def set_pp(self, value):
        self.repair, self.unpack, self.delete = sabnzbd.pp_to_opts(value)
        logging.info('Set pp=%s for job %s', value, self.final_name)
        # Abort unpacking if not desired anymore
        if not self.unpack:
            self.abort_direct_unpacker()

    def set_priority(self, value):
        """ Check if this is a valid priority """
        # When unknown (0 is a known one), set to DEFAULT
        if value == '' or value is None:
            self.priority = DEFAULT_PRIORITY
            return

        # Convert input
        value = int_conv(value)
        if value in (REPAIR_PRIORITY, TOP_PRIORITY, HIGH_PRIORITY, NORMAL_PRIORITY, \
             LOW_PRIORITY, DEFAULT_PRIORITY, PAUSED_PRIORITY, DUP_PRIORITY, STOP_PRIORITY):
            self.priority = value
            return

        # Invalid value, set to normal priority
        self.priority = NORMAL_PRIORITY

    @property
    def final_name_labeled(self):
        prefix = ''
        if self.duplicate:
            prefix = T('DUPLICATE') + ' / '  # : Queue indicator for duplicate job
        if self.encrypted > 0 and self.status == 'Paused':
            prefix += T('ENCRYPTED') + ' / '  #: Queue indicator for encrypted job
        if self.oversized and self.status == 'Paused':
            prefix += T('TOO LARGE') + ' / '  # : Queue indicator for oversized job
        if self.incomplete and self.status == 'Paused':
            prefix += T('INCOMPLETE') + ' / '  # : Queue indicator for incomplete NZB
        if self.unwanted_ext and self.status == 'Paused':
            prefix += T('UNWANTED') + ' / '  # : Queue indicator for unwanted extensions
        if self.rating_filtered and self.status == 'Paused':
            prefix += T('FILTERED') + ' / '  # : Queue indicator for filtered
        if isinstance(self.url_wait, float):
            dif = int(self.url_wait - time.time() + 0.5)
            if dif > 0:
                prefix += T('WAIT %s sec') % dif + ' / '  # : Queue indicator for waiting URL fetch
        if (self.avg_stamp + float(cfg.propagation_delay() * 60)) > time.time() and self.priority != TOP_PRIORITY:
            wait_time = int((self.avg_stamp + float(cfg.propagation_delay() * 60) - time.time()) / 60 + 0.5)
            prefix += T('PROPAGATING %s min') % wait_time + ' / '  # : Queue indicator while waiting for propagation of post
        return '%s%s' % (prefix, self.final_name)

    @property
    def final_name_pw_clean(self):
        if self.password:
            return '%s / %s' % (self.final_name, self.password)
        else:
            return self.final_name

    def set_final_name_pw(self, name, password=None):
        if isinstance(name, basestring):
            if password is not None:
                name = platform_encode(name)
                self.password = platform_encode(password)
            else:
                name, password = scan_password(platform_encode(name))
                if password is not None:
                    self.password = password

            self.final_name = sanitize_foldername(name)
            self.save_to_disk()

    def pause(self):
        self.status = Status.PAUSED
        # Prevent loss of paused state when terminated
        if self.nzo_id and not self.is_gone():
            self.save_to_disk()

    def resume(self):
        self.status = Status.QUEUED
        if self.encrypted > 0:
            # If user resumes after encryption warning, no more auto-pauses
            self.encrypted = 2
        if self.rating_filtered:
            # If user resumes after filtered warning, no more auto-pauses
            self.rating_filtered = 2
        # If user resumes after warning, reset duplicate/oversized/incomplete/unwanted indicators
        self.duplicate = False
        self.oversized = False
        self.incomplete = False
        if self.unwanted_ext:
            # If user resumes after "unwanted" warning, no more auto-pauses
            self.unwanted_ext = 2

    @synchronized(NZO_LOCK)
    def add_parfile(self, parfile):
        """ Add parfile to the files to be downloaded
            Resets trylist just to be sure
            Adjust download-size accordingly
        """
        if not parfile.completed and parfile not in self.files and parfile not in self.finished_files:
            parfile.reset_all_try_lists()
            self.files.append(parfile)
            self.bytes_tried -= parfile.bytes_left

    @synchronized(NZO_LOCK)
    def remove_parset(self, setname):
        if setname in self.extrapars:
            self.extrapars.pop(setname)
        if setname in self.partable:
            self.partable.pop(setname)

    @synchronized(NZO_LOCK)
    def remove_extrapar(self, parfile):
        """ Remove par file from any/all sets """
        for _set in self.extrapars:
            if parfile in self.extrapars[_set]:
                self.extrapars[_set].remove(parfile)

    @synchronized(NZO_LOCK)
    def prospective_add(self, nzf):
        """ Add par2 files to compensate for missing articles
            This fails in case of multi-sets with identical setnames
        """
        # Make sure to also select a parset if it was in the original filename
        original_filename = self.renames.get(nzf.filename, '')

        # Get some blocks!
        if not nzf.is_par2:
            # We have to find the right par-set
            blocks_new = 0
            for parset in self.extrapars.keys():
                if (parset in nzf.filename or parset in original_filename) and self.extrapars[parset]:
                    for new_nzf in self.extrapars[parset]:
                        self.add_parfile(new_nzf)
                        blocks_new += int_conv(new_nzf.blocks)
                        # Enough now?
                        if blocks_new >= self.bad_articles:
                            logging.info('Prospectively added %s repair blocks to %s', blocks_new, self.final_name)
                            break
                    # Reset NZO TryList
                    self.reset_try_list()

    def add_to_direct_unpacker(self, nzf):
        """ Start or add to DirectUnpacker """
        if not self.direct_unpacker:
            sabnzbd.directunpacker.DirectUnpacker(self)
        self.direct_unpacker.add(nzf)

    def abort_direct_unpacker(self):
        """ Abort any running DirectUnpackers """
        if self.direct_unpacker:
            self.direct_unpacker.abort()

    def check_quality(self, req_ratio=0):
        """ Determine amount of articles present on servers
            and return (gross available, nett) bytes
        """
        # Few missing articles in RAR-only job might still work
        if self.bad_articles <= MAX_BAD_ARTICLES:
            logging.debug('Download Quality: bad-articles=%s', self.bad_articles)
            return True, 200

        # Do the full check
        need = 0L
        pars = 0L
        short = 0L
        anypars = False
        for nzf_id in self.files_table:
            nzf = self.files_table[nzf_id]
            if nzf.deleted:
                short += nzf.bytes_left
            if RE_QUICK_PAR2_CHECK.search(nzf.subject):
                pars += nzf.bytes
                anypars = True
            else:
                need += nzf.bytes
        have = need + pars - short
        ratio = float(have) / float(max(1, need))
        if anypars:
            enough = ratio * 100.0 >= (req_ratio or float(cfg.req_completion_rate()))
        else:
            enough = have >= need
        logging.debug('Download Quality: enough=%s, have=%s, need=%s, ratio=%s', enough, have, need, ratio)
        return enough, ratio

    @synchronized(NZO_LOCK)
    def set_download_report(self):
        if self.avg_bps_total and self.bytes_downloaded and self.avg_bps_freq:
            # get the deltatime since the download started
            avg_bps = self.avg_bps_total / self.avg_bps_freq
            timecompleted = datetime.timedelta(seconds=self.bytes_downloaded / (avg_bps * 1024))

            seconds = timecompleted.seconds
            # find the total time including days
            totaltime = (timecompleted.days / 86400) + seconds
            self.nzo_info['download_time'] = totaltime

            # format the total time the download took, in days, hours, and minutes, or seconds.
            complete_time = format_time_string(seconds, timecompleted.days)

            msg1 = T('Downloaded in %s at an average of %sB/s') % (complete_time, to_units(avg_bps * 1024))
            msg1 += u'<br/>' + T('Age') + ': ' + calc_age(self.avg_date, True)

            bad = self.nzo_info.get('bad_articles', 0)
            miss = self.nzo_info.get('missing_articles', 0)
            killed = self.nzo_info.get('killed_articles', 0)
            dups = self.nzo_info.get('duplicate_articles', 0)
            msg2 = msg3 = msg4 = msg5 = ''
            if bad:
                msg2 = (u'<br/>' + T('%s articles were malformed')) % bad
            if miss:
                msg3 = (u'<br/>' + T('%s articles were missing')) % miss
            if dups:
                msg4 = (u'<br/>' + T('%s articles had non-matching duplicates')) % dups
            if killed:
                msg5 = (u'<br/>' + T('%s articles were removed')) % killed
            msg = u''.join((msg1, msg2, msg3, msg4, msg5, ))
            self.set_unpack_info('Download', msg, unique=True)
            if self.url:
                self.set_unpack_info('Source', self.url, unique=True)

            if len(self.servercount) > 0:
                # Sort the servers first
                servers = config.get_servers()
                server_names = sorted(servers.keys(), key=lambda svr: '%d%02d%s' % (int(not servers[svr].enable()), servers[svr].priority(), servers[svr].displayname().lower()))
                msgs = ['%s=%sB' % (servers[server_name].displayname(), to_units(self.servercount[server_name])) for server_name in server_names if server_name in self.servercount]
                self.set_unpack_info('Servers', ', '.join(msgs), unique=True)

    @synchronized(NZO_LOCK)
    def increase_bad_articles_counter(self, type):
        """ Record information about bad articles """
        if type not in self.nzo_info:
            self.nzo_info[type] = 0
        self.nzo_info[type] += 1
        self.bad_articles += 1

    def get_article(self, server, servers):
        article = None
        nzf_remove_list = []

        # Did we go through all first-articles?
        if self.first_articles:
            for article_test in self.first_articles:
                article = article_test.get_article(server, servers)
                if article:
                    break

        # Move on to next ones
        if not article:
            for nzf in self.files:
                if nzf.deleted:
                    logging.debug('Skipping existing file %s', nzf.filename or nzf.subject)
                else:
                    # Don't try to get an article if server is in try_list of nzf
                    if not nzf.server_in_try_list(server):
                        if not nzf.import_finished:
                            # Only load NZF when it's a primary server
                            # or when it's a backup server without active primaries
                            if sabnzbd.highest_server(server):
                                nzf.finish_import()
                                # Still not finished? Something went wrong...
                                if not nzf.import_finished and not self.is_gone():
                                    logging.error(T('Error importing %s'), nzf)
                                    nzf_remove_list.append(nzf)
                                    nzf.nzo.status = Status.PAUSED
                                    continue
                            else:
                                continue

                        article = nzf.get_article(server, servers)
                        if article:
                            break

        # Remove all files for which admin could not be read
        for nzf in nzf_remove_list:
            nzf.deleted = True
            self.files.remove(nzf)

        # If cleanup emptied the active files list, end this job
        if nzf_remove_list and not self.files:
            sabnzbd.NzbQueue.do.end_job(self)

        if not article:
            # No articles for this server, block for next time
            self.add_to_try_list(server)
        return article

    @synchronized(NZO_LOCK)
    def move_top_bulk(self, nzf_ids):
        self.cleanup_nzf_ids(nzf_ids)
        if nzf_ids:
            target = range(len(nzf_ids))

            while 1:
                self.move_up_bulk(nzf_ids, cleanup=False)

                pos_nzf_table = self.build_pos_nzf_table(nzf_ids)

                keys = pos_nzf_table.keys()
                keys.sort()

                if target == keys:
                    break

    @synchronized(NZO_LOCK)
    def move_bottom_bulk(self, nzf_ids):
        self.cleanup_nzf_ids(nzf_ids)
        if nzf_ids:
            target = range(len(self.files) - len(nzf_ids), len(self.files))

            while 1:
                self.move_down_bulk(nzf_ids, cleanup=False)

                pos_nzf_table = self.build_pos_nzf_table(nzf_ids)

                keys = pos_nzf_table.keys()
                keys.sort()

                if target == keys:
                    break

    @synchronized(NZO_LOCK)
    def move_up_bulk(self, nzf_ids, cleanup=True):
        if cleanup:
            self.cleanup_nzf_ids(nzf_ids)
        if nzf_ids:
            pos_nzf_table = self.build_pos_nzf_table(nzf_ids)

            while pos_nzf_table:
                pos = min(pos_nzf_table)
                nzf = pos_nzf_table.pop(pos)

                if pos > 0:
                    tmp_nzf = self.files[pos - 1]
                    if tmp_nzf.nzf_id not in nzf_ids:
                        self.files[pos - 1] = nzf
                        self.files[pos] = tmp_nzf

    @synchronized(NZO_LOCK)
    def move_down_bulk(self, nzf_ids, cleanup=True):
        if cleanup:
            self.cleanup_nzf_ids(nzf_ids)
        if nzf_ids:
            pos_nzf_table = self.build_pos_nzf_table(nzf_ids)

            while pos_nzf_table:
                pos = max(pos_nzf_table)
                nzf = pos_nzf_table.pop(pos)

                if pos < len(self.files) - 1:
                    tmp_nzf = self.files[pos + 1]
                    if tmp_nzf.nzf_id not in nzf_ids:
                        self.files[pos + 1] = nzf
                        self.files[pos] = tmp_nzf

    def verify_nzf_filename(self, nzf, yenc_filename=None):
        """ Get filename from par2-info or from yenc """
        # Already done?
        if nzf.filename_checked:
            return

        # If we have the md5, use it to rename
        if nzf.md5of16k and self.md5of16k:
            # Don't check again, even if no match
            nzf.filename_checked = True
            # Find the match and rename
            if nzf.md5of16k in self.md5of16k:
                new_filename = platform_encode(self.md5of16k[nzf.md5of16k])
                # Was it even new?
                if new_filename != nzf.filename:
                    logging.info('Detected filename based on par2: %s -> %s', nzf.filename, new_filename)
                    self.renamed_file(new_filename, nzf.filename)
                    nzf.filename = new_filename
                return

        # Fallback to yenc/nzb name (also when there is no partnum=1)
        # We also keep the NZB name in case it ends with ".par2" (usually correct)
        if yenc_filename and yenc_filename != nzf.filename and not is_obfuscated_filename(yenc_filename) and not nzf.filename.endswith('.par2'):
            logging.info('Detected filename from yenc: %s -> %s', nzf.filename, yenc_filename)
            self.renamed_file(yenc_filename, nzf.filename)
            nzf.filename = yenc_filename

    def verify_all_filenames_and_resort(self):
        """ Verify all filenames based on par2 info and then re-sort files """
        logging.info('Checking all filenames for %s', self.final_name)
        for nzf_verify in self.files:
            self.verify_nzf_filename(nzf_verify)
        logging.info('Re-sorting %s after getting filename information', self.final_name)
        self.sort_nzfs()

    @synchronized(NZO_LOCK)
    def renamed_file(self, name_set, old_name=None):
        """ Save renames at various stages (Download/PP)
            to be used on Retry. Accepts strings and dicts.
        """
        if not old_name:
            # Add to dict
            self.renames.update(name_set)
        else:
            self.renames[name_set] = old_name

    # Determine if rating information (including site identifier so rating can be updated)
    # is present in metadata and if so store it
    @synchronized(NZO_LOCK)
    def update_rating(self):
        if cfg.rating_enable():
            try:
                def _get_first_meta(type):
                    values = self.nzo_info.get('x-oznzb-rating-' + type, None) or self.nzo_info.get('x-rating-' + type, None)
                    return values[0] if values and isinstance(values, list) else values
                rating_types = ['url', 'host', 'video', 'videocnt', 'audio', 'audiocnt', 'voteup',
                                'votedown', 'spam', 'confirmed-spam', 'passworded', 'confirmed-passworded']
                fields = {}
                for k in rating_types:
                    fields[k] = _get_first_meta(k)
                Rating.do.add_rating(_get_first_meta('id'), self.nzo_id, fields)
            except:
                pass

    ## end nzo.Mutators #######################################################
    ###########################################################################

    @property
    def workpath(self):
        """ Return the full path for my job-admin folder """
        return long_path(get_admin_path(self.work_name, self.futuretype))

    @property
    def downpath(self):
        """ Return the full path for my download folder """
        if self.futuretype:
            return ''
        else:
            return long_path(os.path.join(cfg.download_dir.get_path(), self.work_name))

    @property
    def group(self):
        if self.groups:
            return self.groups[0]
        else:
            return None

    @property
    def remaining(self):
        """ Return remaining bytes """
        return self.bytes - self.bytes_tried

    @synchronized(NZO_LOCK)
    def purge_data(self, keep_basic=False, del_files=False):
        """ Remove all admin info, 'keep_basic' preserves attribs and nzb """
        logging.info('[%s] Purging data for job %s (keep_basic=%s, del_files=%s)', caller_name(), self.final_name, keep_basic, del_files)

        wpath = self.workpath
        for nzf in self.files:
            sabnzbd.remove_data(nzf.nzf_id, wpath)

        for _set in self.extrapars:
            for nzf in self.extrapars[_set]:
                sabnzbd.remove_data(nzf.nzf_id, wpath)

        for nzf in self.finished_files:
            sabnzbd.remove_data(nzf.nzf_id, wpath)

        if not self.futuretype:
            if keep_basic:
                remove_all(wpath, 'SABnzbd_nz?_*', keep_folder=True)
                remove_all(wpath, 'SABnzbd_article_*', keep_folder=True)
                # We save the renames file
                sabnzbd.save_data(self.renames, RENAMES_FILE, self.workpath, silent=True)
            else:
                remove_all(wpath, recursive=True)
            if del_files:
                remove_all(self.downpath, recursive=True)
            else:
                try:
                    remove_dir(self.downpath)
                except:
                    logging.debug('Folder not removed: %s', self.downpath)
                    pass

    def gather_info(self, full=False):
        queued_files = []
        if full:
            # extrapars can change during iteration
            with NZO_LOCK:
                for _set in self.extrapars:
                    for nzf in self.extrapars[_set]:
                        # Don't show files twice
                        if not nzf.completed and nzf not in self.files:
                            queued_files.append(nzf)

        return PNFO(self.repair, self.unpack, self.delete, self.script, self.nzo_id,
                self.final_name_labeled, self.password, {}, '', self.cat, self.url, self.remaining,
                self.bytes, self.avg_stamp, self.avg_date, self.finished_files if full else [],
                self.files if full else [], queued_files, self.status, self.priority,
                self.bytes_missing, self.direct_unpacker.get_formatted_stats() if self.direct_unpacker else 0)

    def get_nzf_by_id(self, nzf_id):
        if nzf_id in self.files_table:
            return self.files_table[nzf_id]

    @synchronized(NZO_LOCK)
    def set_unpack_info(self, key, msg, unique=False):
        """ Builds a dictionary containing the stage name (key) and a message
            If unique is present, it will only have a single line message
        """
        # Unique messages allow only one line per stage(key)
        if not unique:
            if key not in self.unpack_info:
                self.unpack_info[key] = []
            self.unpack_info[key].append(msg)
        else:
            self.unpack_info[key] = [msg]

    def set_action_line(self, action=None, msg=None):
        if action and msg:
            self.action_line = '%s: %s' % (action, msg)
        else:
            self.action_line = ''
        # Make sure it's updated in the interface
        sabnzbd.history_updated()

    @property
    def repair_opts(self):
        return self.repair, self.unpack, self.delete

    @synchronized(NZO_LOCK)
    def save_to_disk(self):
        """ Save job's admin to disk """
        self.save_attribs()
        if self.nzo_id and not self.is_gone():
            sabnzbd.save_data(self, self.nzo_id, self.workpath)

    def save_attribs(self):
        set_attrib_file(self.workpath, (self.cat, self.pp, self.script, self.priority, self.final_name, self.password, self.url))

    @synchronized(NZO_LOCK)
    def build_pos_nzf_table(self, nzf_ids):
        pos_nzf_table = {}
        for nzf_id in nzf_ids:
            if nzf_id in self.files_table:
                nzf = self.files_table[nzf_id]
                pos = self.files.index(nzf)
                pos_nzf_table[pos] = nzf

        return pos_nzf_table

    @synchronized(NZO_LOCK)
    def cleanup_nzf_ids(self, nzf_ids):
        for nzf_id in nzf_ids[:]:
            if nzf_id in self.files_table:
                if self.files_table[nzf_id] not in self.files:
                    nzf_ids.remove(nzf_id)
            else:
                nzf_ids.remove(nzf_id)

    def has_duplicates(self):
        """ Return (res, series)
            where "res" is True when this is a duplicate
            where "series" is True when this is an episode
        """

        no_dupes = cfg.no_dupes()
        no_series_dupes = cfg.no_series_dupes()
        series_propercheck = cfg.series_propercheck()

        # abort logic if dupe check is off for both nzb+series
        if not no_dupes and not no_series_dupes:
            return False, False

        series = False
        res = False
        history_db = HistoryDB()

        # dupe check off nzb contents
        if no_dupes:
            res = history_db.have_md5sum(self.md5sum)
            logging.debug('Dupe checking NZB in history: filename=%s, md5sum=%s, result=%s', self.filename, self.md5sum, res)
            if not res and cfg.backup_for_duplicates():
                res = sabnzbd.backup_exists(self.filename)
                logging.debug('Dupe checking NZB against backup: filename=%s, result=%s', self.filename, res)
        # dupe check off nzb filename
        if not res and no_series_dupes:
            series, season, episode, misc = sabnzbd.newsunpack.analyse_show(self.final_name)
            if RE_PROPER.match(misc) and series_propercheck:
                logging.debug('Dupe checking series+season+ep in history aborted due to PROPER/REAL/REPACK found')
            else:
                res = history_db.have_episode(series, season, episode)
                logging.debug('Dupe checking series+season+ep in history: series=%s, season=%s, episode=%s, result=%s', series, season, episode, res)

        history_db.close()
        return res, series

    def is_gone(self):
        """ Is this job still going somehow? """
        return self.status in (Status.COMPLETED, Status.DELETED, Status.FAILED)

    def __getstate__(self):
        """ Save to pickle file, selecting attributes """
        dict_ = {}
        for item in NzbObjectSaver:
            dict_[item] = getattr(self, item)
        dict_['try_list'] = TryList.__getstate__(self)
        return dict_

    def __setstate__(self, dict_):
        """ Load from pickle file, selecting attributes """
        for item in NzbObjectSaver:
            try:
                setattr(self, item, dict_[item])
            except KeyError:
                # Handle new attributes
                setattr(self, item, None)
        TryList.__setstate__(self, dict_.get('try_list', []))

        # Set non-transferable values
        self.pp_active = False
        self.avg_stamp = time.mktime(self.avg_date.timetuple())
        self.url_wait = None
        self.url_tries = 0
        self.to_be_removed = False
        self.direct_unpacker = None
        if self.meta is None:
            self.meta = {}
        if self.servercount is None:
            self.servercount = {}
        if self.md5of16k is None:
            self.md5of16k = {}
        if self.renames is None:
            self.renames = {}
        if self.bad_articles is None:
            self.bad_articles = 0
        if self.bytes_missing is None:
            self.bytes_missing = 0
        if self.bytes_tried is None:
            # Fill with old info
            self.bytes_tried = 0
            for nzf in self.finished_files:
                # Emulate behavior of 1.0.x
                self.bytes_tried += nzf.bytes
            for nzf in self.files:
                self.bytes_tried += nzf.bytes - nzf.bytes_left

    def __repr__(self):
        return "<NzbObject: filename=%s>" % self.filename


def nzf_get_filename(nzf):
    """ Return filename, if the filename not set, try the
        the full subject line instead. Can produce non-ideal results
    """
    name = nzf.filename
    if not name:
        name = nzf.subject
    if not name:
        name = ''
    return name.lower()


def nzf_cmp_date(nzf1, nzf2):
    """ Compare files based on date, but give vol-par files preference.
        Wrapper needed, because `cmp` function doesn't handle extra parms.
    """
    return nzf_cmp_name(nzf1, nzf2, name=False)


def nzf_cmp_name(nzf1, nzf2, name=True):
    # The comparison will sort .par2 files to the top of the queue followed by .rar files,
    # they will then be sorted by name.
    name1 = nzf_get_filename(nzf1)
    name2 = nzf_get_filename(nzf2)

    # Determine vol-pars
    is_par1 = '.vol' in name1 and '.par2' in name1
    is_par2 = '.vol' in name2 and '.par2' in name2

    # mini-par2 in front
    if not is_par1 and name1.endswith('.par2'):
        return -1
    if not is_par2 and name2.endswith('.par2'):
        return 1

    # vol-pars go to the back
    if is_par1 and not is_par2:
        return 1
    if is_par2 and not is_par1:
        return -1

    if name:
        # Prioritize .rar files above any other type of file (other than vol-par)
        m1 = RE_RAR.search(name1)
        m2 = RE_RAR.search(name2)
        if m1 and not (is_par2 or m2):
            return -1
        elif m2 and not (is_par1 or m1):
            return 1
        # Force .rar to come before 'r00'
        if m1 and m1.group(1) == '.rar':
            name1 = name1.replace('.rar', '.r//')
        if m2 and m2.group(1) == '.rar':
            name2 = name2.replace('.rar', '.r//')
        return cmp(name1, name2)
    else:
        # Do date comparison
        return cmp(nzf1.date, nzf2.date)


def create_work_name(name):
    """ Remove ".nzb" and ".par(2)" """
    strip_ext = ['.nzb', '.par', '.par2']
    name = name.strip()
    if name.find('://') < 0:
        name_base, ext = os.path.splitext(name)
        # In case it was one of these, there might be more
        while ext.lower() in strip_ext:
            name = name_base
            name_base, ext = os.path.splitext(name)
        return name.strip()
    else:
        return name.strip()


def scan_password(name):
    """ Get password (if any) from the title """
    if 'http://' in name or 'https://' in name:
        return name, None

    braces = name.find('{{')
    if braces < 0:
        braces = len(name)
    slash = name.find('/')

    # Look for name/password, but make sure that '/' comes before any {{
    if slash >= 0 and slash < braces and 'password=' not in name:
        # Is it maybe in 'name / password' notation?
        if slash == name.find(' / ') + 1:
            # Remove the extra space after name and before password
            return name[:slash - 1].strip('. '), name[slash + 2:]
        return name[:slash].strip('. '), name[slash + 1:]

    # Look for "name password=password"
    pw = name.find('password=')
    if pw >= 0:
        return name[:pw].strip('. '), name[pw + 9:]

    # Look for name{{password}}
    if braces < len(name) and name.endswith('}}'):
        return name[:braces].strip('. '), name[braces + 2:len(name) - 2]

    # Look again for name/password
    if slash >= 0:
        return name[:slash].strip('. '), name[slash + 1:]

    # No password found
    return name, None


def get_attrib_file(path, size):
    """ Read job's attributes from file """
    attribs = []
    path = os.path.join(path, ATTRIB_FILE)
    try:
        f = open(path, 'r')
    except:
        return [None for unused in xrange(size)]

    for unused in xrange(size):
        line = f.readline().strip('\r\n ')
        if line:
            if line.lower() == 'none':
                line = None
            try:
                line = int(line)
            except:
                pass
            attribs.append(line)
        else:
            attribs.append(None)
    f.close()
    return attribs


def set_attrib_file(path, attribs):
    """ Write job's attributes to file """
    path = os.path.join(path, ATTRIB_FILE)
    try:
        f = open(path, 'w')
    except:
        return

    for item in attribs:
        f.write('%s\n' % item)
    f.close()


def name_extractor(subject):
    """ Try to extract a file name from a subject line, return `subject` if in doubt """
    result = subject
    for name in re.findall(SUBJECT_FN_MATCHER, subject):
        name = name.strip(' "')
        if name and RE_NORMAL_NAME.search(name):
            result = name
    return platform_encode(result)


def matcher(pattern, txt):
    """ Return True if `pattern` is sufficiently equal to `txt` """
    if txt.endswith(pattern):
        txt = txt[:txt.rfind(pattern)].strip()
        return (not txt) or txt.endswith('"')
    else:
        return False
