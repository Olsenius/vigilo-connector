import asyncio
import time
from urllib.parse import urlencode

import httpx
import pytest

from vigilo_connector import config, auth
from vigilo_connector.http_app import Service, SetupError, create_app


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'CONFIG_FILE', tmp_path / 'config.json')
    monkeypatch.setattr(config, 'TOKEN_FILE', tmp_path / 'tokens.json')
    monkeypatch.delenv('VIGILO_CLIENT_ID', raising=False)
    monkeypatch.delenv('VIGILO_CLIENT_SECRET', raising=False)
    from vigilo_connector import server
    previous = server._client
    s = Service()
    yield s
    s.client.close()
    server._client = previous


def test_config_and_login(service, monkeypatch):
    service.configure({'values': {'VIGILO_CLIENT_ID': 'test-id', 'VIGILO_CLIENT_SECRET': 'test-secret'}})
    assert 'test-secret' not in str(service.document())
    monkeypatch.setattr(auth, 'exchange_code', lambda code: {'access_token': 'test-access', 'refresh_token': 'test-refresh', 'expires_in': 3600})
    monkeypatch.setattr(service.client, 'children', lambda: [])
    attempt = service.start()
    state = service.attempts[attempt['attempt_id']][0]
    body = dict(attempt_id=attempt['attempt_id'], input='app://test?' + urlencode(dict(state=state, code='test-code')))
    assert service.complete(body)['ok']
    with pytest.raises(ValueError):
        service.complete(body)
    assert service.tokens.load()['access_token'] == 'test-access'


@pytest.mark.parametrize('query', ['code=test', 'state=wrong&code=test', 'state=wrong&code=test&code=other'])
def test_login_rejects_invalid_state(service, monkeypatch, query):
    monkeypatch.setattr(auth, 'authorize_url', lambda state: 'https://login.example/authorize')
    attempt = service.start()
    with pytest.raises(ValueError):
        service.complete(dict(attempt_id=attempt['attempt_id'], input='app://test?' + query))
    assert not service.tokens.path.exists()


def test_config_atomic_and_locked(service, monkeypatch):
    service.configure({'values': {'VIGILO_CLIENT_ID': 'test-id'}})
    monkeypatch.setenv('VIGILO_CLIENT_SECRET', 'env-secret')
    with pytest.raises(ValueError):
        service.configure({'values': {'VIGILO_CLIENT_ID': 'changed', 'VIGILO_CLIENT_SECRET': 'bad'}})
    assert config.load_client_credentials() == ('test-id', 'env-secret')


def test_http_auth_health_and_discovery(service):
    async def run():
        app = create_app('internal-test-token', service)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as c:
            assert (await c.get('/healthz')).status_code == 200
            for path in ('/config', '/healthz?check=1', '/mcp'):
                assert (await c.get(path)).status_code == 401
            for path in ('/setup/start', '/setup/complete', '/setup/cancel'):
                assert (await c.post(path, json={})).status_code == 401
            headers = {'Authorization': 'Bearer internal-test-token'}
            assert (await c.get('/config', headers=headers)).json()['setup']['status'] == 'login_required'
    asyncio.run(run())


def test_mcp_http_catalogue_and_tool(service, monkeypatch):
    from contextlib import nullcontext
    monkeypatch.setattr(service.client, 'fresh_reads', lambda **kwargs: nullcontext())
    monkeypatch.setattr(service.client, 'get_children', lambda **kwargs: [{'childId': 'test-child'}])

    async def run():
        app = create_app('test-token', service)
        async with app.inner.router.lifespan_context(app.inner):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as c:
                headers = {'Authorization': 'Bearer test-token', 'Accept': 'application/json, text/event-stream'}
                async def rpc(method, params):
                    response = await asyncio.wait_for(c.post('/mcp', headers=headers, json=dict(jsonrpc='2.0', id=1, method=method, params=params)), 5)
                    assert response.status_code == 200, response.text
                    from tests.http_wire import _sse_json
                    return _sse_json(response)
                await rpc('initialize', dict(protocolVersion='2025-11-25', capabilities={}, clientInfo=dict(name='test', version='1')))
                result = await rpc('tools/list', {})
                from vigilo_connector import server
                assert {t['name'] for t in result['result']['tools']} == {f.__name__ for f in server.TOOLS}
                result = await rpc('tools/call', dict(name='list_children', arguments={}))
                assert 'test-child' in str(result)
    asyncio.run(run())


def test_new_login_cannot_be_overwritten_by_old_refresh(service, monkeypatch):
    import threading
    service.tokens.save(dict(access_token='expired', refresh_token='old-refresh', expires_in=0))
    entered, release = threading.Event(), threading.Event()
    errors = []
    def refresh(_):
        entered.set()
        assert release.wait(3)
        return dict(access_token='refreshed-old', refresh_token='rotated-old', expires_in=3600)
    monkeypatch.setattr(auth, 'refresh', refresh)
    monkeypatch.setattr(auth, 'authorize_url', lambda state: 'https://login.example/authorize')
    monkeypatch.setattr(auth, 'exchange_code', lambda code: dict(access_token='new-login', refresh_token='new-refresh', expires_in=3600))
    monkeypatch.setattr(service.client, 'children', lambda: [])
    attempt = service.start()
    state = service.attempts[attempt['attempt_id']][0]
    def capture(fn):
        try:
            fn()
        except BaseException as error:
            errors.append(error)
    first = threading.Thread(target=lambda: capture(service.tokens.access_token))
    second = threading.Thread(target=lambda: capture(lambda: service.complete(dict(attempt_id=attempt['attempt_id'], input='app://test?' + urlencode(dict(state=state, code='test-code'))))))
    first.start()
    assert entered.wait(3)
    second.start()
    release.set()
    first.join(3)
    second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert service.tokens.load()['access_token'] == 'new-login'


def test_expired_attempt_and_credential_change(service, monkeypatch):
    monkeypatch.setattr(auth, 'authorize_url', lambda state: 'https://login.example/authorize')
    attempt = service.start()
    service.attempts[attempt['attempt_id']] = ('state', time.time() - 1)
    with pytest.raises(ValueError):
        service.complete(dict(attempt_id=attempt['attempt_id'], input='app://test?state=state&code=test'))
    service.start()
    service.tokens.save(dict(access_token='test', refresh_token='test', expires_in=3600))
    service.configure({'values': {'VIGILO_CLIENT_ID': 'changed'}})
    assert not service.attempts
    assert not service.tokens.path.exists()


def test_two_tabs_and_idempotent_cancel(service, monkeypatch):
    monkeypatch.setattr(auth, 'authorize_url', lambda state: 'https://login.example/authorize')
    monkeypatch.setattr(auth, 'exchange_code', lambda code: dict(access_token=code, refresh_token='test', expires_in=3600))
    monkeypatch.setattr(service.client, 'children', lambda: [])
    first, second = service.start(), service.start()
    service.cancel({'attempt_id': first['attempt_id']})
    service.cancel({'attempt_id': first['attempt_id']})
    state = service.attempts[second['attempt_id']][0]
    result = service.complete(dict(attempt_id=second['attempt_id'], input='app://test?' + urlencode(dict(state=state, code='second-tab'))))
    assert result['ok']
    assert service.tokens.load()['access_token'] == 'second-tab'
    assert service.document()['setup']['status'] == 'connected'


def test_old_health_result_cannot_verify_changed_credentials(service, monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    def children():
        entered.set()
        assert release.wait(3)
        return []
    monkeypatch.setattr(service.client, 'children', children)
    results = []
    worker = threading.Thread(target=lambda: results.append(service.check()))
    worker.start()
    try:
        assert entered.wait(3)
        service.configure({'values': {'VIGILO_CLIENT_ID': 'new-client'}})
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert not results[0]['ok']
    assert service.document()['setup']['status'] == 'login_required'


@pytest.mark.parametrize('make_input, code', [
    (lambda state: '', 'paste_required'),
    (lambda state: 'https://auth.example.test/connect/authorize?' + urlencode(dict(client_id='test-client', response_type='code', scope='openid', state=state)), 'authorize_url_pasted'),
    (lambda state: 'app://test?' + urlencode(dict(error='access_denied', state=state)), 'provider_error'),
    (lambda state: 'app://test?' + urlencode(dict(state=state)), 'missing_code'),
    (lambda state: 'app://test?' + urlencode(dict(state='other-attempt', code='test-code')), 'state_mismatch'),
])
def test_login_errors_have_fixed_codes(service, monkeypatch, make_input, code):
    monkeypatch.setattr(auth, 'authorize_url', lambda state: 'https://login.example/authorize')
    monkeypatch.setattr(auth, 'exchange_code', lambda c: pytest.fail('must not exchange a rejected return URL'))
    attempt = service.start()
    state = service.attempts[attempt['attempt_id']][0]
    pasted = make_input(state)
    with pytest.raises(SetupError) as err:
        service.complete(dict(attempt_id=attempt['attempt_id'], input=pasted))
    assert err.value.code == code
    assert state not in str(err.value) and (not pasted or pasted not in str(err.value))
    assert not service.tokens.path.exists()


@pytest.mark.parametrize('failure, code', [
    (auth.AuthError('Token-kall avvist (400): invalid_grant'), 'exchange_failed'),
    (httpx.ConnectError('down'), 'service_unreachable'),
])
def test_login_exchange_failures(service, monkeypatch, failure, code):
    monkeypatch.setattr(auth, 'authorize_url', lambda state: 'https://login.example/authorize')
    def fail(c):
        raise failure
    monkeypatch.setattr(auth, 'exchange_code', fail)
    attempt = service.start()
    state = service.attempts[attempt['attempt_id']][0]
    with pytest.raises(SetupError) as err:
        service.complete(dict(attempt_id=attempt['attempt_id'], input='app://test?' + urlencode(dict(state=state, code='test-code'))))
    assert err.value.code == code and 'invalid_grant' not in str(err.value)
    with pytest.raises(SetupError) as again:
        service.complete(dict(attempt_id=attempt['attempt_id'], input='app://test?' + urlencode(dict(state=state, code='test-code'))))
    assert again.value.code == 'attempt_expired'


def test_http_setup_error_code(service, monkeypatch):
    monkeypatch.setattr(auth, 'authorize_url', lambda state: 'https://login.example/authorize')
    async def run():
        app = create_app('internal-test-token', service)
        headers = {'Authorization': 'Bearer internal-test-token'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as c:
            attempt = (await c.post('/setup/start', headers=headers, json={})).json()
            pasted = 'https://auth.example.test/connect/authorize?response_type=code&state=x'
            r = await c.post('/setup/complete', headers=headers, json=dict(attempt_id=attempt['attempt_id'], input=pasted))
            assert r.status_code == 400
            assert r.json()['code'] == 'authorize_url_pasted' and pasted not in r.text
    asyncio.run(run())
