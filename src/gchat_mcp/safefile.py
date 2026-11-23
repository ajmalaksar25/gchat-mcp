"""Open a local file for upload safely: no symlink swap between check and use, secrets locations refused.

The file is opened once (O_NOFOLLOW, non-blocking so a FIFO cannot hang us); everything is then checked on the
opened descriptor (fstat for type and size, the real path of the descriptor for the denylist), and the same
descriptor is what gets uploaded.
"""
import errno
import fnmatch
import hashlib
import os
import stat
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover (Windows)
    fcntl = None

DENY_DIRS = [".config", ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".mozilla", ".local/share/keyrings",
             "Library/Keychains", "Library/Cookies", "Library/Safari", "Library/Application Support/Google/Chrome",
             "Library/Application Support/Firefox", "Library/Application Support/BraveSoftware",
             "Library/Application Support/Microsoft Edge", "Library/Application Support/Arc",
             "Library/Application Support/Vivaldi"]
DENY_NAMES = [".netrc", ".env*", "*.pem", "*.key", "*.p12", "*.pfx", "id_*", "token*.json", "credentials*.json",
              "client_secret*.json"]


def denied(real: Path, home: Path) -> str:
    """Why `real` (an already resolved path) may not be uploaded, or ''."""
    home = home.resolve()
    for d in DENY_DIRS:
        base = (home / d).resolve()
        if real == base or base in real.parents:
            return f"it is inside ~/{d}"
    for pat in DENY_NAMES:
        if fnmatch.fnmatch(real.name.lower(), pat.lower()):
            return f"its name matches {pat}"
    return ""


def fd_path(fd: int) -> Path:
    """The real path of an open descriptor (macOS F_GETPATH, Linux /proc)."""
    if fcntl is not None and hasattr(fcntl, "F_GETPATH"):
        return Path(os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0")[0]))
    return Path(os.readlink(f"/proc/self/fd/{fd}"))


def fingerprint(st) -> str:
    """`dev:ino:size:mtime_ns` of a stat result; what the approval hook records and send_file re-checks."""
    return f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"


def open_checked(path: str, cap: int, home: Path | None = None, expect_stat: str = "", expect_sha256: str = ""):
    """(binary file object, real path, size) for an upload; ValueError when refused. Caller closes the file."""
    p = os.path.expanduser(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(p, flags)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise ValueError(f"Refusing {path!r}: the final path component is a symbolic link; give the real path.") from e
        raise ValueError(f"Cannot open {path!r}: {e.strerror}") from e
    keep = False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"Not a regular file: {path!r}")
        if st.st_nlink > 1:
            raise ValueError(f"Refusing {path!r}: it has {st.st_nlink} hard links, so its name proves nothing about "
                             "what is behind it. Copy it to a fresh file and send that.")
        real = fd_path(fd)
        why = denied(real, home or Path.home())
        if why:
            raise ValueError(f"Refusing to upload {real.name}: {why} (credentials or secrets location).")
        if not 0 < st.st_size <= cap:
            raise ValueError(f"{real.name} is {st.st_size / (1 << 20):.1f} MB: Chat accepts 1 byte to {cap >> 20} MB.")
        if expect_stat and expect_stat != fingerprint(st):
            raise ValueError(f"Refusing {real.name}: it is not the file that was approved (device/inode/size/mtime "
                             "changed since the approval prompt). Ask again.")
        if expect_sha256:
            h, n = hashlib.sha256(), 0
            while chunk := os.read(fd, 1 << 20):
                h.update(chunk)
                n += len(chunk)
            os.lseek(fd, 0, os.SEEK_SET)
            after = os.fstat(fd)
            if h.hexdigest() != expect_sha256.lower() or fingerprint(after) != fingerprint(st):
                raise ValueError(f"Refusing {real.name}: its bytes are not the ones that were approved (sha256 differs "
                                 "or the file changed while being read). Ask again.")
        f = os.fdopen(fd, "rb")
        keep = True
        return f, real, st.st_size
    finally:
        if not keep:
            os.close(fd)
