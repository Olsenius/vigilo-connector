import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio
import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from vigilo_connector import telemetry
from vigilo_connector.http_logging import LoggedClient


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry.trace, 'get_tracer', lambda name: provider.get_tracer(name))
    yield exporter
    provider.shutdown()


def test_request_tool_http_parentage_and_privacy(spans):
    private = 'PRIVATE_PERSON_TOKEN_SIGNATURE'
    def send(request):
        assert 'traceparent' not in request.headers and 'baggage' not in request.headers
        return httpx.Response(200, json={'childId': private})
    @telemetry.tool
    def list_children():
        with LoggedClient(origin='app', transport=httpx.MockTransport(send)) as client:
            client.get('https://api.example/api/students/' + private + '/lessons?sig=' + private,
                       headers={'Authorization': 'Bearer '+private})
    async def app(scope, receive, send):
        await anyio.to_thread.run_sync(list_children)
        await send({'type':'http.response.start', 'status':200, 'headers':[]})
        await send({'type':'http.response.body', 'body':private.encode()})
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=telemetry.TraceHTTP(app)), base_url='http://backend.example') as client:
            await client.post('/mcp?childId='+private, content=private, headers={
                'traceparent':'00-0123456789abcdef0123456789abcdef-0123456789abcdef-01',
                'baggage':'private='+private,
            })
    asyncio.run(run())
    finished = {s.name:s for s in spans.get_finished_spans()}
    assert len(finished)==3
    request, tool, upstream = (finished[n] for n in ['POST /mcp','tool list_children','vigilo GET'])
    assert request.parent.span_id == int('0123456789abcdef',16)
    assert tool.parent.span_id == request.context.span_id
    assert upstream.parent.span_id == tool.context.span_id
    assert all(s.context.trace_id==int('0123456789abcdef0123456789abcdef',16) for s in finished.values())
    assert upstream.attributes['http.route']=='/api/students/{childId}/lessons'
    assert private not in str([(s.name,dict(s.attributes),s.events,s.status.description) for s in finished.values()])


def test_errors_do_not_record_exception_payload(spans):
    with pytest.raises(ValueError):
        with telemetry.span('test'):
            raise ValueError('SECRET_TOKEN_AND_PRIVATE_URL')
    result=spans.get_finished_spans()[0]
    assert result.status.status_code==StatusCode.ERROR
    assert not result.events
    assert 'SECRET' not in str((result.attributes,result.status.description))


def test_liveness_is_not_traced(spans):
    async def app(scope,receive,send):
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'ok'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=telemetry.TraceHTTP(app)),base_url='http://test') as c:
            await c.get('/healthz')
    asyncio.run(run())
    assert not spans.get_finished_spans()


def test_otlp_export_resource_and_shutdown(monkeypatch):
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    requests=[]
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path,self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200);self.end_headers()
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Receiver)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_ENDPOINT',f'http://127.0.0.1:{server.server_port}')
    monkeypatch.setenv('OTEL_SERVICE_NAME','vigilo-test-service')
    monkeypatch.setenv('OTEL_TRACES_SAMPLER','always_on')
    monkeypatch.delenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT',raising=False)
    monkeypatch.delenv('OTEL_SDK_DISABLED',raising=False)
    providers=[]
    monkeypatch.setattr(telemetry.trace,'set_tracer_provider',providers.append)
    try:
        shutdown=telemetry.configure()
        with providers[0].get_tracer('vigilo-mcp').start_as_current_span('export-check'):pass
        shutdown()
        assert len(requests)==1 and requests[0][0]=='/v1/traces'
        message=ExportTraceServiceRequest();message.ParseFromString(requests[0][1])
        resource=message.resource_spans[0]
        attrs={a.key:a.value.string_value for a in resource.resource.attributes}
        assert attrs['service.name']=='vigilo-test-service'
        assert resource.scope_spans[0].spans[0].name=='export-check'
    finally:
        server.shutdown();server.server_close();thread.join(2)


@pytest.mark.parametrize('values',[{'OTEL_SDK_DISABLED':'true'},{'OTEL_TRACES_EXPORTER':'none'}])
def test_disabled_export(monkeypatch,values):
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_ENDPOINT','http://127.0.0.1:1')
    for key,value in values.items():monkeypatch.setenv(key,value)
    monkeypatch.setattr(telemetry.trace,'set_tracer_provider',lambda provider: pytest.fail('unexpected provider'))
    telemetry.configure()()


def test_real_mcp_tool_continues_incoming_trace(spans, monkeypatch, tmp_path):
    from vigilo_connector import config, server
    from vigilo_connector.http_app import Service, create_app
    from tests.http_wire import _sse_json
    monkeypatch.setattr(config,'TOKEN_FILE',tmp_path/'tokens.json')
    service=Service()
    previous=server._client
    def children(**kwargs):
        with LoggedClient(origin='app',transport=httpx.MockTransport(lambda r: httpx.Response(200,json=[]))) as c:
            return c.get('https://api.example/api/children').json()
    monkeypatch.setattr(service.client,'get_children',children)
    async def run():
        app=create_app('test-token',service)
        async with app.inner.router.lifespan_context(app.inner):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as c:
                r=await c.post('/mcp',headers={'Authorization':'Bearer test-token','Accept':'application/json, text/event-stream',
                    'traceparent':'00-0123456789abcdef0123456789abcdef-0123456789abcdef-01'},json={
                    'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'list_children','arguments':{}}})
                assert r.status_code==200
                assert not _sse_json(r)['result'].get('isError')
    try:asyncio.run(run())
    finally:server._client=previous
    finished={s.name:s for s in spans.get_finished_spans()}
    by_id={s.context.span_id:s for s in spans.get_finished_spans()}
    parent=finished['tool list_children'].parent
    while parent.span_id != finished['POST /mcp'].context.span_id:
        parent=by_id[parent.span_id].parent
    assert parent.trace_id == finished['POST /mcp'].context.trace_id
    assert finished['vigilo GET'].parent.span_id==finished['tool list_children'].context.span_id


def test_sdk_span_payload_is_removed_before_export():
    exporter=InMemorySpanExporter()
    provider=TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(telemetry.PrivateExporter(exporter)))
    with provider.get_tracer('mcp.server').start_as_current_span('tools/call PRIVATE') as current:
        current.set_attribute('jsonrpc.request.id','PRIVATE')
        current.record_exception(ValueError('PRIVATE'))
        current.set_status(telemetry.Status(StatusCode.ERROR,'PRIVATE'))
    result=exporter.get_finished_spans()[0]
    assert result.name=='MCP request'
    assert not result.events and not result.attributes
    assert result.status.description is None
    provider.shutdown()
