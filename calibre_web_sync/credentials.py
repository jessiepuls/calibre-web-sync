"""Retrieve credentials from 1Password without persisting secrets."""
import json
import subprocess
import urllib.parse


def onepassword_credentials(item, vault=None, account=None, url_override=None, username_override=None):
    """Read exactly one Login item; capture secret output only in memory."""
    command = ['op', 'item', 'get', item, '--format=json', '--reveal']
    if vault:
        command += ['--vault', vault]
    if account:
        command += ['--account', account]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=120, check=False)
    except FileNotFoundError:
        raise RuntimeError('1Password CLI (op) is not installed or is not on PATH') from None
    except subprocess.TimeoutExpired:
        raise RuntimeError('1Password authorization timed out; unlock the desktop app and try again') from None
    if result.returncode:
        # Neither stderr nor stdout is printed: either could contain item data.
        raise RuntimeError('Unable to read the 1Password item. Check the item/vault, unlock 1Password, and enable CLI integration.')
    try:
        entry = json.loads(result.stdout)
        if not isinstance(entry, dict):
            raise ValueError()
        fields = entry.get('fields') or []
        if not isinstance(fields, list):
            raise ValueError()
        fields = [f for f in fields if isinstance(f, dict)]
    except (ValueError, TypeError):
        raise RuntimeError('1Password returned an unexpected item format; no item contents were printed') from None

    def value(name, aliases):
        # Standard Login fields have a purpose, even if their label was renamed.
        populated = [f for f in fields if isinstance(f.get('value'), str) and f['value'].strip()]
        for matches in (
            [f for f in populated if str(f.get('purpose', '')).casefold() == name],
            [f for f in populated if str(f.get('id', '')).casefold() == name],
            [f for f in populated if str(f.get('label', '')).strip().casefold() in aliases],
        ):
            values = list(dict.fromkeys(f['value'] for f in matches))
            if len(values) == 1:
                return values[0]
            if len(values) > 1:
                raise RuntimeError(f'1Password item has multiple matching {name} fields; use a standard Login item with one {name}')
        raise RuntimeError(f'1Password item has no populated {name} field; fill in the {name} on the Login item')

    username = username_override or value('username', {'username', 'user name', 'user', 'email', 'email address'})
    password = value('password', {'password', 'pass'})
    if url_override:
        url = url_override
    else:
        urls = [u for u in (entry.get('urls') or []) if isinstance(u, dict) and isinstance(u.get('href'), str) and u['href'].strip()]
        primary = [u['href'] for u in urls if u.get('primary')]
        candidates = list(dict.fromkeys(primary or [u['href'] for u in urls]))
        if not candidates:
            url = value('url', {'url', 'website', 'web site', 'website url', 'web address'})
        elif len(candidates) == 1:
            url = candidates[0]
        else:
            raise RuntimeError(f'1Password item has {len(candidates)} website URLs and no single primary URL; specify the Calibre-Web base URL with --url')
    url = url.strip().rstrip('/')
    # Login items often save the login page instead of the application root.
    parsed = urllib.parse.urlsplit(url)
    if parsed.path.endswith('/login') and not parsed.query and not parsed.fragment:
        url = urllib.parse.urlunsplit(parsed._replace(path=parsed.path[:-6]))
    return url, username, password
