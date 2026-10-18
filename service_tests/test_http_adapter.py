"""Actual ASGI and loopback TCP acceptance; optional HTTP dependencies required."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from urllib.parse import quote
from urllib.request import build_opener, ProxyHandler, Request
from urllib.error import HTTPError
from unittest.mock import patch
from agent_forge_service.app import create_app
from agent_forge_service.publishing import digest_file
from service_tests import test_publication_query as fixture

AVAILABLE=all(importlib.util.find_spec(name) is not None for name in ('fastapi','uvicorn','httpx'))
REASON='Install the reviewed http extra and http-test group before real HTTP acceptance'


@unittest.skipUnless(AVAILABLE,REASON)
class HttpAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.case=fixture.PublicationQueryTests('runTest');self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.identity='plugin/数据库/releases'
        for version in ('1.0.0','2.0.0'):
            self.case.write(fixture.record(identity=self.identity,version=version))
        self.case.index({'alpha':{'latest':'2.0.0'}});self.case.build()

    def client(self,origins=(),root_path=''):
        import httpx
        app=create_app(self.case.settings,allowed_origins=origins)
        transport=httpx.ASGITransport(app=app,root_path=root_path,raise_app_exceptions=False)
        return app,httpx.AsyncClient(transport=transport,base_url='http://test')

    async def test_real_asgi_routes_exact_versions_and_conditional_requests(self):
        _,client=self.client()
        async with client:
            self.assertEqual(200,(await client.get('/health/live')).status_code)
            self.assertEqual(200,(await client.get('/health/ready')).status_code)
            response=await client.get('/api/v1/packages')
            self.assertEqual(200,response.status_code);self.assertEqual(self.identity,response.json()['items'][0]['id'])
            cached=await client.get('/api/v1/packages',headers={'If-None-Match':response.headers['etag']})
            self.assertEqual(304,cached.status_code);self.assertEqual(b'',cached.content)
            path='/api/v1/packages/'+quote(self.identity,safe='')+'/detail'
            old=await client.get(path,params={'revision':'one','version':'1.0.0'})
            self.assertEqual(200,old.status_code);self.assertEqual('1.0.0',old.json()['canonicalRecord']['version'])
            missing=await client.get(path,params={'version':'missing'})
            self.assertEqual(404,missing.status_code);self.assertEqual('version_not_found',missing.json()['error']['code'])

    async def test_cors_exact_origin_preflight_and_no_credentials(self):
        _,client=self.client(('https://catalog.example.test',))
        async with client:
            allowed=await client.get('/api/v1/manifest',headers={'Origin':'https://catalog.example.test'})
            self.assertEqual('https://catalog.example.test',allowed.headers['access-control-allow-origin'])
            self.assertNotIn('access-control-allow-credentials',allowed.headers)
            denied=await client.get('/api/v1/manifest',headers={'Origin':'https://other.example.test'})
            self.assertNotIn('access-control-allow-origin',denied.headers)
            response=await client.options('/api/v1/packages',headers={'Origin':'https://catalog.example.test','Access-Control-Request-Method':'GET','Access-Control-Request-Headers':'If-None-Match'})
            self.assertEqual(200,response.status_code)
            denied=await client.options('/api/v1/packages',headers={'Origin':'https://catalog.example.test','Access-Control-Request-Method':'POST'})
            self.assertEqual(400,denied.status_code)

    async def test_proxy_prefix_unicode_suffix_identity_and_explicit_capabilities(self):
        _,client=self.client(root_path='/project')
        async with client:
            response=await client.get('/project/api/v1/packages/'+quote(self.identity,safe='')+'/detail',params={'version':'1.0.0'})
            self.assertEqual(200,response.status_code,response.text);self.assertEqual(self.identity,response.json()['id'])
            self.assertEqual(400,(await client.get('/project/api/v1/packages',params={'sort':'latest_release'})).status_code)
            self.assertEqual(503,(await client.get('/project/api/v1/releases')).status_code)
            self.assertEqual(503,(await client.get('/project/api/v1/source-controls')).status_code)

    async def test_methods_parameters_and_unknown_paths_have_bounded_json_errors(self):
        _,client=self.client()
        async with client:
            for method in ('POST','PUT','DELETE'):
                response=await client.request(method,'/api/v1/packages')
                self.assertEqual(405,response.status_code);self.assertEqual('method_not_allowed',response.json()['error']['code'])
            duplicate=await client.get('/api/v1/packages?limit=1&limit=2')
            self.assertEqual(400,duplicate.status_code)
            self.assertEqual(414,(await client.get('/api/v1/packages?q='+'a'*17000)).status_code)
            self.assertEqual(404,(await client.get('/does-not-exist')).status_code)
            self.assertEqual(404,(await client.get('/openapi.json')).status_code)

    async def test_unexpected_errors_are_redacted_and_private_files_unchanged(self):
        app,client=self.client()
        state=self.case.settings.state_path;before=digest_file(state)
        async with client:
            with patch.object(app.state.reader,'handle',side_effect=RuntimeError('private-secret-fixture')):
                response=await client.get('/api/v1/packages')
            self.assertEqual(500,response.status_code);self.assertEqual('internal_error',response.json()['error']['code'])
            self.assertNotIn('private-secret-fixture',response.text)
        self.assertEqual(before,digest_file(state))


@unittest.skipUnless(AVAILABLE,REASON)
class HttpTcpTests(unittest.TestCase):
    def test_uvicorn_loopback_restart_revision_switch_and_encoded_identity(self):
        case=fixture.PublicationQueryTests('runTest');case.setUp();self.addCleanup(case.doCleanups)
        identity='plugin/数据库/releases'
        case.write(fixture.record(identity=identity));case.build('one')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        base='http://127.0.0.1:'+str(port)
        opener=build_opener(ProxyHandler({}))
        def get(path):
            with opener.open(base+path,timeout=3) as response:return response.status,json.load(response)
        for attempt in range(2):
            log=(case.root/('http-'+str(attempt)+'.log')).open('wb')
            command=[sys.executable,'-m','agent_forge_service.app','--runtime',str(case.settings.runtime_root),'--catalog',str(case.settings.catalog_root),'--port',str(port)]
            process=subprocess.Popen(command,cwd=Path(__file__).resolve().parents[1],stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                deadline=time.monotonic()+12
                while True:
                    try:
                        if get('/health/ready')[0]==200:break
                    except (OSError,HTTPError):pass
                    if process.poll() is not None or time.monotonic()>deadline:
                        log.flush();self.fail('Uvicorn failed to become ready: '+(case.root/('http-'+str(attempt)+'.log')).read_text(errors='replace'))
                    time.sleep(.05)
                response=get('/api/v1/packages/'+quote(identity,safe='')+'/detail')[1]
                self.assertEqual(identity,response['id'])
                if attempt==0:
                    case.build('two',now=fixture.NOW+fixture.timedelta(hours=1))
                self.assertEqual('two',get('/api/v1/manifest')[1]['revision'])
                self.assertEqual('one',get('/api/v1/manifest?revision=one')[1]['revision'])
            finally:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
                log.close()
