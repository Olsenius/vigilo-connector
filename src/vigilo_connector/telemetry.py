"""Opt-in OTLP/HTTP tracing with a strict metadata-only attribute policy."""
import functools
import os
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


def configure():
    """Configure once at process startup; return a graceful shutdown callback."""
    disabled = os.environ.get('OTEL_SDK_DISABLED', 'false').lower()
    if disabled not in {'true', 'false'}:
        raise ValueError('OTEL_SDK_DISABLED must be true or false')
    exporter = os.environ.get('OTEL_TRACES_EXPORTER', 'otlp')
    endpoint = os.environ.get('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT') or os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT')
    if disabled == 'true' or exporter == 'none' or not endpoint:
        return lambda: None
    if exporter != 'otlp':
        raise ValueError('OTEL_TRACES_EXPORTER must be otlp or none')
    protocol = os.environ.get('OTEL_EXPORTER_OTLP_TRACES_PROTOCOL', os.environ.get('OTEL_EXPORTER_OTLP_PROTOCOL', 'http/protobuf'))
    if protocol != 'http/protobuf':
        raise ValueError('Only OTLP HTTP/protobuf is supported')
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # Resource.create reads standard env attributes and service name. Defaults
    # must not override deployment-specific OTEL_SERVICE_NAME/resource settings.
    attributes = dict(Resource.create().attributes)
    if not os.environ.get('OTEL_SERVICE_NAME') and not any(
        item.strip().startswith('service.name=')
        for item in os.environ.get('OTEL_RESOURCE_ATTRIBUTES', '').split(',')
    ):
        attributes['service.name'] = 'vigilo-mcp'
    attributes.setdefault('service.version', '0.2.0')
    resource = Resource(attributes)
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(PrivateExporter(OTLPSpanExporter())))
    trace.set_tracer_provider(provider)
    return provider.shutdown


@contextmanager
def span(name, *, kind=SpanKind.INTERNAL, attributes=None, context=None):
    with trace.get_tracer('vigilo-mcp').start_as_current_span(
        name, kind=kind, attributes=attributes, context=context,
        record_exception=False, set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except BaseException as error:
            current.set_attribute('error.type', type(error).__name__)
            current.set_status(Status(StatusCode.ERROR, 'Operation failed'))
            raise


def tool(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with span('tool ' + fn.__name__, attributes={'mcp.tool.name': fn.__name__}):
            return fn(*args, **kwargs)
    return wrapped


class TraceHTTP:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['path'] == '/healthz' and not scope.get('query_string'):
            return await self.app(scope, receive, send)
        path = scope['path']
        route = path if path in {'/mcp', '/config', '/healthz', '/setup/start', '/setup/complete', '/setup/cancel'} else '/{redacted}'
        method = scope.get('method', 'OTHER')
        if method not in {'GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS'}:
            method = 'OTHER'
        # Extract W3C trace context only; baggage and other headers are ignored.
        headers = {k.decode('latin1'): v.decode('latin1') for k, v in scope.get('headers', []) if k in {b'traceparent', b'tracestate'}}
        parent = TraceContextTextMapPropagator().extract(headers)
        with span(method + ' ' + route, kind=SpanKind.SERVER, context=parent,
                  attributes={'http.request.method': method, 'http.route': route}) as current:
            async def traced_send(message):
                if message['type'] == 'http.response.start':
                    status = message['status']
                    current.set_attribute('http.response.status_code', status)
                    if status >= 500:
                        current.set_status(Status(StatusCode.ERROR, 'HTTP request failed'))
                await send(message)
            await self.app(scope, receive, traced_send)


class PrivateExporter:
    """Strip SDK-generated request IDs, arbitrary tool targets and exceptions.

    MCP's own instrumentation may include caller-controlled names/messages.
    Retain its topology but only export attributes from our fixed schema.
    """
    def __init__(self, delegate):
        self.delegate = delegate

    def export(self, spans):
        from opentelemetry.sdk.trace import ReadableSpan
        cleaned = []
        allowed = {'http.request.method', 'http.response.status_code', 'http.route',
                   'mcp.tool.name', 'vigilo.origin', 'error.type'}
        for original in spans:
            own = original.instrumentation_scope.name == 'vigilo-mcp'
            cleaned.append(ReadableSpan(
                name=original.name if own else 'MCP request',
                context=original.context, parent=original.parent, resource=original.resource,
                attributes={k:v for k,v in original.attributes.items() if own and k in allowed},
                events=(), links=(), kind=original.kind,
                status=Status(original.status.status_code),
                start_time=original.start_time, end_time=original.end_time,
                instrumentation_scope=original.instrumentation_scope,
            ))
        return self.delegate.export(cleaned)

    def shutdown(self):
        return self.delegate.shutdown()

    def force_flush(self, timeout_millis=30000):
        return self.delegate.force_flush(timeout_millis)
