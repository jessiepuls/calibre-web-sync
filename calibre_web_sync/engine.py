"""Verify uploaded EPUB bytes through the mounted server library."""
import time

from .library import lookup_epub, stable_digest


def verify_upload(remote_root, server_id, sha, *, digest_provider=None):
    """Freshly verify only the returned book, then seed an optional digest cache."""
    for attempt in range(6):
        book = lookup_epub(remote_root, server_id)
        if book is not None:
            result = stable_digest(book['path'])
            if result.sha256 == sha:
                if digest_provider is not None:
                    digest_provider.remember_verified(book, result)
                return
        if attempt < 5:
            time.sleep(2)
    raise RuntimeError(f'Upload returned book {server_id}, but its EPUB could not be verified through the shared folder. Stop and inspect before retrying.')
