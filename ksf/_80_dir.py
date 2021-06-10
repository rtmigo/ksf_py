from pathlib import Path
from typing import Optional

from ksf._20_kdf import FilesetPrivateKey
from ksf._61_encryption import _DecryptedFile
from ksf._70_navigator import update_fileset, Fileset
from ksf.hidden_salt import find_salt_in_dir, write_salt


class CryptoDir:
    # todo test re-reading and finding salt
    def __init__(self, directory: Path):
        self.directory = directory

        salt = find_salt_in_dir(self.directory)
        if salt is None:
            salt, _ = write_salt(self.directory)
        assert isinstance(salt, bytes)
        self.salt = salt

    def set_from_file(self, name: str, source: Path):
        pk = FilesetPrivateKey(name, self.salt)
        update_fileset(source, pk, self.directory)

    def get(self, name: str, body=True) -> Optional[_DecryptedFile]:
        pk = FilesetPrivateKey(name, self.salt)
        fs = Fileset(self.directory, pk)
        if fs.real_file is None:
            return None
        return _DecryptedFile(fs.real_file, pk, decrypt_body=body)
