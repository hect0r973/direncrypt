#------------------------------------------------------------------------------
# direncrypt - Sync contents between encrypted and decrypted directories
# Copyright (C) 2015  Domagoj Marsic
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Contact:
# https://github.com/dmarsic
# <dmars+github@protonmail.com>
#------------------------------------------------------------------------------

import os
import sys
import uuid
import logging
from direncrypt.gpgops import GPGOps
from direncrypt.inventory import Inventory
from direncrypt.fileops import FileOps
from direncrypt.util import printit


class DirEncryption(object):
    """DirEncryption encrypts and decrypts files between two directories.

    One directory serves as a repository of unencrypted files, and the
    other contains encrypted version of the same files.

    DirEncryption uses a SQLite database as a register of known files.
    Files from the source (unencrypted) directory will be encrypted
    into the destination directory if the modified time of a file
    is newer than the time of the last run.
    """

    def __init__(self, args, database=None):
        """Set program parameters and initialize GPG operations object."""
        if database is None:
            self.database = 'inventory.sqlite'
        else:
            self.database = database

        self.set_parameters(args)
        self.gpg = GPGOps(gpg_binary=self.gpg_binary,
                          gpg_recipient=self.public_id,
                          gpg_keyring=self.gpg_keyring)

    def set_parameters(self, args):
        """Set parameters based on database config and passed args."""
        self.verbose = False
        if args and args.verbose:
            self.verbose = True

        with Inventory(self.database) as i:
            parameters = i.read_parameters()
        for parameter, value in parameters.items():
            if self.verbose:
                printit('Parameters: {:<15} : {}', parameter, value)

        self.last_timestamp = parameters['last_timestamp']

        self.plaindir    = os.path.expanduser(parameters['plaindir'])
        self.securedir   = os.path.expanduser(parameters['securedir'])
        self.public_id   = parameters['public_id']
        self.gpg_keyring = parameters['gpg_keyring']
        self.gpg_homedir = os.path.expanduser(parameters['gpg_homedir'])
        self.gpg_binary  = os.path.expanduser(parameters['gpg_binary'])

        if args is None:
            return

        if args.plaindir:
            self.plaindir    = os.path.expanduser(args.plaindir)
        if args.securedir:
            self.securedir   = os.path.expanduser(args.securedir)
        if args.public_id:
            self.public_id   = args.public_id
        if args.gpg_keyring:
            self.gpg_keyring = args.gpg_keyring
        if args.gpg_homedir:
            self.gpg_homedir = os.path.expanduser(args.gpg_homedir)
        if args.gpg_binary:
            self.gpg_binary  = os.path.expanduser(args.gpg_binary)

    def encrypt_all(self):
        """Encrypt all new files from unencrypted directory.

        New files are those that have modified timestamp newer than
        the timestamp of the last run. At the start of run, the timestamp
        is updated.

        The files are recursively searched for in the source directory.
        """
        register = {}
        with Inventory(self.database) as inv:
            register = inv.read_all_register()
            inv.update_last_timestamp()
            # treat regular files first
            files = self.find_unencrypted_files(register)
            for plainfile, val in files.items():
                if not val['is_new']:
                    # remove old file in secure directory
                    encfile = inv.read_line_from_register(plainfile)
                    FileOps.delete_file(self.securedir, encfile)
                encryptedfile = self.generate_name()
                self.encrypt(plainfile, encryptedfile, inv, False)
                if self.verbose:
                    printit('Encrypted: {} ---> {}', plainfile, encryptedfile)
            # then treat symlinks
            links = self.find_unregistered_links(register)
            for link_name, val in links.items():
                if not val['is_new']:
                    # remove old link in register
                    inv.clean_record(link_name)
                self.encrypt(link_name, '', inv, True, val['target'])
                if self.verbose:
                    printit('Registered: {} ---> {}', link_name, val['target'])
                
    def encrypt(self, plainfile, encfile, inventory, is_link, target=None):
        """Encrypt the file and register input and output filenames."""
        plain_path = os.path.join(self.plaindir, plainfile)
        encrypted_path = os.path.join(self.securedir, encfile)
        # encrypt only regular files (not symlinks)
        if not is_link:
            self.gpg.encrypt(plain_path, encrypted_path)
        # save regular files and symlinks in register
        inventory.register(plainfile, encfile, self.public_id, is_link, target)

    def decrypt_all(self, passphrase):
        """Decrypt all files from encrypted source.

        Files that are being decrypted must be registered under the same
        public id in the database, so the passed passphrase would work
        for decryption process.
        """
        register = {}
        with Inventory(self.database) as i:
            register = i.read_all_register()
            for filename, record in register.items():
                if record['public_id'] != self.public_id:
                    continue
                # decrypt regular files
                if record['is_link'] == 0:
                    try:
                        self.decrypt(record['encrypted_file'],
                             record['unencrypted_file'],
                             passphrase)
                    except IOError as e:
                        logging.warning('decrypt_all: {}'.format(e))
                # restore symlinks
                elif record['is_link'] == 1:
                    plain_path = os.path.join(self.plaindir, record['unencrypted_file'])
                    FileOps.create_symlink(record['target'], plain_path)

    def decrypt(self, encfile, plainfile, phrase):
        """Decrypt the file using a supplied passphrase."""
        encrypted_path = os.path.join(self.securedir, encfile)
        plain_path = os.path.join(self.plaindir, plainfile)
        if self.verbose:
            printit('Decrypt: {} ---> {}', encrypted_path, plain_path)
        self.gpg.decrypt(encrypted_path, plain_path, phrase)

    def find_unencrypted_files(self, register):
        """List all files that need to be encrypted.

        os.walk does not expand tilde in paths, so the walk directory
        is explicitly expanded.

        register is the currently known list of encrypted files.

        Returns a dict, with relative path of the unencrypted files
        for keys, and is_new boolean flag for values.
        """
        files = {}
        if self.verbose:
            printit('Walking: {}', self.plaindir)

        for (dirpath, dirnames, filenames) in os.walk(self.plaindir):
            for name in filenames:
                filepath = os.path.join(dirpath, name)
                if os.path.islink(filepath):
                    continue
                statinfo = os.stat(filepath)
                mtime = statinfo.st_mtime
                relative_path = filepath[(len(self.plaindir) + 1):]
                if relative_path not in register:
                    # new file
                    enc_flag = '*'
                    files[relative_path] = {'is_new': True}
                elif relative_path in register and mtime > int(self.last_timestamp):
                    # file exists and has changed since last run
                    enc_flag = '*'
                    files[relative_path] = {'is_new': False}
                else:
                    # file has not changed since last run
                    enc_flag = ' '
                if self.verbose:
                    printit('List files: {} {} ({}): {}',
                            enc_flag, int(mtime), self.last_timestamp,
                            relative_path)
        return files

    def find_unregistered_links(self, register):
        """List all links that need to be registered.

        Returns a dict, with relative path of the unencrypted links
        for keys, having a dict with target of the link and
        is_new boolean flag for values.
        """
        links = {}
        if self.verbose:
            printit('Walking: {}', self.plaindir)
        links_to_treat = list()
        # get symlinks to directories
        for (dirpath, dirnames, filenames) in os.walk(self.plaindir, followlinks=True):
            if os.path.islink(dirpath):
                links_to_treat.append(dirpath)
        # get symlinks to files
        for (dirpath, dirnames, filenames) in os.walk(self.plaindir):
            for name in filenames:
                linkpath = os.path.join(dirpath, name)
                if not os.path.islink(linkpath):
                    continue
                links_to_treat.append(linkpath)
        for link in links_to_treat:
            # stat to the link and not his target
            statinfo = os.stat(link, follow_symlinks=False)
            mtime = statinfo.st_mtime
            linkto = os.readlink(link)
            relative_path = link[(len(self.plaindir) + 1):]
            if relative_path not in register:
                # new link
                enc_flag = '*'
                links[relative_path] = {'target': linkto, 'is_new': True}
            elif relative_path in register and mtime > int(self.last_timestamp):
                # link exists and has changed since last run
                enc_flag = '*'
                links[relative_path] = {'target': linkto, 'is_new': False}
            else:
                # link has not changed since last run
                enc_flag = ' '
            if self.verbose:
                printit('List links: {} {} ({}): {}',
                        enc_flag, int(mtime), self.last_timestamp,
                        relative_path)
        return links
         
    def generate_name(self):
        """Return a unique file name for encrypted file."""
        return str(uuid.uuid4())
