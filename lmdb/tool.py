"""
Basic tools for working with LMDB.

    copy: Consistent high speed backup an environment.
        %prog copy -e source.lmdb target.lmdb

    copyfd: Consistent high speed backup an environment to stdout.
        %prog copyfd -e source.lmdb > target.lmdb/data.mdb

    drop: Delete one or more sub-databases.
        %prog drop db1

    dump: Dump one or more databases to disk in 'cdbmake' format.
        Usage: dump [db1=file1.cdbmake db2=file2.cdbmake]

        If no databases are given, dumps the main database to 'main.cdbmake'.

    edit: Add/delete/replace values from a database.
        %prog edit --set key=value --set-file key=/path \\
                   --add key=value --add-file key=/path/to/file \\
                   --delete key

    get: Read one or more values from a database.
        %prog get [<key1> [<keyN> [..]]]

    restore: Read one or more database from disk in 'cdbmake' format.
        %prog restore db1=file1.cdbmake db2=file2.cdbmake

        The special db name ":main:" may be used to indicate the main DB.

    rewrite: Re-create an environment using MDB_APPEND
        %prog rewrite -e src.lmdb -E dst.lmdb [<db1> [<dbN> ..]]

        If no databases are given, rewrites only the main database.

    shell: Open interactive console with ENV set to the open environment.

    stat: Print environment statistics.

    warm: Read environment into page cache sequentially.

    watch: Show live environment statistics
"""

from __future__ import absolute_import
from __future__ import with_statement
import array
import collections
import contextlib
import csv
import fcntl
import functools
import optparse
import os
import pprint
import signal
import string
import struct
import sys
import termios
import time

# Python3.x bikeshedded trechery.
try:
    from io import BytesIO as StringIO
except ImportError:
    try:
        from cStringIO import StringIO
    except ImportError:
        from StringIO import StringIO

import lmdb


BUF_SIZE = 10485760
ENV = None
DB = None


def isprint(c):
    """Return ``True`` if the character `c` can be printed visibly and without
    adversely affecting printing position (e.g. newline)."""
    return c in string.printable and ord(c) > 16


def xxd(s):
    """Return a vaguely /usr/bin/xxd formatted representation of the bytestring
    `s`."""
    sio = StringIO()
    for idx, ch in enumerate(s):
        if not (idx % 16):
            if idx:
                sio.write('  ')
                sio.write(pr)
                sio.write('\n')
            sio.write('%07x:' % idx)
            pr = ''
        if not (idx % 2):
            sio.write(' ')
        sio.write('%02x' % (ord(ch),))
        pr += ch if isprint(ch) else '.'

    if idx % 16:
        need = 15 - (idx % 16)
        # fill remainder of last line.
        sio.write('  ' * need)
        sio.write(' ' * (need / 2))
        sio.write('  ')
        sio.write(pr)

    sio.write('\n')
    return sio.getvalue()


def make_parser():
    parser = optparse.OptionParser()
    parser.prog = 'python -mlmdb'
    parser.usage = '%prog [options] <command>\n' + __doc__.rstrip()
    parser.add_option('-e', '--env', help='Environment file to open')
    parser.add_option('-d', '--db', help='Database to open (default: main)')
    parser.add_option('-r', '--read', help='Open environment read-only')
    parser.add_option('-S', '--map_size', type='int', default='10',
                      help='Map size in megabytes (default: 10)')
    parser.add_option('-a', '--all', action='store_true',
                      help='Make "dump" dump all databases')
    parser.add_option('-T', '--txn_size', type='int', default=1000,
                      help='Writes per transaction (default: 1000)')
    parser.add_option('-E', '--target_env',
                      help='Target environment file for "dumpfd"')
    parser.add_option('-x', '--xxd', action='store_true',
                      help='Print values in xxd format')
    parser.add_option('-M', '--max-dbs', type='int', default=128,
                      help='Maximum open DBs (default: 128)')
    parser.add_option('--out-fd', type='int', default=1,
                      help='"copyfd" command target fd')
    group = parser.add_option_group('Options for "edit" command')
    group.add_option('--set', action='append',
                     help='List of key=value pairs to set.')
    group.add_option('--set-file', action='append',
                     help='List of key pairs to read from files.')
    group.add_option('--add', action='append',
                     help='List of key=value pairs to add.')
    group.add_option('--add-file', action='append',
                     help='List of key pairs to read from files.')
    group.add_option('--delete', action='append',
                     help='List of key=value pairs to delete.')
    group = parser.add_option_group('Options for "watch" command')
    group.add_option('--csv', action='store_true',
                     help='Generate CSV instead of terminal output.')
    group.add_option('--interval', type='int', default=1,
                     help='Interval size (default: 1sec)')
    group.add_option('--window', type='int', default=10,
                     help='Average window size (default: 10)')
    return parser


def die(fmt, *args):
    if args:
        fmt %= args
    sys.stderr.write('lmdb.tool: %s\n' % (fmt,))
    raise SystemExit(1)


def dump_cursor_to_fp(cursor, fp):
    for key, value in cursor:
        fp.write('+%d,%d:' % (len(key), len(value)))
        fp.write(key)
        fp.write('->')
        fp.write(value)
        fp.write('\n')
    fp.write('\n')


def db_map_from_args(args):
    db_map = {}

    for arg in args:
        dbname, sep, path = arg.partition('=')
        if not sep:
            die('DB specification missing "=": %r', arg)

        if dbname == ':main:':
            dbname = None
        if dbname in db_map:
            die('DB specified twice: %r', arg)
        db_map[dbname] = (ENV.open_db(dbname), path)

    if not db_map:
        db_map[':main:'] = (ENV.open_db(None), 'main.cdbmake')
    return db_map


def cmd_copy(opts, args):
    if len(args) != 1:
        die('Please specify output directory (see --help)')

    output_dir = args[0]
    if os.path.exists(output_dir):
        die('Output directory %r already exists.', output_dir)

    os.makedirs(output_dir, int('0755', 8))
    print('Running copy to %r....' % (output_dir,))
    ENV.copy(output_dir)


def cmd_copyfd(opts, args):
    if args:
        die('"copyfd" command takes no arguments (see --help)')

    try:
        fp = os.fdopen(opts.out_fd, 'w', 0)
    except OSError:
        e = sys.exc_info()[1]
        die('Bad --out-fd %d: %s', opts.out_fd, e)

    ENV.copyfd(opts.out_fd)


def cmd_dump(opts, args):
    db_map = db_map_from_args(args)
    with ENV.begin(buffers=True) as txn:
        for dbname, (db, path) in db_map.iteritems():
            with open(path, 'wb', BUF_SIZE) as fp:
                print('Dumping to %r...' % (path,))
                cursor = txn.cursor(db=db)
                dump_cursor_to_fp(cursor, fp)


def restore_cursor_from_fp(txn, fp, db):
    read = fp.read
    read1 = functools.partial(read, 1)
    read_until = lambda sep: ''.join(iter(read1, sep))

    rec_nr = 0

    while True:
        rec_nr += 1
        plus = read(1)
        if plus == '\n':
            break
        elif plus != '+':
            die('bad or missing plus, line/record #%d', rec_nr)

        bad = False
        try:
            klen = int(read_until(','), 10)
            dlen = int(read_until(':'), 10)
        except ValueError:
            die('bad or missing length, line/record #%d', rec_nr)

        key = read(klen)
        if read(2) != '->':
            die('bad or missing separator, line/record #%d', rec_nr)

        data = read(dlen)
        if (len(key) + len(data)) != (klen + dlen):
            die('short key or data, line/record #%d', rec_nr)

        if read(1) != '\n':
            die('bad line ending, line/record #%d', rec_nr)

        txn.put(key, data, db=db)

    return rec_nr


def cmd_drop(opts, args):
    if not args:
        die('Must specify at least one sub-database (see --help)')

    dbs = map(ENV.open_db, args)
    for idx, db in enumerate(dbs):
        name = args[idx]
        if name == ':main:':
            die('Cannot drop main DB')
        print('Dropping DB %r...' % (name,))
        with ENV.begin(write=True) as txn:
            txn.drop(db)


def cmd_restore(opts, args):
    db_map = db_map_from_args(args)
    with ENV.begin(buffers=True, write=True) as txn:
        for dbname, (db, path) in db_map.iteritems():
            with open(path, 'rb', BUF_SIZE) as fp:
                print('Restoring from %r...' % (path,))
                count = restore_cursor_from_fp(txn, fp, db)
                print('Loaded %d keys from %r' % (count, path))


def delta(hst):
    return [(hst[i] - hst[i-1]) for i in xrange(1, len(hst))]

def cmd_watch(opts, args):
    info = None
    stat = None

    def window(func):
        history = collections.deque()
        def windowfunc():
            history.append(func())
            if len(history) > opts.window:
                history.popleft()
            if len(history) <= 1:
                return 0
            n = sum(delta(history)) / float(len(history) - 1)
            return n / opts.interval
        return windowfunc

    envmb = lambda: (info['last_pgno'] * stat['psize']) / 1048576.

    cols = [
        ('%d',    'Depth', lambda: stat['depth']),
        ('%d',    'Branch', lambda: stat['branch_pages']),
        ('%d',    'Leaf', lambda: stat['leaf_pages']),
        ('%+d',   'Leaf/s', window(lambda: stat['leaf_pages'])),
        ('%d',    'Oflow', lambda: stat['overflow_pages']),
        ('%+d',   'Oflow/s', window(lambda: stat['overflow_pages'])),
        ('%d',    'Recs', lambda: stat['entries']),
        ('%+d',   'Recs/s', window(lambda: stat['entries'])),
        ('%d',    'Rdrs', lambda: info['num_readers']),
        ('%.2f',  'EnvMb', envmb),
        ('%+.2f', 'EnvMb/s', window(envmb)),
        ('%d',    'Txs', lambda: info['last_txnid']),
        ('%+.2f', 'Txs/s', window(lambda: info['last_txnid']))
    ]

    term_width = 0
    widths = [len(head) for _, head, _ in cols]

    if opts.csv:
        writer = csv.writer(sys.stdout, quoting=csv.QUOTE_ALL)
        writer.writerow([head for _, head, _ in cols])

    cnt = 0
    try:
        while True:
            stat = ENV.stat()
            info = ENV.info()

            vals = []
            for i, (fmt, head, func) in enumerate(cols):
                val = fmt % func()
                vals.append(val)
                widths[i] = max(widths[i], len(val))

            if opts.csv:
                writer.writerow(vals)
            else:
                if term_width != _TERM_WIDTH or not (cnt % (_TERM_HEIGHT - 2)):
                    for i, (fmt, head, func) in enumerate(cols):
                        sys.stdout.write(head.rjust(widths[i] + 1))
                    sys.stdout.write('\n')
                    term_width = _TERM_WIDTH
                for i, val in enumerate(vals):
                    sys.stdout.write(val.rjust(widths[i] + 1))
                sys.stdout.write('\n')

            time.sleep(opts.interval)
            cnt += 1
    except KeyboardInterrupt:
        pass


def cmd_warm(opts, args):
    stat = ENV.stat()
    info = ENV.info()

    bufsize = 32768
    last_offset = stat['psize'] * info['last_pgno']
    buf = array.array('c', '\x00' * bufsize)
    t0 = time.time()

    fp = open(opts.env + '/data.mdb', 'rb', bufsize)
    while fp.tell() < last_offset:
        fp.readinto(buf)
    print('Warmed %.2fmb in %dms' %
        (last_offset / 1048576., 1000 * (time.time() - t0)))


def cmd_rewrite(opts, args):
    if not opts.target_env:
        die('Must specify target environment path with -E')

    src_info = ENV.info()
    target_env = lmdb.open(opts.target_env,
                           map_size=src_info['map_size'] * 2,
                           max_dbs=opts.max_dbs, sync=False,
                           writemap=True, map_async=True,
                           metasync=False)

    dbs = []
    for arg in args:
        name = None if arg == ':main:' else arg
        src_db = ENV.open_db(name)
        dst_db = ENV.open_db(name)
        dbs.append((arg, src_db, dst_db))

    if not dbs:
        dbs.append((':main:', ENV.open_db(None), target_env.open_db(None)))

    for name, src_db, dst_db in dbs:
        print('Writing %r...' % (name,))
        with target_env.begin(db=dst_db, write=True) as wtxn:
            for key, value in ENV.cursor(db=src_db, buffers=True):
                wtxn.put(key, value, append=True)

    print('Syncing..')
    target_env.sync(True)


def cmd_get(opts, args):
    print_header = len(args) > 1

    with ENV.begin(buffers=True, db=DB) as txn:
        for arg in args:
            value = txn.get(arg)
            if value is None:
                print('%r: missing' % (arg,))
                continue
            if print_header:
                print('%r:' % (arg,))
            if opts.xxd:
                print(xxd(value))
            else:
                print(value)


def cmd_edit(opts, args):
    if args:
        die('Edit command only takes options, not arguments (see --help)')

    with ENV.begin(write=True) as txn:
        cursor = txn.cursor(db=DB)
        for elem in opts.add or []:
            key, _, value = elem.partition('=')
            cursor.put(key, value, overwrite=False)

        for elem in opts.set or []:
            key, _, value = elem.partition('=')
            cursor.put(key, value)

        for key in opts.delete or []:
            cursor.delete(key)

        for elem in opts.add_file or []:
            key, _, path = elem.partition('=')
            with open(path, 'rb') as fp:
                cursor.put(key, fp.read(), overwrite=False)

        for elem in opts.set_file or []:
            key, _, path = elem.partition('=')
            with open(path, 'rb') as fp:
                cursor.put(key, fp.read())


def cmd_shell(opts, args):
    import code
    import readline
    code.InteractiveConsole(globals()).interact()


def cmd_stat(opts, args):
    pprint.pprint(ENV.stat())
    pprint.pprint(ENV.info())


def _get_term_width(default=80):
    try:
        s = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, '1234')
        height, width = struct.unpack('hh', s)
        return width, height
    except:
        return default

def _on_sigwinch(*args):
    global _TERM_WIDTH, _TERM_HEIGHT
    _TERM_WIDTH, _TERM_HEIGHT = _get_term_width()

def main():
    parser = make_parser()
    opts, args = parser.parse_args()

    if not args:
        die('Please specify a command (see --help)')
    if not opts.env:
        die('Please specify environment (--env)')

    global ENV
    ENV = lmdb.open(opts.env, map_size=opts.map_size*1048576,
                    max_dbs=opts.max_dbs, create=False)

    if opts.db:
        global DB
        DB = ENV.open_db(opts.db)

    signal.signal(signal.SIGWINCH, _on_sigwinch)
    _on_sigwinch()

    func = globals().get('cmd_' + args[0])
    if not func:
        die('No such command: %r' % (args[0],))

    func(opts, args[1:])


if __name__ == '__main__':
    main()
