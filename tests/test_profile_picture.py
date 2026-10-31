"""Profilbilete (POST /api/current-user/profile-picture).

Berre syntetiske data.
"""
import base64
import json

import httpx
import pytest

from tests.test_manual_regressions import real_client, wire  # noqa: F401
from tests.test_parent_actions import client, transport  # noqa: F401
from vigilo_connector.client import WriteOutcomeUnknown

JPEG = base64.b64encode(b'\xff\xd8\xff\xe0syntetisk').decode()
ORIGINAL = {'name': 'original.jpg', 'dataBase64': JPEG}
CROPPED = {'name': 'cropped.jpg', 'dataBase64': JPEG}


def test_upload_matches_app_body(client):
    requests = []
    transport(client, lambda r: requests.append(r) or httpx.Response(200))
    result = client.set_profile_picture(ORIGINAL, CROPPED)
    [request] = requests
    assert request.method == 'POST' and request.url.path == '/api/current-user/profile-picture'
    # Som UploadProfilePictureUseCase: mimeType er "image/jpeg", ikkje enum-namnet.
    assert json.loads(request.content) == {
        'originalImage': {'name': 'original.jpg', 'mimeType': 'image/jpeg', 'dataBase64': JPEG},
        'croppedImage': {'name': 'cropped.jpg', 'mimeType': 'image/jpeg', 'dataBase64': JPEG}}
    assert result['status'] == 'updated'


@pytest.mark.parametrize('original,cropped,match', [
    ({'name': 'a.png', 'dataBase64': JPEG}, CROPPED, 'original_image må vere eit JPEG'),
    (ORIGINAL, {'name': 'b.jpg', 'dataBase64': base64.b64encode(b'PNG').decode()},
     'cropped_image er ikkje eit JPEG'),
    ('a.jpg', CROPPED, 'original_image'),
    ({'name': 'a.jpg'}, CROPPED, 'name og dataBase64'),
])
def test_invalid_images_never_reach_api(client, original, cropped, match):
    transport(client, lambda r: pytest.fail('Ugyldig bilete nådde API-et'))
    with pytest.raises(ValueError, match=match):
        client.set_profile_picture(original, cropped)


def test_unknown_outcome_is_not_retried(client):
    requests = []
    transport(client, lambda r: requests.append(r) or httpx.Response(502))
    with pytest.raises(WriteOutcomeUnknown, match='Profilbiletet'):
        client.set_profile_picture(ORIGINAL, CROPPED)
    assert len(requests) == 1


def test_upload_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, lambda r: requests.append(r) or httpx.Response(200))
    result = wire('set_profile_picture', {'original_image': ORIGINAL, 'cropped_image': CROPPED})
    assert result.get('isError') is not True, result
    assert len(requests) == 1
