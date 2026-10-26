"""Transfer verification shared by command-line and future plugin interfaces."""
import time

from .library import catalog, digest


def verify_upload(remote_root, server_id, sha):
    for attempt in range(6):
        for book in catalog(remote_root):
            if book['id'] == server_id and digest(book['path']) == sha:
                return
        time.sleep(2)
    raise RuntimeError(f'Upload returned book {server_id}, but its EPUB could not be verified through the shared folder. Stop and inspect before retrying.')
