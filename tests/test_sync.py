import contextlib
from email.parser import BytesParser
from email.policy import default
import io
import json
import subprocess
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import urllib.error
import urllib.parse
import urllib.request

from calibre_web_sync import cli as sync, client, credentials, library as library_module, planner


def library(root, entries):
    root.mkdir(exist_ok=True)
    c = sqlite3.connect(root / 'metadata.db')
    c.executescript('''CREATE TABLE books(id INTEGER PRIMARY KEY,title TEXT,path TEXT);
        CREATE TABLE authors(id INTEGER PRIMARY KEY,name TEXT);
        CREATE TABLE books_authors_link(id INTEGER PRIMARY KEY,book INTEGER,author INTEGER);
        CREATE TABLE data(book INTEGER,format TEXT,name TEXT);''')
    c.execute("INSERT INTO authors VALUES(1,'Test Author')")
    for bid, title, content in entries:
        add(c, root, bid, title, content)
    c.commit()
    c.close()


def add(c, root, bid, title, content):
    folder = root / str(bid)
    folder.mkdir(exist_ok=True)
    (folder / 'book.epub').write_bytes(content)
    c.execute('INSERT INTO books VALUES(?,?,?)', (bid, title, str(bid)))
    c.execute('INSERT INTO data VALUES(?,?,?)', (bid, 'EPUB', 'book'))
    c.execute('INSERT INTO books_authors_link(book,author) VALUES(?,1)', (bid,))


class SyncTests(unittest.TestCase):
    def test_content_identity_and_edition_review(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            library(root/'local', [(1,'Renamed edition',b'identical'),(2,'Same title',b'different'),(3,'New book',b'new'),(4,'Duplicate file',b'new')])
            library(root/'remote', [(10,'Original title',b'identical'),(11,'Same title',b'original')])
            plan = planner.make_plan(library_module.catalog(root/'local'), library_module.catalog(root/'remote'))
            self.assertEqual([p['status'] for p in plan], ['present','review','missing','duplicate-local'])

    def test_journal_not_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            library(root, [])
            (root/'metadata.db-wal').touch()
            with self.assertRaisesRegex(RuntimeError,'WAL/journal'):
                library_module.catalog(root)

    def test_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            library(root, [(1,'Bad path',b'bytes')])
            with contextlib.closing(sqlite3.connect(root/'metadata.db')) as c:
                c.execute("UPDATE books SET path='../outside'")
                c.commit()
            with self.assertRaisesRegex(RuntimeError,'escapes'):
                library_module.catalog(root)

    def test_upload_login_csrf_and_repeat_run(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            library(root/'local', [(1,'New book',b'EPUB payload')])
            library(root/'remote', [])
            uploads = []
            class Response:
                def __init__(self, body, url):self.body,self.url=body,url
                def read(self):return self.body
                def __enter__(self):return self
                def __exit__(self,*args):pass
            class Transport:
                authenticated=False
                def open(self, request, timeout):
                    path=urllib.parse.urlsplit(request.full_url).path
                    if request.data is None:
                        if path=='/cw/login':return Response(b'<input name="csrf_token" value="token"><input type="password">',request.full_url)
                        if not self.authenticated:return Response(b'<input type="password">',request.full_url.replace('/me','/login'))
                        return Response(b'<input name="csrf_token" value="token">',request.full_url)
                    body=request.data
                    if path=='/cw/login':
                        if b'password=secret' in body and b'csrf_token=token' in body:
                            self.authenticated=True
                            return Response(b'<input name="csrf_token" value="token">',request.full_url.replace('/login','/'))
                        raise urllib.error.HTTPError(request.full_url,401,'Unauthorized',{},None)
                    if path=='/cw/upload':
                        if not self.authenticated or request.get_header('X-csrftoken')!='token':
                            raise urllib.error.HTTPError(request.full_url,403,'Forbidden',{},None)
                        msg=BytesParser(policy=default).parsebytes(b'Content-Type: '+request.get_header('Content-type').encode()+b'\r\n\r\n'+body)
                        payload=next(p.get_payload(decode=True) for p in msg.iter_parts() if p.get_param('name',header='Content-Disposition')=='btn-upload')
                        uploads.append(payload)
                        with contextlib.closing(sqlite3.connect(root/'remote/metadata.db')) as c:
                            add(c,root/'remote',10,'New book',payload)
                            c.commit()
                        return Response(b'{"location":"/cw/admin/book/10"}',request.full_url)
                    raise AssertionError(path)
            (root/'config.json').write_text('{}')
            args=['sync.py','--config',str(root/'config.json'),'--local',str(root/'local'),'--remote',str(root/'remote'),'--apply','--url','http://test.invalid/cw','--username','user']
            with patch.object(urllib.request,'build_opener',return_value=Transport()):
                with patch('sys.argv',args),patch.object(sync.getpass,'getpass',return_value='secret'),contextlib.redirect_stdout(io.StringIO()):sync.main()
                self.assertEqual(uploads,[b'EPUB payload'])
                self.assertEqual(planner.make_plan(library_module.catalog(root/'local'),library_module.catalog(root/'remote'))[0]['status'],'present')
                with patch('sys.argv',args),patch.object(sync.getpass,'getpass',side_effect=AssertionError('Should not log in')),contextlib.redirect_stdout(io.StringIO()):sync.main()
                http_client=client.Client('http://test.invalid/cw')
                with self.assertRaisesRegex(RuntimeError,'401'):http_client.login('user','wrong')

    def test_onepassword_primary_url_and_standard_fields(self):
        entry={'fields':[{'id':'username','value':'example-user'}, {'id':'password','value':'private-test-value'}],
               'urls':[{'href':'https://other.example'}, {'href':'https://books.example/cw/login','primary':True}]}
        result=subprocess.CompletedProcess([],0,json.dumps(entry),'')
        with patch.object(credentials.subprocess,'run',return_value=result) as run:
            self.assertEqual(credentials.onepassword_credentials('item-id','vault-id','account-id'),
                             ('https://books.example/cw','example-user','private-test-value'))
        command=run.call_args.args[0]
        self.assertEqual(command,['op','item','get','item-id','--format=json','--reveal','--vault','vault-id','--account','account-id'])
        self.assertNotIn('private-test-value',command)
        self.assertIs(run.call_args.kwargs['stdout'],subprocess.PIPE)
        self.assertIs(run.call_args.kwargs['stderr'],subprocess.PIPE)

    def test_onepassword_errors_do_not_expose_output(self):
        result=subprocess.CompletedProcess([],1,'secret-output','secret-error')
        with patch.object(credentials.subprocess,'run',return_value=result):
            with self.assertRaises(RuntimeError) as error:credentials.onepassword_credentials('item')
        self.assertNotIn('secret',str(error.exception))
        with patch.object(credentials.subprocess,'run',side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError,'not installed'):credentials.onepassword_credentials('item')

    def test_onepassword_rejects_ambiguous_urls(self):
        entry={'fields':[{'id':'username','value':'user'},{'id':'password','value':'secret'}],
               'urls':[{'href':'https://one.example'},{'href':'https://two.example'}]}
        result=subprocess.CompletedProcess([],0,json.dumps(entry),'')
        with patch.object(credentials.subprocess,'run',return_value=result):
            with self.assertRaisesRegex(RuntimeError,'primary URL'):credentials.onepassword_credentials('item')

    def test_onepassword_custom_labels_and_purposes(self):
        entry={'fields':[{'purpose':'USERNAME','label':'Renamed','value':'user'},
                         {'purpose':'PASSWORD','label':'Renamed too','value':'secret'},
                         {'label':'website','value':'https://books.example/login'}]}
        result=subprocess.CompletedProcess([],0,json.dumps(entry),'')
        with patch.object(credentials.subprocess,'run',return_value=result):
            self.assertEqual(credentials.onepassword_credentials('item'),('https://books.example','user','secret'))

    def test_onepassword_missing_field_diagnostic_is_redacted(self):
        entry={'fields':[{'id':'password','value':'private-password'}]}
        result=subprocess.CompletedProcess([],0,json.dumps(entry),'')
        with patch.object(credentials.subprocess,'run',return_value=result):
            with self.assertRaisesRegex(RuntimeError,'no populated username') as error:
                credentials.onepassword_credentials('item')
        self.assertNotIn('private-password',str(error.exception))

    def test_onepassword_explicit_overrides_bypass_missing_url_and_username(self):
        entry={'fields':[{'id':'password','value':'secret'}]}
        result=subprocess.CompletedProcess([],0,json.dumps(entry),'')
        with patch.object(credentials.subprocess,'run',return_value=result):
            self.assertEqual(credentials.onepassword_credentials('item',url_override='https://books.example',username_override='user'),
                             ('https://books.example','user','secret'))

    def test_check_login_does_not_scan_or_upload(self):
        args=['sync.py','--check-login','--op-item','item']
        with patch('sys.argv',args),patch.object(sync,'authenticated_client') as login,patch.object(sync,'catalog',side_effect=AssertionError('Must not scan')),contextlib.redirect_stdout(io.StringIO()):
            sync.main()
        login.assert_called_once()

    def test_upload_forbidden_explains_permissions_without_response_body(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'book.epub';path.write_bytes(b'epub')
            http_client=client.Client('https://books.example')
            error=urllib.error.HTTPError('https://books.example/upload',403,'Forbidden',{},io.BytesIO(b'private-response'))
            with patch.object(http_client.opener,'open',side_effect=error):
                with self.assertRaisesRegex(RuntimeError,'Upload permission') as caught:
                    http_client.upload(path)
            self.assertNotIn('private-response',str(caught.exception))

    def test_config_defaults_and_cli_override(self):
        with tempfile.TemporaryDirectory() as d:
            config=Path(d)/'config.json'
            config.write_text(json.dumps({'local':'/example/local','remote':'/example/remote','op_item':'Saved item','op_vault':'Saved vault'}))
            args=['sync.py','--config',str(config),'--check-login','--op-item','Override item']
            with patch('sys.argv',args),patch.object(sync,'authenticated_client') as login,contextlib.redirect_stdout(io.StringIO()):
                sync.main()
            parsed=login.call_args.args[0]
            self.assertEqual(parsed.op_item,'Override item')
            self.assertEqual(parsed.op_vault,'Saved vault')
            self.assertEqual(parsed.local,Path('/example/local'))
            self.assertEqual(parsed.remote,Path('/example/remote'))

    def test_cross_host_redirect_rejected(self):
        handler = client.SameOriginRedirect(('https','books.example'))
        with self.assertRaisesRegex(RuntimeError,'another host'):
            handler.redirect_request(None,None,302,'',{},'https://other.example/login')


if __name__ == '__main__':unittest.main()
