# SPDX-FileCopyrightText: (c) 2021 Artёm IG <github.com/rtmigo>
# SPDX-License-Identifier: MIT

import io
import os
import random
import zlib
from pathlib import Path
from typing import Optional, NamedTuple, BinaryIO, Union

from Crypto.Cipher import ChaCha20
from Crypto.Random import get_random_bytes

from codn._common import read_or_fail, InsufficientData, MAX_BLOB_SIZE, \
    MAX_PART_CONTENT_SIZE
from codn.cryptodir._10_kdf import CodenameKey
from codn.cryptodir.namegroup.imprint import Imprint, pk_matches_imprint_bytes
from codn.utils.dirty_file import WritingToTempFile
from codn.utils.randoms import set_random_last_modified
from ._10_padding import IntroPadding
from ._20_byte_funcs import bytes_to_uint32, \
    bytes_to_int64, uint32_to_bytes, int64_to_bytes, uint8_to_bytes, \
    uint24_to_bytes, bytes_to_uint8, bytes_to_uint24

_DEBUG_PRINT = False


class ChecksumMismatch(Exception):
    pass


class GroupImprintMismatch(Exception):
    pass


class ItemImprintMismatch(Exception):
    pass


_intro_padding_64 = IntroPadding(64)

ENCRYPTION_NONCE_LEN = 8
MAC_LEN = 16
HEADER_CHECKSUM_LEN = 4
VERSION_LEN = 1


def bytes_to_str(lst: bytes):
    result = '['
    if len(lst) > 0:
        result += hex(lst[0])[2:]
    if len(lst) > 1:
        result += ' ' + hex(lst[1])[2:]
    if len(lst) > 2:
        result += ' .. ' + hex(lst[-1])[2:]
    result += ']'
    result += f' len {len(lst)}'
    return result


class Cryptographer:
    def __init__(self,
                 fpk: CodenameKey,
                 nonce: Optional[bytes]):
        self.fpk = fpk
        if nonce is not None:
            self.cipher = ChaCha20.new(key=self.fpk.as_bytes, nonce=nonce)
        else:
            self.cipher = ChaCha20.new(key=self.fpk.as_bytes)

    @property
    def nonce(self):
        return self.cipher.nonce

    def __str__(self):
        return '\n'.join([
            f'fpk: {self.fpk.as_bytes}',

            f'nonce: {bytes_to_str(self.nonce)}',
        ])


class Header(NamedTuple):
    format_version: int  # todo rename
    data_version: int  # todo rename
    data_size: int  # todo rename
    parts_len: int
    part_idx: int
    part_size: int


def get_stream_size(stream: BinaryIO) -> int:
    pos = stream.seek(0, io.SEEK_CUR)
    size = stream.seek(0, io.SEEK_END)
    stream.seek(pos, io.SEEK_SET)
    return size


class Encrypt:
    def __init__(self,
                 cnk: CodenameKey,
                 data_version: int = 0,
                 # original_size: int = None,
                 part_idx: int = 0,
                 parts_len: int = 1,
                 part_size: int = None):

        if not 0 <= part_idx <= 0xFF:
            raise ValueError(f"part_idx={part_idx}")
        if not 1 <= parts_len <= 0xFF + 1:
            raise ValueError(f"parts_len={parts_len}")
        if part_size is not None and not (
                0 <= part_size <= MAX_PART_CONTENT_SIZE):
            raise ValueError(f"part_size={part_size}")

        self.cnk = cnk
        self.data_version = data_version

        # self.original_size = original_size

        if not 0 <= part_idx <= 0xFF:
            raise ValueError(part_idx)

        self.part_idx = part_idx

        if not 0 <= parts_len <= 0xFFFFFF:
            raise ValueError(part_idx)

        self.parts_len = parts_len

        if part_size is None and not (part_idx == 0 and parts_len == 1):
            raise ValueError("part_size is not specified")
        self.part_size = part_size

    def io_to_io(self,
                 source: BinaryIO,
                 outfile: BinaryIO):
        """
        File format
        -----------

        <imprint A/>
        <imprint B/>
        <encrypted>
            intro padding: bytes (1-64 bytes)

            <header>
                FORMAT_ID     (2 bytes) 'LS': format identifier, two bytes

                FORMAT_VER    (uint8)   Always 1

                ITEM_VER      (int64)   Increases on each write

                FULL_SIZE     (uint32)  Total size of the original data

                PARTS_LEN     (uint8)  The total number of parts (files)
                                        into which the original data was split

                PART_IDX      (uint8)   Zero-based part number contained in
                                        the current file

                PART_SIZE     (uint24)  Size of the part contained in the
                                        current file. This is the number of
                                        bytes to read from the source_io
            </header>

            HEADER_CRC  (uint32) The CRC-32 checksum of the header

            body: bytes
            body crc-32: uint32
        </encrypted>
        <random-padding/>
        ****


        When decrypting, we will check the name against the imprint. After that,
        we assume that the name is correct and the data can be successfully
        decrypted by this name.

        Instead of cryptographic MAC algorithms, we will use CRC32 for header
        and CRC32 for body. This will help us verify that the program is working
        as expected and that the contents of the file is correct. The CRC values
        themselves will be encrypted.

        """

        imprint_a = Imprint(self.cnk)
        imprint_b = Imprint(self.cnk)
        assert imprint_a.as_bytes != imprint_b.as_bytes

        # if total_size is None:

        # FORMAT_VER
        format_ver_bytes = bytes([1])

        # ITEM_VER
        item_version_bytes = int64_to_bytes(self.data_version)

        # FORMAT_ID
        format_id = 'LS'.encode('ascii')  # little secret
        assert len(format_id) == 2

        # FULL_SIZE
        full_size = get_stream_size(source)
        full_size_bytes = uint32_to_bytes(full_size)

        # PART_SIZE
        if self.part_size is None:
            assert self.parts_len == 1 and self.part_idx == 0
            self.part_size = full_size

        parts_len_bytes = uint8_to_bytes(self.parts_len - 1)
        part_idx_bytes = uint8_to_bytes(self.part_idx)
        part_size_bytes = uint24_to_bytes(self.part_size)

        header_bytes = b''.join((
            format_id,
            format_ver_bytes,
            item_version_bytes,
            full_size_bytes,
            parts_len_bytes,
            part_idx_bytes,
            part_size_bytes
        ))

        header_crc_bytes = uint32_to_bytes(zlib.crc32(header_bytes))

        body_bytes = read_or_fail(source, self.part_size)
        body_crc_bytes = uint32_to_bytes(zlib.crc32(body_bytes))

        cryptographer = Cryptographer(fpk=self.cnk,
                                      nonce=None)

        if _DEBUG_PRINT:
            print("---")
            print("ENCRYPTION:")
            print(cryptographer)
            print("---")

        # writing the imprint. It is not encrypted, but it's a hash +
        # random nonce. It's indistinguishable from any random rubbish
        outfile.write(imprint_a.as_bytes)
        outfile.write(imprint_b.as_bytes)

        assert len(cryptographer.nonce) == ENCRYPTION_NONCE_LEN, \
            f"Unexpected nonce length: {len(cryptographer.nonce)}"

        outfile.write(cryptographer.nonce)

        assert outfile.seek(0, io.SEEK_CUR) <= 1024

        def encrypt_and_write(data: bytes):
            outfile.write(cryptographer.cipher.encrypt(data))

        encrypt_and_write(_intro_padding_64.gen_bytes())
        encrypt_and_write(header_bytes)
        encrypt_and_write(header_crc_bytes)
        # todo chunked r/w
        encrypt_and_write(body_bytes)
        encrypt_and_write(body_crc_bytes)

        # adding random data to the end of file.
        # This data is not encrypted, it's from urandom (is it ok?)
        current_size = outfile.seek(0, os.SEEK_CUR)
        assert current_size <= MAX_BLOB_SIZE, current_size

        target_size = random.randint(current_size, MAX_BLOB_SIZE)

        padding_size = target_size - current_size
        assert padding_size >= 0
        outfile.write(get_random_bytes(padding_size))

        assert outfile.seek(0, os.SEEK_CUR) <= MAX_BLOB_SIZE

    def io_to_file(self,
                   source_io: BinaryIO,
                   target_file: Path):
        with WritingToTempFile(target_file) as wtf:
            # must be the same as writing a fake file
            with wtf.dirty.open('wb') as outfile:
                self.io_to_io(source_io, outfile)
            set_random_last_modified(wtf.dirty)
            # dirty file is written AND closed (important for Windows)
            wtf.replace()

    def file_to_file(self, source_file: Path, target_file: Path):
        with source_file.open('rb') as source_io:
            self.io_to_file(source_io, target_file)


class DecryptedIO:
    """
    Reads encrypted data in a "lazy manner".

    After the object is created, only the imprint is read and checked.
    After accessing the `header` property, the header is read.
    After calling read_data() - the data itself (and the header).
    """

    def __init__(self,
                 fpk: CodenameKey,
                 source: Union[bytes, BinaryIO]):

        if isinstance(source, bytes):
            data = source
        else:
            # todo make this obsolete, only accept bytes
            source.seek(0, io.SEEK_SET)
            data = source.read()



        self.fpk = fpk
        self._source: io.BytesIO = io.BytesIO(data)

        self._header: Optional[Header] = None
        self._data_read = False

        self._belongs_to_namegroup: Optional[bool] = None
        self._contains_data: Optional[bool] = None

        self._imprint_a_checked = False
        self._imprint_b_checked = False

        pos = self._source.seek(0, io.SEEK_CUR)
        if pos != 0:
            raise ValueError(f"Unexpected stream position {pos}")

        # self.__read_imprint()

    def __read_and_decrypt(self, n: int) -> bytes:
        encrypted = self._source.read(n)
        assert encrypted is not None
        if len(encrypted) < n:
            raise InsufficientData
        return self.cfg.cipher.decrypt(encrypted)

    @property
    def belongs_to_namegroup(self) -> bool:
        # reads and interprets IMPRINT_A
        # pos = self.source.seek(0, io.SEEK_CUR)
        # if pos != 0:
        #     raise ValueError(f"Unexpected stream position: {pos}")

        if self._belongs_to_namegroup is None:
            try:
                self._source.seek(0, io.SEEK_SET) # todo temp

                pos = self._source.tell()
                if pos != 0:
                    raise ValueError(f"Unexpected stream position {pos}")
                imp = read_or_fail(self._source, Imprint.FULL_LEN)
                self._belongs_to_namegroup = \
                    pk_matches_imprint_bytes(self.fpk, imp)
            except InsufficientData:
                self._belongs_to_namegroup = False
        assert self._belongs_to_namegroup is not None
        return self._belongs_to_namegroup

    @property
    def contains_data(self) -> bool:
        # reads and interprets IMPRINT_B

        if not self.belongs_to_namegroup:
            return False

        if self._contains_data is None:
            try:
                imp = read_or_fail(self._source, Imprint.FULL_LEN)
                self._contains_data = pk_matches_imprint_bytes(self.fpk, imp)
            except InsufficientData:
                self._contains_data = False

        assert self._contains_data is not None
        return self._contains_data

    @property
    def header(self) -> Header:
        if not self.belongs_to_namegroup:
            raise GroupImprintMismatch
        if not self.contains_data:
            raise ItemImprintMismatch
        if self._header is None:
            self._header = self.__read_header()
        assert self._header is not None
        return self._header

    def __read_header(self) -> Header:

        nonce = read_or_fail(self._source, ENCRYPTION_NONCE_LEN)

        self.cfg = Cryptographer(fpk=self.fpk, nonce=nonce)

        if _DEBUG_PRINT:
            print("---")
            print("DECRYPTION:")
            print(self.cfg)
            print("---")

        # skipping the padding
        ip_first = self.__read_and_decrypt(1)[0]
        ip_len = _intro_padding_64.first_byte_to_len(ip_first)
        if ip_len > 0:
            self.__read_and_decrypt(ip_len)

        format_id = self.__read_and_decrypt(2)
        assert format_id.decode('ascii') == "LS"

        # FORMAT VERSION is always 1
        format_version_bytes = self.__read_and_decrypt(VERSION_LEN)
        format_version = format_version_bytes[0]
        assert format_version == 1

        data_version_bytes = self.__read_and_decrypt(8)
        data_version = bytes_to_int64(data_version_bytes)

        # FULL_SIZE is the size of original file
        full_size_bytes = self.__read_and_decrypt(4)
        size = bytes_to_uint32(full_size_bytes)

        # PARTS_LEN
        parts_len_bytes = self.__read_and_decrypt(1)
        parts_len = bytes_to_uint8(parts_len_bytes) + 1

        # PART_IDX
        part_idx_bytes = self.__read_and_decrypt(1)
        part_idx = bytes_to_uint8(part_idx_bytes)

        # PART_SIZE
        part_size_bytes = self.__read_and_decrypt(3)
        part_size = bytes_to_uint24(part_size_bytes)

        # reading and checking the header checksum
        header_crc = int.from_bytes(
            self.__read_and_decrypt(HEADER_CHECKSUM_LEN),
            byteorder='big',
            signed=False)
        header_bytes = b''.join((format_id,
                                 format_version_bytes,
                                 data_version_bytes,
                                 full_size_bytes,
                                 parts_len_bytes,
                                 part_idx_bytes,
                                 part_size_bytes))
        if zlib.crc32(header_bytes) != header_crc:
            raise ChecksumMismatch("Header CRC mismatch.")

        return Header(data_version=data_version,
                      format_version=format_version,
                      data_size=size,
                      part_size=part_size,
                      parts_len=parts_len,
                      part_idx=part_idx)

    def read_data(self) -> bytes:
        if self._data_read:
            raise RuntimeError("Cannot read data more than once")

        _ = self.header

        body = self.__read_and_decrypt(self.header.part_size)
        body_crc = bytes_to_uint32(self.__read_and_decrypt(4))
        if zlib.crc32(body) != body_crc:
            raise ChecksumMismatch("Body CRC mismatch.")

        self._data_read = True
        return body


class _DecryptedFile:
    """It is better to always use DecryptedIO instead of this class.
    The class is kept for temporary compatibility with tests."""

    # todo remove all usages of this class
    def __init__(self,
                 source_file: Path,
                 fpk: CodenameKey,
                 decrypt_body=True):

        with source_file.open('rb') as f:
            di = DecryptedIO(fpk, f)
            self.data_version = di.header.data_version
            self.size = di.header.data_size

            self.data: Optional[bytes]
            if decrypt_body:
                self.data = di.read_data()
            else:
                self.data = None

    def write(self, target: Path):
        if self.data is None:
            raise RuntimeError("Body is not set.")
        target.write_bytes(self.data)


# def encrypt_file_to_dir(source_file: Path, fpk: FilesetPrivateKey,
#                         target_dir: Path,
#                         data_version: int = 0) -> Path:
#     # todo remove this method
#     with source_file.open('rb') as f:
#         return encrypt_io_to_dir(f,
#                                  fpk,
#                                  target_dir,
#                                  data_version)
#
#
# def encrypt_io_to_dir(source_io: BinaryIO,
#                       fpk: FilesetPrivateKey,
#                       target_dir: Path,
#                       data_version: int = 0) -> Path:
#     # todo remove
#     raise NotImplemented
#     fn = unique_filename(target_dir)  # / imprint.as_str
#     assert looks_like_random_basename(fn.name)
#     if fn.exists():
#         raise HashCollision
#     Encrypt(fpk, data_version).io_to_file(source_io, fn)
#     return fn


def is_file_from_namegroup(fpk: CodenameKey, file: Path) -> bool:
    with file.open('rb') as f:
        return DecryptedIO(fpk, f).belongs_to_namegroup


def is_content(fpk: CodenameKey, file: Path) -> bool:
    with file.open('rb') as f:
        return DecryptedIO(fpk, f).contains_data


def is_fake(fpk: CodenameKey, file: Path) -> bool:
    with file.open('rb') as f:
        dio = DecryptedIO(fpk, f)
        return dio.belongs_to_namegroup and not dio.contains_data


def is_content_io(fpk: CodenameKey, stream: BinaryIO) -> bool:
    return DecryptedIO(fpk, stream).contains_data


def is_fake_io(fpk: CodenameKey, stream: BinaryIO) -> bool:
    dio = DecryptedIO(fpk, stream)
    return dio.belongs_to_namegroup and not dio.contains_data

    # is_file_from_namegroup(fpk, f)
    # with file.open('rb') as f:
#        return DecryptedIO(fpk, f).contains_data
