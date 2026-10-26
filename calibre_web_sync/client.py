"""Calibre-Web HTTP login and upload adapter."""
import http.cookiejar
from html.parser import HTMLParser
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid


class Fields(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.has_password = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input':
            if attrs.get('type') == 'password':
                self.has_password = True
            if attrs.get('name') == 'csrf_token':
                self.values['csrf_token'] = attrs.get('value', '')


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin):
        self.origin = origin

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if (target.scheme, target.netloc) != self.origin:
            raise RuntimeError('Refusing redirect to another host')
        return super().redirect_request(request, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, base):
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError('URL must be an http(s) base URL without credentials, query, or fragment')
        self.base = base.rstrip('/') + '/'
        self.origin = (parsed.scheme, parsed.netloc)
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), SameOriginRedirect(self.origin))
        self.csrf = ''

    def request(self, route, data=None, content_type=None):
        url = urllib.parse.urljoin(self.base, route)
        if (urllib.parse.urlsplit(url).scheme, urllib.parse.urlsplit(url).netloc) != self.origin:
            raise RuntimeError('Refusing cross-origin request')
        headers = {'User-Agent': 'CalibreLibrarySync/1.0', 'Referer': self.base}
        if content_type:
            headers['Content-Type'] = content_type
        if self.csrf:
            headers['X-CSRFToken'] = self.csrf
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=180) as response:
                final = urllib.parse.urlsplit(response.url)
                if (final.scheme, final.netloc) != self.origin:
                    raise RuntimeError('Unexpected redirect to another host')
                return response.read(), response.url
        except urllib.error.HTTPError as error:
            # Do not print response bodies that could include submitted credentials.
            code = error.code
            error.close()
            if code == 403 and route == 'upload':
                raise RuntimeError('Upload forbidden (HTTP 403). In Calibre-Web, enable uploading in Basic Configuration and grant this user Upload permission under Admin → Users. If both are enabled, check the reverse proxy access rules.') from None
            raise RuntimeError(f'Calibre-Web returned HTTP {code} for {route}') from None

    def login(self, username, password):
        html, _ = self.request('login')
        fields = Fields(html.decode('utf-8', 'replace'))
        payload = dict(fields.values, username=username, password=password)
        html, _ = self.request('login', urllib.parse.urlencode(payload).encode(), 'application/x-www-form-urlencoded')
        page = Fields(html.decode('utf-8', 'replace'))
        if page.has_password:
            raise RuntimeError('Login did not succeed. Check URL, credentials, and authentication type.')
        self.csrf = page.values.get('csrf_token') or fields.values.get('csrf_token', '')
        # Confirm login instead of treating HTTP 200 as success.
        _, url = self.request('me')
        if urllib.parse.urlsplit(url).path.rstrip('/').endswith('/login'):
            raise RuntimeError('Login session was not established')

    def upload(self, path):
        boundary = 'calibresync' + uuid.uuid4().hex
        body = bytearray()
        if self.csrf:
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\n{self.csrf}\r\n'.encode())
        # A generated ASCII filename avoids multipart filename encoding problems.
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="btn-upload"; filename="book.epub"\r\nContent-Type: application/epub+zip\r\n\r\n'.encode())
        body.extend(path.read_bytes())
        body.extend(f'\r\n--{boundary}--\r\n'.encode())
        response, _ = self.request('upload', bytes(body), f'multipart/form-data; boundary={boundary}')
        try:
            result = json.loads(response)
            location = result.get('location', '')
            match = re.search(r'/(?:admin/book|book)/(\d+)(?:[/?#]|$)', location)
            if not match:
                raise ValueError()
            return int(match.group(1))
        except (ValueError, AttributeError, TypeError):
            raise RuntimeError('Upload response was not a recognized success. Stopping; inspect the server before retrying.') from None
