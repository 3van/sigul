# Copyright (C) 2008, 2009 Red Hat, Inc.  All rights reserved.
#
# This copyrighted material is made available to anyone wishing to use, modify,
# copy, or redistribute it subject to the terms and conditions of the GNU
# General Public License v.2.  This program is distributed in the hope that it
# will be useful, but WITHOUT ANY WARRANTY expressed or implied, including the
# implied warranties of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.  You should have
# received a copy of the GNU General Public License along with this program; if
# not, write to the Free Software Foundation, Inc., 51 Franklin Street, Fifth
# Floor, Boston, MA 02110-1301, USA.  Any Red Hat trademarks that are
# incorporated in the source code or documentation are not subject to the GNU
# General Public License and may only be used or replicated with the express
# permission of Red Hat, Inc.
#
# Red Hat Author: Miloslav Trmac <mitr@redhat.com>

import ConfigParser
import Queue
import datetime
import errno
import getpass
import grp
import logging
import optparse
import os
import pwd
import re
import socket
import stat
import struct
import sys
import tempfile
import threading
import xmlrpclib

import nss.error
import nss.nss
import nss.ssl

import settings

 # Configuration handling

class ConfigurationError(Exception):
    '''Error reading utility configuration.'''
    pass

class Configuration(object):
    '''A configuration of one of the utilities.'''

    default_config_file = None

    def __init__(self, config_file, **unused_kwargs):
        '''Read config_file, overriding data in self.default_config_file.

        Raise ConfigurationError.

        '''
        defaults = {}
        self._add_defaults(defaults)
        parser = ConfigParser.RawConfigParser(defaults)
        sections = set()
        self._add_sections(sections)
        for s in sections:
            parser.add_section(s)
        config_paths = (os.path.join(settings.config_dir,
                                     self.default_config_file),
                        os.path.expanduser(config_file))
        files = parser.read(config_paths)
        if len(files) == 0:
            raise ConfigurationError('No configuration file found (tried %s)' %
                                     ', '.join("'%s'" % p
                                               for p in config_paths))
        try:
            self._read_configuration(parser)
        # ValueError is not handled by parser.getint()
        except (ConfigParser.Error, ValueError), e:
            raise ConfigurationError('Error reading configuration: %s' % str(e))

    def _add_defaults(self, defaults):
        '''Add more default values to defaults.'''
        pass

    def _add_sections(self, sections):
        '''Add more section names to sections.'''
        pass

    def _read_configuration(self, parser):
        '''Set attributes depending on parser.

        Raise ConfigParser.Error.

        '''
        pass

def optparse_add_batch_option(parser):
    '''Add --batch option to parser.'''
    parser.add_option('--batch', action='store_true',
                      help='Communicate in batch-friendly mode (omit prompts, '
                      'expect NUL-terminated input)')
    parser.set_defaults(batch=False)

def optparse_add_config_file_option(parser, default):
    '''Add a --config-file option to parser with the specified default.'''
    parser.add_option('-c', '--config-file', help='Configuration file path')
    parser.set_defaults(config_file=default)

def optparse_add_verbosity_option(parser):
    '''Add --verbose option to parser.'''
    parser.add_option('-v', '--verbose', action='count',
                      help='More verbose output (twice for debugging messages)')

def logging_level_from_options(options):
    '''Return a logging verbosity level depending on options.verbose'''
    if options.verbose <= 0:
        return logging.WARNING
    elif options.verbose == 1:
        return logging.INFO
    else: # options.verbose >= 2
        return logging.DEBUG

def create_basic_parser(description, default_config_file):
    '''Create a basic optparse parser for a bridge/server component.'''
    parser = optparse.OptionParser(usage='%prog [options]',
                                   version='%%prog %s' % (settings.version),
                                   description=description)
    optparse_add_config_file_option(parser, default_config_file)
    optparse_add_verbosity_option(parser)
    return parser

def optparse_parse_options_only(parser):
    '''Parse input using parser, expecting no arguments.

    Return resulting options.  Exit on unexpected arguments.

    '''
    (options, args) = parser.parse_args()
    if len(args) != 0:
        parser.error('unexpected argument')
    return options

def get_daemon_options(description, default_config_file):
    '''Handle command-line options for a daemon.

    Return the options object.  Use daemon options if daemon_options.  Use
    --batch if batch.  Exit on error.

    '''
    parser = create_basic_parser(description, default_config_file)
    parser.add_option('--internal-log-dir', help=optparse.SUPPRESS_HELP,
                      dest='log_dir')
    parser.add_option('--internal-pid-dir', help=optparse.SUPPRESS_HELP,
                      dest='pid_dir')
    parser.add_option('-d', '--daemonize', action='store_true',
                      help='Run in the background')
    parser.set_defaults(log_dir=settings.log_dir, pid_dir=settings.pid_dir,
                        daemonize=False)
    return optparse_parse_options_only(parser)

 # Koji utilities

class KojiConfiguration(Configuration):

    def _add_defaults(self, defaults):
        super(KojiConfiguration, self)._add_defaults(defaults)
        defaults.update({'koji-config': '~/.koji/config',
                         'koji-instances': ''})

    def _add_sections(self, sections):
        super(KojiConfiguration, self)._add_sections(sections)
        sections.add('koji')

    def _read_configuration(self, parser):
        super(KojiConfiguration, self)._read_configuration(parser)
        self.koji_config = parser.get('koji', 'koji-config')
        self.koji_instances = {}
        for v in parser.get('koji', 'koji-instances').split():
            self.koji_instances[v] = parser.get('koji', 'koji-config-' + v)

class KojiError(Exception):
    pass

_u8_format = '!B'
def u8_pack(v):
    return struct.pack(_u8_format, v)

def u8_unpack(data):
    return struct.unpack(_u8_format, data)[0]

u8_size = struct.calcsize(_u8_format)

_u32_format = '!I'
def u32_pack(v):
    return struct.pack(_u32_format, v)

def u32_unpack(data):
    return struct.unpack(_u32_format, data)[0]

u32_size = struct.calcsize(_u32_format)

def koji_read_config(global_config, instance):
    '''Read koji's configuration and verify it, using global_config.

    Use the selected Koji instance if it is not None.  Return a dictionary of
    options.

    '''
    if instance is not None:
        try:
            config_path = global_config.koji_instances[instance]
        except KeyError:
            raise KojiError('Koji configuration instance %s not defined' %
                            instance)
    else:
        config_path = global_config.koji_config
    parser = ConfigParser.ConfigParser()
    parser.read(('/etc/koji.conf', os.path.expanduser(config_path)))
    config = dict(parser.items('koji'))
    for opt in ('server', 'cert', 'ca', 'serverca', 'topurl'):
        if opt not in config:
            raise KojiError('Missing koji configuration option %s' % opt)
    for opt in ('cert', 'ca', 'serverca'):
        config[opt] = os.path.expanduser(config[opt])
    return config

def koji_connect(koji_config, authenticate, proxyuser=None):
    '''Return an authenticated koji session.

    Authenticate as user, on behalf of proxyuser if not None.

    '''
    # Don't import koji at the top of the file!  The rpm Python module calls
    # NSS_NoDB_Init() during its initialization, which breaks our attempts to
    # initialize nss with our certificate database.
    import koji

    session = koji.ClientSession(koji_config['server'])
    if authenticate:
        session.ssl_login(koji_config['cert'], koji_config['ca'],
                          koji_config['serverca'], proxyuser=proxyuser)
    try:
        version = session.getAPIVersion()
    except xmlrpclib.ProtocolError:
        raise KojiError('Cannot connect to Koji')
    if version != koji.API_VERSION:
        raise KojiError('Koji API version mismatch (server %d, client %d)' %
                        (version, koji.API_VERSION))
    return session

def koji_disconnect(session):
    try:
        session.logout()
    except:
        pass

 # Crypto utilities

class NSSConfiguration(Configuration):

    def _add_defaults(self, defaults):
        super(NSSConfiguration, self)._add_defaults(defaults)
        defaults.update({'nss-dir': '~/.sigul', 'nss-password': None})

    def _add_sections(self, sections):
        super(NSSConfiguration, self)._add_sections(sections)
        sections.add('nss')

    def _read_configuration(self, parser):
        super(NSSConfiguration, self)._read_configuration(parser)
        self.nss_dir = os.path.expanduser(parser.get('nss', 'nss-dir'))
        if not os.path.isdir(self.nss_dir):
            raise ConfigurationError('[nss] nss-dir \'%s\' is not a directory' %
                                     self.nss_dir)
        self.nss_password = parser.get('nss', 'nss-password')
        if self.nss_password is None:
            self.nss_password = getpass.getpass('NSS database password: ')

def nss_client_auth_callback_single(unused_ca_names, cert):
    '''Provide the specified certificate.'''
    return (cert, nss.nss.find_key_by_any_cert(cert))

class NSSInitError(Exception):
    '''Error in nss_init.'''
    pass

def nss_init(config):
    '''Initialize NSS.

    Raise NSSInitError.

    '''
    def _password_callback(unused_slot, retry):
        if not retry:
            return config.nss_password
        return None

    nss.nss.set_password_callback(_password_callback)
    try:
        # The initialization name has changed in python-nss 0.4.
        try:
            nss.nss.nss_init(config.nss_dir)
        except AttributeError:
            nss.ssl.nssinit(config.nss_dir)
    except nss.error.NSPRError, e:
        if e.errno == nss.error.SEC_ERROR_BAD_DATABASE:
            raise NSSInitError('\'%s\' does not contain a valid NSS database' %
                               (config.nss_dir,))
        raise
    nss.ssl.set_domestic_policy()
    nss.ssl.config_server_session_id_cache()

_derivation_counter_1 = u32_pack(1)
def derived_key(nss_base_key, instance):
    '''Return a NSS HMAC key derived from nss_base_key and instance number.'''
    # This is a degenerate case of NIST SP 800-56A section 5.8, with
    # AlgorithmInfo empty (SHA-512 HMAC implied), PartyUInfo empty, PartyVInfo
    # empty, instance number as SuppPubInfo
    # reps = 1
    # Hash1 == Hash == DerivedKeyingMaterial
    digest = nss.nss.create_digest_context(nss.nss.SEC_OID_SHA512)
    digest.digest_op(_derivation_counter_1)
    digest.digest_key(nss_base_key)
    digest.digest_op(u32_pack(instance))
    raw = digest.digest_final()
    mech = nss.nss.CKM_SHA512_HMAC
    return nss.nss.import_sym_key(nss.nss.get_best_slot(mech), mech,
                                  nss.nss.PK11_OriginDerive, nss.nss.CKA_SIGN,
                                  nss.nss.SecItem(raw))

class _DigestsReader(object):
    '''A wrapper with a .read method, computing digests of the data.'''

    def __init__(self, read_fn, *digests):
        self._read_fn = read_fn
        self.__digests = digests
        for d in self.__digests:
            d.digest_begin()

    def read(self, size):
        '''Return up to size bytes of data, adding it to digests as well.'''
        data = self._read_fn(size)
        for d in self.__digests:
            d.digest_op(data)
        return data

class SHA512Reader(_DigestsReader):
    '''A wrapper with a .read method, computing a SHA-512 hash of the data.'''

    def __init__(self, read_fn):
        self.__digest = nss.nss.create_digest_context(nss.nss.SEC_OID_SHA512)
        super(SHA512Reader, self).__init__(read_fn, self.__digest)

    def sha512(self):
        '''Return a SHA-512 hash of the data sent so far.'''
        digest = self.__digest.digest_final()
        self.__digest = None # Just to be sure nothing unexpected happens
        return digest

class SHA512HMACReader(_DigestsReader):
    '''A wrapper with a .read method, computing a SHA-512 HMAC of the data.'''

    def __init__(self, read_fn, nss_key):
        self.__hmac = nss.nss.create_context_by_sym_key \
            (nss.nss.CKM_SHA512_HMAC, nss.nss.CKA_SIGN, nss_key, None)
        super(SHA512HMACReader, self).__init__(read_fn, self.__hmac)

    def verify_64B_hmac_authenticator(self):
        '''Compute and read HMAC of the data sent so far.

        Return True if the computed HMAC matches the input value.

        '''
        digest = self.__hmac.digest_final()
        self.__hmac = None # Just to be sure nothing unexpected happens
        assert len(digest) == 64
        auth = self._read_fn(64)
        return auth == digest

class SHA512HashAndHMACReader(_DigestsReader):
    '''A wrapper with a .read method, computing a SHA-512 hash and HMAC.'''

    def __init__(self, read_fn, nss_key):
        self.__digest = nss.nss.create_digest_context(nss.nss.SEC_OID_SHA512)
        self.__hmac = nss.nss.create_context_by_sym_key \
            (nss.nss.CKM_SHA512_HMAC, nss.nss.CKA_SIGN, nss_key, None)
        super(SHA512HashAndHMACReader, self).__init__(read_fn, self.__digest,
                                                      self.__hmac)

    def hmac(self):
        '''Return HMAC of the data sent so far.'''
        auth = self.__hmac.digest_final()
        self.__hmac = None # Just to be sure nothing unexpected happens
        assert len(auth) == 64
        return auth

    def sha512(self):
        '''Return a SHA-512 hash of the data sent so far.'''
        digest = self.__digest.digest_final()
        self.__digest = None # Just to be sure nothing unexpected happens
        return digest

class _DigestsWriter(object):
    '''A wrapper with a .write method, computing digests of the data.'''

    def __init__(self, write_fn, *digests):
        self._write_fn = write_fn
        self.__digests = digests
        for d in self.__digests:
            d.digest_begin()

    def write(self, data):
        '''Write data, adding it to digests as well.'''
        self._write_fn(data)
        for d in self.__digests:
            d.digest_op(data)

class SHA512Writer(_DigestsWriter):
    '''A wrapper with a .write method, computing a SHA-512 hash of the data.'''

    def __init__(self, write_fn):
        self.__digest = nss.nss.create_digest_context(nss.nss.SEC_OID_SHA512)
        super(SHA512Writer, self).__init__(write_fn, self.__digest)

    def sha512(self):
        '''Return a SHA-512 hash of the data sent so far.'''
        digest = self.__digest.digest_final()
        self.__digest = None # Just to be sure nothing unexpected happens
        return digest

class SHA512HMACWriter(_DigestsWriter):
    '''A wrapper with a .write method, computing a SHA-512 HMAC of the data.'''

    def __init__(self, write_fn, nss_key):
        self.__hmac = nss.nss.create_context_by_sym_key \
            (nss.nss.CKM_SHA512_HMAC, nss.nss.CKA_SIGN, nss_key, None)
        super(SHA512HMACWriter, self).__init__(write_fn, self.__hmac)

    def write_64B_hmac(self):
        '''Compute and write HMAC of the data sent so far.'''
        auth = self.__hmac.digest_final()
        self.__hmac = None # Just to be sure nothing unexpected happens
        assert len(auth) == 64
        self._write_fn(auth)

 # Protocol utilities

protocol_version = 0

class InvalidFieldsError(Exception):
    pass

def read_fields(read_fn):
    '''Read field mapping using read_fn(size).

    Return field mapping.  Raise InvalidFieldsError on error.  read_fn(size)
    must return exactly size bytes.

    '''
    buf = read_fn(u8_size)
    num_fields = u8_unpack(buf)
    if num_fields > 255:
        raise InvalidFieldsError('Too many fields')
    fields = {}
    for _ in xrange(num_fields):
        buf = read_fn(u8_size)
        size = u8_unpack(buf)
        if size == 0 or size > 255:
            raise InvalidFieldsError('Invalid field key length')
        key = read_fn(size)
        if not string_is_safe(key):
            raise InvalidFieldsError('Unprintable key value')
        buf = read_fn(u8_size)
        size = u8_unpack(buf)
        if size > 255:
            raise InvalidFieldsError('Invalid field value length')
        value = read_fn(size)
        fields[key] = value
    return fields

def format_fields(fields):
    '''Return fields formated using the protocol.

    Raise ValueError on invalid values.

    '''
    if len(fields) > 255:
        raise ValueError('Too many fields')
    data = u8_pack(len(fields))
    for (key, value) in fields.iteritems():
        if len(key) > 255:
            raise ValueError('Key name %s too long' % key)
        data += u8_pack(len(key))
        data += key
        if isinstance(value, int):
            value = u32_pack(value)
        elif isinstance(value, bool):
            if value:
                value = u32_pack(1)
            else:
                value = u32_pack(0)
        elif not isinstance(value, str):
            raise ValueError('Unknown value type of %s' % repr(value))
        if len(value) > 255:
            raise ValueError('Value %s too long' % repr(value))
        data += u8_pack(len(value))
        data += value
    return data

def readable_fields(fields):
    '''Return a string representing fields.'''
    keys = sorted(fields.iterkeys())
    return ', '.join(('%s = %s' % (repr(k), repr(fields[k])) for k in keys))

def string_is_safe(s):
    '''Return True if s an allowed readable string.'''
    # Motivated by 100% readable logs
    for c in s:
        if ord(c) < 0x20 or ord(c) > 0x7F:
            return False
    return True

_date_re = re.compile('^\d\d\d\d-\d\d-\d\d$')
def yyyy_mm_dd_is_valid(s):
    '''Return True if s is a valid yyyy-mm-dd date.'''
    if _date_re.match(s) is None:
        return False
    try:
        datetime.date(int(s[:4]), int(s[5:7]), int(s[8:]))
    except ValueError:
        return False
    return True

 # Threading utilities

class WorkerQueueOrphanedError(Exception):
    '''Putting an item into a WorkerQueue failed because it is orphaned.'''
    pass

class WorkerQueue(object):
    '''A synchronized queue similar to Queue.Queue, except that it can be marked
    as orphaned; if so, attempts to put more items into the queue will never
    block, but may raise WorkerQueueOrphanedError.

    (Note that the writer is not guaranteed to get WorkerQueueOrphanedError for
    all unprocessed items because the queue may become orphaned after putting
    an item into the queue.)
    '''

    def __init__(self, maxsize=0):
        '''Create a FIFO queue.  See Queue.Queue.'''
        self.__queue = Queue.Queue(maxsize)
        self.__orphaned = threading.Event() # Used as a thread-safe boolean.

    def get(self):
        '''Remove and return an item from the queue. See Queue.get().'''
        assert not self.__orphaned.is_set()
        return self.__queue.get()

    def put(self, item):
        '''Put item into the queue.  See Queue.put().

        Do not block on an orphaned queue; possibly raise
        WorkerQueueOrphanedError.
        '''
        timeout = 0.001
        while True:
            if self.__orphaned.is_set():
                raise WorkerQueueOrphanedError('Work queue orphaned by consumer')
            try:
                self.__queue.put(item, True, timeout)
            except Queue.Full:
                pass
            else:
                break
            if timeout < 10:
                timeout *= 2

    def mark_orphaned(self):
        '''Mark the queue as orphaned; future additions will fail.'''
        self.__orphaned.set()

class WorkerThread(threading.Thread):
    '''A temporary thread, with exception logging done in parent.

    Output may go into a queue, which is automatically terminated after the
    _real_run method finishes.'''

    def __init__(self, name, description, input_queues=(), output_queues=()):
        '''Initialize.

        If input_queues and/or output_queues is specified, it is a list of
        (queue, EOF value) pairs.  A single queue may be repeated in the queue
        lists.

        '''
        super(WorkerThread, self).__init__(name=name)
        self.description = description
        self.input_queues = input_queues
        self.output_queues = output_queues
        self.exc_info = None
        self.ignored_exception_types = ()

    def run(self):
        try:
            try:
                self._real_run()
            finally:
                for (queue, _) in self.input_queues:
                    queue.mark_orphaned()
                for (queue, eof) in self.output_queues:
                    try:
                        queue.put(eof)
                    except WorkerQueueOrphanedError:
                        logging.debug('%s: Sending queue EOF failed, queue '
                                      'already orphaned', self.name)
                    except:
                        logging.warning('%s: Error sending queue EOF',
                                        self.name, exc_info=True)
        except:
            self.exc_info = sys.exc_info()
            if not isinstance(self.exc_info[1], self.ignored_exception_types):
                log_exception(self.name, self.exc_info,
                              ('Unexpected error in %s' % self.description))
            else:
                logging.debug('%s: Terminated by an exception %s' %
                              (self.name, repr(self.exc_info)))

    def _real_run(self):
        '''The real body of the thread.'''
        raise NotImplementedError

def run_worker_threads(threads, exception_types=()):
    '''Run the specified WorkerThreads.

    Start all threads, and wait for them in the specified order; make sure the
    EOF value is sent, if relevant.  Automatically log exceptions, except for
    exceptions in exception_types - be silent about such exceptions, only
    collect the exception info of first such exception.

    Return (no exceptions raised, first collected exception info).

    '''
    for t in threads:
        t.ignored_exception_types = exception_types
        t.start()

    ok = True
    exception = None
    for t in threads:
        # Terminate the input queues in case the producer threads crashed.  All
        # queues should be large enough that we can (eventually) safely add one
        # more element; if "t" livelocks and never reads the queue, we would
        # block on t.join() anyway.
        logging.debug('Sending final EOFs to %s...', t.name)
        for (queue, eof) in t.input_queues:
            try:
                queue.put(eof)
            except WorkerQueueOrphanedError:
                pass
        logging.debug('Waiting for %s...', t.name)
        t.join()
        logging.debug('%s finished, exc_info: %s', t.name, repr(t.exc_info))
        if t.exc_info is not None:
            ok = False
            if isinstance(t.exc_info[1], exception_types) and exception is None:
                exception = t.exc_info
    return (ok, exception)

 # Utilities for daemons

class DaemonIDConfiguration(Configuration):
    '''UID/GID configuration for a daemon.'''

    def _add_sections(self, sections):
        super(DaemonIDConfiguration, self)._add_sections(sections)
        sections.add('daemon')

    def _read_configuration(self, parser):
        super(DaemonIDConfiguration, self)._read_configuration(parser)
        user = parser.get('daemon', 'unix-user')
        if user == '':
            user = None
        if user is not None:
            try:
                user = pwd.getpwnam(user).pw_uid
            except KeyError:
                try:
                    user = int(user)
                except ValueError:
                    raise ConfigurationError('[daemon] unix-user \'%s\' not '
                                             'found' %
                                        user)
        self.daemon_uid = user
        group = parser.get('daemon', 'unix-group')
        if group == '':
            group = None
        if group is not None:
            try:
                group = grp.getgrnam(group).gr_gid
            except KeyError:
                try:
                    group = int(group)
                except ValueError:
                    raise ConfigurationError('[daemon] unix-group \'%s\' not '
                                             'found' % group)
        self.daemon_gid = group

def set_regid(config):
    '''Change real and effective GID according to config.'''
    if config.daemon_gid is not None:
        try:
            os.setregid(config.daemon_gid, config.daemon_gid)
        except:
            logging.error('Error switching to group %d: %s', config.daemon_gid,
                          sys.exc_info()[1])
            raise

def set_reuid(config):
    '''Change real and effective UID according to config.'''
    if config.daemon_uid is not None:
        try:
            os.setreuid(config.daemon_uid, config.daemon_uid)
        except:
            logging.error('Error switching to user %d: %s', config.daemon_uid,
                          sys.exc_info()[1])
            raise

def update_HOME_for_uid(config):
    '''Update $HOME for config.daemon_uid if necessary.'''
    if config.daemon_uid is not None:
        os.environ['HOME'] = pwd.getpwuid(config.daemon_uid).pw_dir

def daemonize():
    '''Fork and terminate the parent, prepare the child to run as a daemon.'''
    if os.fork() != 0:
        logging.shutdown()
        os._exit(0)
    os.setsid()
    os.chdir('/')
    try:
        fd = os.open('/dev/null', os.O_RDWR)
    except OSError:
        pass
    else:
        try:
            os.dup2(fd, 0)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
        finally:
            if fd > 2:
                try:
                    os.close(fd)
                except OSError:
                    pass

def create_pid_file(options, daemon_name):
    '''Create a PID file with the specified name.

    The options argument should come from get_daemon_options().

    '''
    f = open(os.path.join(options.pid_dir, daemon_name + '.pid'), 'w')
    try:
        f.write('%s\n' % os.getpid())
    finally:
        f.close()

def delete_pid_file(options, daemon_name):
    '''Delete a PID file with the specified name.

    The options argument should come from get_daemon_options().

    '''
    os.remove(os.path.join(options.pid_dir, daemon_name + '.pid'))

def sigterm_handler(*unused_args):
    sys.exit(0) # "raise SystemExit..."

 # Miscellaneous utilities

def copy_data(write_fn, read_fn, size):
    '''Copy size bytes using write_fn and read_fn.'''
    while size > 0:
        data = read_fn(min(size, 4096))
        write_fn(data)
        size -= len(data)

def file_size_in_blocks(fd):
    '''Return size of fd, taking into account block sizes.'''
    st = os.fstat(fd.fileno())
    # 512 is what (info libc) says.  See also <sys/stat.h> in POSIX.
    return st.st_blocks * 512

def path_size_in_blocks(path):
    '''Return size of path, taking into account block sizes.'''
    st = os.stat(path)
    # 512 is what (info libc) says.  See also <sys/stat.h> in POSIX.
    return st.st_blocks * 512

def log_exception(thread_name, exc_info, default_msg):
    '''Log exc_info, using default_msg if nothing better is known.

    Use thread_name if it is not None.
    '''
    if thread_name is not None:
        prefix = thread_name + ': '
    else:
        prefix = ''
    e = exc_info[1]
    if isinstance(e, (IOError, EOFError, socket.error)):
        logging.error(prefix + 'I/O error: %s' % repr(e))
    elif isinstance(e, nss.error.NSPRError):
        if e.errno == nss.error.PR_CONNECT_RESET_ERROR:
            logging.error(prefix + 'I/O error: NSPR connection reset')
        elif e.errno == nss.error.PR_END_OF_FILE_ERROR:
            logging.error(prefix + 'I/O error: Unexpected EOF in NSPR')
        else:
            logging.error(prefix + 'NSPR error', exc_info=exc_info)
    elif isinstance(e, WorkerQueueOrphanedError):
        logging.info(prefix + 'Writing to work queue failed, queue orphaned')
    else:
        logging.error(prefix + default_msg, exc_info=exc_info)

def read_password(config, prompt):
    '''Return a password using prompt, based on config.batch_mode.

    Raise EOFError.

    '''
    if not config.batch_mode:
        return getpass.getpass(prompt)
    password = ''
    while True:
        c = sys.stdin.read(1)
        if c == '\x00':
            break;
        if c == '':
            raise EOFError, 'Unexpected EOF when reading a batch mode password'
        password += c
    return password

def write_new_file(path, write_fn):
    '''Atomically replace file at path with data written by write_fn(fd).'''
    (dirname, basename) = os.path.split(path)
    (fd, tmp_path) = tempfile.mkstemp(prefix=basename, dir=dirname)
    remove_tmp_path = True
    f = None
    try:
        f = os.fdopen(fd, 'w')
        write_fn(f)
        try:
            st = os.stat(path)
        except OSError, e:
            if e.errno != errno.ENOENT:
                raise
        else:
            # fchmod is unfortunately not available
            os.chmod(tmp_path,
                     st.st_mode & (stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO))
        f.close()
        f = None
        backup_path = path + '~'
        try:
            os.remove(backup_path)
        except OSError:
            pass
        try:
            os.link(path, backup_path)
        except OSError, e:
            if e.errno != errno.ENOENT:
                raise
        os.rename(tmp_path, path)
        remove_tmp_path = False
    finally:
        if f:
            f.close()
        if remove_tmp_path:
            os.remove(tmp_path)
