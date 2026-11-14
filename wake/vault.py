"""Where your things are kept before an agent's call changes or removes them.

A file that is about to be deleted, replaced or moved is kept as a hard link: the same
file, no copy, instant. A file that is about to be written in place is kept as a copy
(a reflink where the filesystem supports one). A hard link is turned into a copy the
moment anyone is about to write into the file.
"""
import fcntl
import os
import shutil

FICLONE = 0x40049409


class Vault:
    def __init__(self, root):
        self.root = root
        for area in ("kept", "removed", "displaced"):
            os.makedirs(os.path.join(root, area), exist_ok=True)
        self.n = sum(len(os.listdir(os.path.join(root, a))) for a in ("kept", "removed", "displaced"))

    def _dest(self, area):
        self.n += 1
        return os.path.join(self.root, area, f"{self.n:07d}")

    @staticmethod
    def copy(src, dest):
        if os.path.islink(src):
            os.symlink(os.readlink(src), dest)
            return
        try:
            with open(src, "rb") as s, open(dest, "wb") as d:
                fcntl.ioctl(d.fileno(), FICLONE, s.fileno())
            shutil.copystat(src, dest, follow_symlinks=False)
            return
        except OSError:
            pass
        shutil.copy2(src, dest, follow_symlinks=False)

    def keep(self, path, prefer_link):
        dest = self._dest("kept")
        if prefer_link:
            try:
                os.link(path, dest, follow_symlinks=False)
                return dest, "link"
            except OSError:
                pass
        self.copy(path, dest)
        return dest, "copy"

    def to_copy(self, store):
        """Make a kept hard link independent, before the file it shares is written."""
        tmp = store + ".copy"
        self.copy(store, tmp)
        os.replace(tmp, store)
        return store, "copy"

    def move_in(self, path, area):
        """Move something out of the way into the vault, without destroying it."""
        dest = self._dest(area)
        try:
            os.rename(path, dest)
        except OSError:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.copytree(path, dest, symlinks=True)
                shutil.rmtree(path)
            else:
                self.copy(path, dest)
                os.unlink(path)
        return dest

    def size(self):
        total = 0
        for d, _, files in os.walk(self.root):
            for f in files:
                try:
                    st = os.lstat(os.path.join(d, f))
                    if st.st_nlink == 1:
                        total += st.st_size
                except OSError:
                    pass
        return total
