import base64
import io
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pawroyalty_backend', ROOT / 'app.py')
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


ORIGINAL_PROVIDER = backend.edit_with_gemini
AUTH_HEADERS = {'Authorization': 'Bearer test-user-token', 'X-Firebase-AppCheck': 'test-appcheck-token'}


def fake_image():
    stream = io.BytesIO()
    Image.new('RGB', (64, 64), (50, 100, 150)).save(stream, format='JPEG')
    return stream.getvalue()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    backend.ROOT = tmp_path / 'quota.db'
    backend.app.dependency_overrides[backend.principal] = lambda: 'user-abc'
    monkeypatch.setenv('IMAGE_PROVIDER', 'gemini')
    monkeypatch.setenv('GEMINI_API_KEY', 'test-only')
    monkeypatch.setenv('ENABLE_PAID_IMAGE_GENERATION', 'true')

    async def fake_provider(images, prompt):
        assert len(images) in (1, 2)
        assert 'fur markings' in prompt
        return fake_image(), 'Your pet now runs the kingdom.'

    monkeypatch.setattr(backend, 'edit_with_gemini', fake_provider)
    with TestClient(backend.app) as api:
        yield api
    backend.app.dependency_overrides.clear()


def upload(api, mode='single', theme='ceo', count=1, prompt=''):
    return api.post('/api/v1/generate', data={
        'mode': mode, 'theme': theme, 'name1': 'Momo', 'name2': 'Luna', 'prompt': prompt,
    }, files=[('photos', (f'pet{i}.jpg', fake_image(), 'image/jpeg')) for i in range(count)], headers=AUTH_HEADERS)


def test_quota_and_generation_result(client):
    assert client.get('/api/v1/quota').json()['remaining'] == 5
    result = upload(client)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['generation_id']
    assert base64.b64decode(data['image_base64']).startswith(b'\xff\xd8')
    assert data['lore']
    assert client.get('/api/v1/quota').json()['remaining'] == 4


def test_requires_two_images_for_duo(client):
    assert upload(client, 'duo', 'monarch_knight', count=1).status_code == 422
    assert upload(client, 'duo', 'monarch_knight', count=2).status_code == 200


def test_credits_enforced(client):
    for i in range(5):
        assert upload(client).status_code == 200
    assert upload(client).status_code == 429
    assert client.get('/api/v1/quota').json()['remaining'] == 0


def test_rejects_empty_and_invalid_upload(client):
    result = client.post('/api/v1/generate', data={'mode': 'single', 'theme': 'ceo'},
                         files=[('photos', ('notimage.jpg', b'garbage', 'image/jpeg'))], headers=AUTH_HEADERS)
    assert result.status_code == 422
    assert client.get('/api/v1/quota').json()['remaining'] == 5


def test_invalid_prompt_blocked(client):
    result = upload(client, 'custom', 'custom', 1, 'graphic gore of animals')
    assert result.status_code == 422


def test_report_saves_thumbnail_for_review(client):
    result = upload(client).json()
    posted = client.post('/api/v1/report', headers=AUTH_HEADERS, json={
        'generation_id': result['generation_id'],
        'reason': 'Offensive content', 'notes': 'Please review',
        'image_base64': base64.b64encode(fake_image()).decode(),
    })
    assert posted.status_code == 200, posted.text
    with backend.db_connect() as db:
        rows = db.execute('SELECT reason,review_image FROM reports').fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 'Offensive content'
        assert rows[0][1].startswith(b'\xff\xd8')


def test_photo_never_persisted_for_normal_generation(client):
    assert upload(client).status_code == 200
    with backend.db_connect() as db:
        cols = db.execute('PRAGMA table_info(generations)').fetchall()
        assert not any('image' in x[1] or 'photo' in x[1] for x in cols)


def test_privacy_requires_real_developer_contact(client):
    assert client.get('/privacy').status_code == 503


def test_free_tier_configuration_does_not_invoke_paid_image_model(client, monkeypatch):
    monkeypatch.setenv('ENABLE_PAID_IMAGE_GENERATION', 'false')
    info = client.get('/api/v1/quota').json()
    assert info['image_provider_configured'] is False
    assert info['image_generation_requires_billing'] is True
    result = upload(client)
    assert result.status_code == 503
    assert 'not configured' in result.json()['detail']
    assert client.get('/api/v1/quota').json()['remaining'] == 5


def test_no_api_key_does_not_invoke_paid_image_model(client, monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY')
    assert upload(client).status_code == 503
    assert client.get('/api/v1/quota').json()['remaining'] == 5


def test_old_generation_still_reportable_after_cleanup(client):
    result = upload(client).json()
    with backend.db_connect() as db:
        db.execute("UPDATE generations SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (result['generation_id'],))
    backend.prune_old_records()
    response = client.post('/api/v1/report', headers=AUTH_HEADERS, json={
        'generation_id': result['generation_id'],
        'reason': 'Offensive content', 'notes': 'Old saved image',
        'image_base64': base64.b64encode(fake_image()).decode(),
    })
    assert response.status_code == 200, response.text


def test_ip_quota_is_optional_behind_untrusted_proxy(client):
    old = backend.IP_LIMIT
    try:
        backend.IP_LIMIT = 0
        assert upload(client).status_code == 200
        with backend.db_connect() as db:
            assert db.execute("SELECT count(*) FROM credits WHERE actor LIKE 'ip:%'").fetchone()[0] == 0
    finally:
        backend.IP_LIMIT = old


def test_report_cannot_reference_another_users_generation(client):
    original = upload(client).json()['generation_id']
    backend.app.dependency_overrides[backend.principal] = lambda: 'different-user'
    response = client.post('/api/v1/report', headers=AUTH_HEADERS, json={
        'generation_id': original, 'reason': 'Offensive content',
        'image_base64': base64.b64encode(fake_image()).decode(),
    })
    assert response.status_code == 404


def test_failed_model_request_refunds_one_credit(client, monkeypatch):
    from fastapi import HTTPException

    async def fake_failure(images, prompt):
        raise HTTPException(503, 'AI is unavailable')

    monkeypatch.setattr(backend, 'edit_with_gemini', fake_failure)
    assert upload(client).status_code == 503
    assert client.get('/api/v1/quota').json()['remaining'] == 5


def test_deleting_account_removes_server_data_and_firebase_identity(client, monkeypatch):
    id = upload(client).json()['generation_id']
    received = []
    class MockAdmin:
        class auth:
            @staticmethod
            def delete_user(uid):
                received.append(uid)
    monkeypatch.setattr(backend, 'firebase_auth', lambda: MockAdmin())
    assert client.delete('/api/v1/me').json() == {'deleted': True}
    assert received == ['user-abc']
    with backend.db_connect() as db:
        assert db.execute('SELECT COUNT(*) FROM generations WHERE id=?', (id,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM credits WHERE actor='u:user-abc'").fetchone()[0] == 0


def test_unsupported_image_mime_is_rejected(client):
    result = client.post('/api/v1/generate', data={'mode':'single','theme':'ceo'},
                         files=[('photos', ('image.gif', b'GIF89a', 'image/gif'))], headers=AUTH_HEADERS)
    assert result.status_code == 415


def test_public_privacy_page_requires_contact_and_then_works(client, monkeypatch):
    monkeypatch.setenv('DEVELOPER_LEGAL_NAME', 'Example Developer')
    monkeypatch.setenv('PRIVACY_CONTACT_EMAIL', 'privacy@example.com')
    response = client.get('/privacy')
    assert response.status_code == 200
    assert 'Example Developer' in response.text
    assert 'until account deletion' in response.text


def test_web_account_deletion_request_is_queued(client):
    result = client.post('/deletion-request', data={
        'uid': 'abcdefghijk123', 'email': 'test@example.com', 'website': ''})
    assert result.status_code == 200
    with backend.db_connect() as db:
        assert db.execute('SELECT COUNT(*) FROM deletion_requests').fetchone()[0] == 1


def test_public_health_reflects_paid_image_safety_switch(client, monkeypatch):
    monkeypatch.setenv('ENABLE_PAID_IMAGE_GENERATION', 'false')
    response = client.get('/health')
    assert response.status_code == 200
    result = response.json()
    assert result['status'] == 'ok'
    assert result['image_provider_configured'] is False
    assert result['image_generation_requires_billing'] is True


def test_cloudflare_free_provider_status(client, monkeypatch):
    monkeypatch.setenv('IMAGE_PROVIDER', 'cloudflare')
    monkeypatch.setenv('CF_ACCOUNT_ID', 'fake-account')
    monkeypatch.setenv('CF_API_TOKEN', 'not-real-secret')
    details = client.get('/api/v1/quota').json()
    assert details['image_provider_configured'] is True
    assert details['image_generation_requires_billing'] is False
    check = client.get('/health').json()
    assert check['provider'] == 'cloudflare'
    assert 'flux-2-klein-4b' in check['model']


def test_cloudflare_multipart_with_two_small_references(client, monkeypatch):
    import httpx
    monkeypatch.setenv('IMAGE_PROVIDER', 'cloudflare')
    monkeypatch.setenv('CF_ACCOUNT_ID', 'fake-account')
    monkeypatch.setenv('CF_API_TOKEN', 'not-real-secret')
    sent = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, *, headers=None, json=None, data=None, files=None):
            sent.append((url, headers, data, files))
            assert json is None
            return httpx.Response(200, json={"success": True, "result": {
                "image": base64.b64encode(fake_image()).decode()}} ,
                request=httpx.Request('POST', url))

    monkeypatch.setattr(backend, 'edit_with_gemini', ORIGINAL_PROVIDER)
    monkeypatch.setattr(backend.httpx, 'AsyncClient', FakeClient)
    result = upload(client, mode='duo', theme='monarch_knight', count=2)
    assert result.status_code == 200, result.text
    assert len(sent) == 1
    url, headers, data, files = sent[0]
    assert url.endswith('/@cf/black-forest-labs/flux-2-klein-4b')
    assert headers['Authorization'] == 'Bearer not-real-secret'
    assert data['width'] == '768'
    assert [f[0] for f in files] == ['input_image_0', 'input_image_1']
    for _, (_, photo, mime) in files:
        with Image.open(io.BytesIO(photo)) as im:
            assert im.width < 512 and im.height < 512
        assert mime == 'image/jpeg'
    assert client.get('/api/v1/quota').json()['remaining'] == 4


def test_cloudflare_quota_error_refunds_local_credit(client, monkeypatch):
    import httpx
    monkeypatch.setenv('IMAGE_PROVIDER', 'cloudflare')
    monkeypatch.setenv('CF_ACCOUNT_ID', 'fake-account')
    monkeypatch.setenv('CF_API_TOKEN', 'not-real-secret')

    class ExhaustedClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kwargs):
            return httpx.Response(429, json={"success": False}, request=httpx.Request('POST', url))

    monkeypatch.setattr(backend, 'edit_with_gemini', ORIGINAL_PROVIDER)
    monkeypatch.setattr(backend.httpx, 'AsyncClient', ExhaustedClient)
    result = upload(client)
    assert result.status_code == 503
    assert 'Free image quota' in result.json()['detail']
    assert client.get('/api/v1/quota').json()['remaining'] == 5


def test_missing_cloudflare_token_never_spends_credit(client, monkeypatch):
    monkeypatch.setenv('IMAGE_PROVIDER', 'cloudflare')
    monkeypatch.setenv('CF_ACCOUNT_ID', 'fake-account')
    monkeypatch.delenv('CF_API_TOKEN', raising=False)
    result = upload(client)
    assert result.status_code == 503
    assert client.get('/api/v1/quota').json()['remaining'] == 5


def test_cloudflare_ref_photo_resize_preserves_aspect_ratio():
    stream = io.BytesIO()
    Image.new('RGB', (1400, 700)).save(stream, format='JPEG')
    resized = backend.cloudflare_reference_jpeg(stream.getvalue())
    with Image.open(io.BytesIO(resized)) as picture:
        assert picture.size == (480, 240)


def test_shared_allowance_stops_fresh_anonymous_accounts(client, monkeypatch):
    # Creating fresh anonymous users cannot bypass the studio's total daily budget.
    monkeypatch.setattr(backend, 'GLOBAL_DAILY_LIMIT', 2)
    assert upload(client).status_code == 200
    backend.app.dependency_overrides[backend.principal] = lambda: 'fresh-user-1'
    assert upload(client).status_code == 200
    backend.app.dependency_overrides[backend.principal] = lambda: 'fresh-user-2'
    response = upload(client)
    assert response.status_code == 429
    assert 'shared' not in response.text or 'allowance' in response.text
    quota = client.get('/api/v1/quota').json()
    assert quota['remaining'] == 0
    assert quota['user_remaining'] == 5
    assert quota['service_remaining'] == 0
    assert quota['service_limit'] == 2
    assert backend.global_credit_count() == 2


def test_failed_generation_refunds_shared_daily_allowance(client, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(backend, 'GLOBAL_DAILY_LIMIT', 1)

    async def unavailable(*_):
        raise HTTPException(503, 'Provider offline')

    monkeypatch.setattr(backend, 'edit_with_gemini', unavailable)
    assert upload(client).status_code == 503
    assert backend.global_credit_count() == 0
    response = client.get('/api/v1/quota').json()
    assert response['remaining'] == 1
    assert response['service_remaining'] == 1


def test_emergency_shutdown_zero_shared_budget_does_not_call_provider(client, monkeypatch):
    monkeypatch.setattr(backend, 'GLOBAL_DAILY_LIMIT', 0)
    calls = []

    async def provider(*_):
        calls.append(True)
        return fake_image(), 'caption'

    monkeypatch.setattr(backend, 'edit_with_gemini', provider)
    assert upload(client).status_code == 429
    assert calls == []
    assert backend.global_credit_count() == 0


def test_sensitive_responses_are_not_cached(client):
    response = client.get('/api/v1/quota')
    assert response.headers['cache-control'] == 'no-store, private'
    assert response.headers['x-content-type-options'] == 'nosniff'
    response = upload(client)
    assert response.headers['cache-control'] == 'no-store, private'


def test_public_health_never_returns_api_tokens(client, monkeypatch):
    monkeypatch.setenv('IMAGE_PROVIDER', 'cloudflare')
    monkeypatch.setenv('CF_API_TOKEN', 'super-secret-provider-token')
    monkeypatch.setenv('CF_ACCOUNT_ID', 'not-secret-account')
    monkeypatch.setenv('GEMINI_API_KEY', 'super-secret-gemini-key')
    result = client.get('/health')
    assert result.status_code == 200
    assert 'super-secret-provider-token' not in result.text
    assert 'super-secret-gemini-key' not in result.text


def test_missing_auth_headers_rejected_before_multipart_parsing(client):
    response = client.post('/api/v1/generate', data={'mode': 'single', 'theme': 'ceo'},
                           files=[('photos', ('bad.jpg', b'bad', 'image/jpeg'))])
    assert response.status_code == 401
    assert client.get('/api/v1/quota').json()['remaining'] == 5


def test_missing_appcheck_header_rejected(client):
    response = client.post('/api/v1/generate', headers={'Authorization': 'Bearer dummy'},
                           data={'mode': 'single', 'theme': 'ceo'},
                           files=[('photos', ('bad.jpg', b'bad', 'image/jpeg'))])
    assert response.status_code == 401


def test_oversized_content_length_rejected_before_upload(client):
    headers = {**AUTH_HEADERS, 'Content-Length': str(18 * 1024 * 1024)}
    response = client.post('/api/v1/generate', headers=headers,
                           data={'mode':'single', 'theme':'ceo'},
                           files=[('photos', ('pet.jpg', fake_image(), 'image/jpeg'))])
    assert response.status_code == 413


def test_android_source_never_calls_provider_directly():
    project = Path(__file__).resolve().parents[2]
    client_code = (project / 'app/src/main/java').rglob('*.kt')
    for path in client_code:
        text = path.read_text(encoding='utf-8')
        assert 'generativelanguage.googleapis.com' not in text
        assert 'api.cloudflare.com' not in text
        assert 'CF_API_TOKEN' not in text
        assert 'GEMINI_API_KEY' not in text
