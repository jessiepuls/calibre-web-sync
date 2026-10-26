"""Compare catalogs without modifying either library."""
import collections

from .library import digest


def make_plan(local, remote, progress=None):
    sizes = collections.defaultdict(list)
    keys = collections.defaultdict(list)
    for book in remote:
        sizes[book['size']].append(book)
        keys[book['key']].append(book)
    hash_cache = {}
    seen = set()
    plan = []
    for number, book in enumerate(local, 1):
        if progress and (number == 1 or number % 50 == 0):
            progress(number, len(local))
        sha = digest(book['path'])
        book = dict(book, sha256=sha)
        identical = []
        for candidate in sizes[book['size']]:
            path = candidate['path']
            if path not in hash_cache:
                hash_cache[path] = digest(path)
            if hash_cache[path] == sha:
                identical.append(candidate['id'])
        if identical:
            status, reason = 'present', f'Identical EPUB on server: {identical}'
        elif sha in seen:
            status, reason = 'duplicate-local', 'Identical to another local EPUB in this run'
        elif book['key'] in keys:
            status, reason = 'review', 'Same title and author, different EPUB; may be a different edition'
        else:
            status, reason = 'missing', 'No identical EPUB or matching title/author on server'
        seen.add(sha)
        plan.append(dict(book, status=status, reason=reason,
                         server_ids=[b['id'] for b in keys.get(book['key'], [])]))
    return plan
