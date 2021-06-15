from typing import List, BinaryIO, Optional

from codn.container import BlobsIndexedReader
from codn.cryptodir._10_kdf import CodenameKey
from codn.cryptodir.namegroup.encdec import DecryptedIO


class NameGroupItem:
    def __init__(self, idx: int, dio: DecryptedIO):
        self.idx: int = idx
        self.dio: DecryptedIO = dio
        self.is_fresh_data: bool = False
        self.is_fake: bool = False


class NameGroup:
    """Inside the list of blobs, it detects those that are associated with
    the specified code name. It also finds out which of these blobs have
    fresh data, which blobs are outdated, and which are fake.

    The speed of searching for blobs depends linearly on the number of blobs,
    but does not depend on their size.

    According to the design, you can only check the contents of a blob if
    you know its code name (blobs are encrypted). Therefore, this object only
    finds and interprets blobs related to the name. The remaining blobs are
    ignored.
    """

    def __init__(self, blobs: BlobsIndexedReader, cnk: CodenameKey):
        self.blobs = blobs
        self.cnk = cnk
        self._streams: List[BinaryIO] = []
        self._fresh_content_dios: Optional[List[DecryptedIO]] = None

        self.items: List[NameGroupItem] = []

        for idx in range(len(self.blobs)):
            input_io = self.blobs.io(idx)
            assert input_io.tell() == 0
            dio = DecryptedIO(self.cnk, input_io)
            if not dio.belongs_to_namegroup:
                continue
            assert dio.belongs_to_namegroup
            gf = NameGroupItem(idx, dio)
            self.items.append(gf)

        # Marking fakes
        for f in self.items:
            if not f.dio.contains_data:
                f.is_fake = True
            else:
                assert not f.is_fake

        # Finding the latest content version
        #
        # This code was written for format version 1 of the storage. Each
        # blob was stored in a separate file. It was possible that we started
        # saving the new content version, but not all the parts (files) were
        # saved. For this reason, we did not look for not just the maximum
        # of the mentioned content versions, but the maximum with a full
        # set of parts.
        #
        # For format version 2, this problem does not exist: now we are
        # completely rewriting the entire file with all the blobs, and this
        # is an "atomic" operation. Only after saving all the parts, we give
        # the file a final name.
        #
        # However, this code is kept here just in case. Who knows, maybe the
        # next versions will update the file instead of rewriting it, and
        # and incomplete saving will be possible again.

        all_content_files = [gf for gf in self.items if gf.dio.contains_data]
        self.all_content_versions = set(gf.dio.header.data_version
                                        for gf in all_content_files)

        # trying versions from maximum to minimum
        for ver in sorted(self.all_content_versions, reverse=True):
            files_by_ver = [gf for gf in all_content_files
                            if gf.dio.header.data_version == ver]
            if files_by_ver[0].dio.header.parts_len == len(files_by_ver):
                # okay, this is the fresh content with all parts
                for gf in files_by_ver:
                    gf.is_fresh_data = True
                break

        #print("blobs", len(blobs), "items", len(self.items), "content", len(all_content_files), "versions", self.all_content_versions)

    @property
    def fresh_content_files(self) -> List[DecryptedIO]:
        if self._fresh_content_dios is None:
            self._fresh_content_dios = [gf.dio for gf in self.items
                                        if gf.is_fresh_data]
        return self._fresh_content_dios
