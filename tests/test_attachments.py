"""Vedlegg i meldingar, svar og skulefråvær (AttachmentRequest i appen).

Berre syntetiske data.
"""
import base64
import json

import httpx
import pytest

from tests.test_absence_thread import api as absence_api
from tests.test_manual_regressions import real_client, wire  # noqa: F401
from tests.test_messages import contacts, send
from tests.test_parent_actions import client, register, transport  # noqa: F401
from vigilo_connector.client import MAX_ATTACHMENT_BYTES, _attachments

PDF = {'name': 'Legeerklæring.pdf', 'dataBase64': base64.b64encode(b'%PDF-test').decode()}
WIRE_PDF = {'name': 'Legeerklæring.pdf', 'mimeType': 'APP_PDF',
            'dataBase64': base64.b64encode(b'%PDF-test').decode()}


def file(name, data=b'x'):
    return {'name': name, 'dataBase64': base64.b64encode(data).decode()}


@pytest.mark.parametrize('name,mime', [
    ('a.jpg', 'IMAGE_JPEG'), ('a.JPEG', 'IMAGE_JPEG'), ('a.png', 'PNG'), ('a.pdf', 'APP_PDF'),
    ('a.gif', 'GIF'), ('a.rtf', 'RTF'), ('a.csv', 'CSV'), ('a.txt', 'PLAIN'),
    ('a.docx', 'DOCUMENT'), ('a.doc', 'MS_WORD'), ('a.odt', 'OPEN_DOC'), ('a.xlsx', 'SHEET'),
    ('a.xls', 'EXCEL'), ('a.ods', 'SHEET_OPEN_OFFICE'), ('a.ppt', 'POWERPOINT'),
    # Som FileUtils.getMimeType: "pptx" inneheld "ppt" og blir POWERPOINT.
    ('a.pptx', 'POWERPOINT'), ('a.odp', 'OPEN_PRESENTATION'),
])
def test_mime_type_follows_app_mapping(name, mime):
    assert _attachments([file(name)])[0]['mimeType'] == mime


def test_none_and_empty_mean_no_attachments():
    assert _attachments(None) == [] and _attachments([]) == []


@pytest.mark.parametrize('items,match', [
    ('a.pdf', 'liste'),
    ([file('a.pdf')] * 11, 'Høgst 10'),
    ([{'name': 'a.pdf'}], 'name og dataBase64'),
    ([{**file('a.pdf'), 'mimeType': 'APP_PDF'}], 'name og dataBase64'),
    ([file('a.exe')], 'ikkje støtta'),
    ([file('utan-ending')], 'ikkje støtta'),
    ([file('../a.pdf')], 'Ugyldig filnamn'),
    ([{'name': 'a.pdf', 'dataBase64': '!!!'}], 'gyldig base64'),
    ([file('a.pdf', b'')], 'tomt'),
    ([file('a.pdf', b'x' * MAX_ATTACHMENT_BYTES)], 'for stort'),
])
def test_invalid_attachments_are_rejected(items, match):
    with pytest.raises(ValueError, match=match):
        _attachments(items)


def test_largest_allowed_file_passes():
    assert len(_attachments([file('a.pdf', b'x' * (MAX_ATTACHMENT_BYTES - 1))])) == 1


def test_student_absence_carries_attachments(client):
    bodies = []
    transport(client, lambda r: bodies.append(json.loads(r.content)) or httpx.Response(204))
    register(client, attachments=[PDF])
    assert bodies[0]['attachments'] == [WIRE_PDF]


def test_new_message_carries_attachments(client):
    posts = []
    def handler(request):
        if request.method == 'GET':
            return contacts(request)
        posts.append(json.loads(request.content))
        return httpx.Response(204)
    transport(client, handler)
    result = send(client, attachments=[PDF])
    assert posts[0]['attachments'] == [WIRE_PDF] and result['attachments'] == [PDF['name']]


def test_reply_carries_attachments(client):
    posts = []
    def handler(request):
        if request.method == 'GET':
            return contacts(request)
        posts.append(json.loads(request.content))
        return httpx.Response(204)
    transport(client, handler)
    client.reply_message('test-thread', 'Testsvar', [PDF])
    assert posts[0] == {'body': 'Testsvar', 'attachments': [WIRE_PDF]}


def test_absence_reply_carries_attachments(client):
    requests = []
    transport(client, absence_api(requests))
    client.reply_absence_thread('test-child', 'test-absence', 'Takk', [PDF])
    post = next(r for r in requests if r.method == 'POST')
    assert json.loads(post.content) == {'body': 'Takk', 'attachments': [WIRE_PDF]}


def test_invalid_attachment_never_reaches_api(client):
    transport(client, lambda r: pytest.fail('Ugyldig vedlegg nådde API-et'))
    with pytest.raises(ValueError, match='ikkje støtta'):
        register(client, attachments=[file('a.exe')])
    with pytest.raises(ValueError, match='ikkje støtta'):
        client.reply_message('test-thread', 'Testsvar', [file('a.exe')])


def test_write_tools_advertise_optional_attachments(wire, real_client):
    tools = {tool['name']: tool for tool in wire.list_tools()}
    for name in ('send_message', 'reply_message', 'register_student_absence',
                 'reply_absence_thread'):
        schema = tools[name]['inputSchema']
        assert 'attachments' in schema['properties'], name
        assert 'attachments' not in schema.get('required', []), name
