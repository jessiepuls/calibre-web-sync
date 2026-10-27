"""Compare catalogs without modifying either library."""
import collections
from contextlib import nullcontext

from .cache import DigestProvider


def make_plan(local, remote, progress=None, *, digest_provider=None):
    """Compare freshly read catalogs; an injected provider remains caller-owned.

    Complete LibraryCatalog scans permit pruning after successful comparison.
    Plain book lists still work, but cannot establish that a scan was complete.
    Without a provider, only in-memory reuse is enabled for this comparison.
    """
    context = DigestProvider() if digest_provider is None else nullcontext(digest_provider)
    with context as provider:
        plan = _build_plan(local, remote, provider, progress)
        provider.prune_catalog(local)
        provider.prune_catalog(remote)
        return plan


def _build_plan(local, remote, provider, progress):
    sizes = collections.defaultdict(list)
    keys = collections.defaultdict(list)
    for book in remote:
        sizes[book['size']].append(book)
        keys[book['key']].append(book)
    seen = set()
    plan = []
    for number, book in enumerate(local, 1):
        if progress and (number == 1 or number % 50 == 0):
            progress(number, len(local))
        sha = provider.digest(book)
        book = dict(book, sha256=sha)
        identical = []
        for candidate in sizes[book['size']]:
            if provider.digest(candidate) == sha:
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
