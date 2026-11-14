from fastapi.testclient import TestClient
from navigator.auth import AuthConfig, authenticate, issue_token, read_token, ROLE_ADMIN, ROLE_USER
from tests.test_auth import make_app
from scripts.deploy.gateway import ReaderGateway


def test_explicit_shared_operator_mode_and_existing_sessions():
    config = AuthConfig(secret=b'x'*40, demo_operator_access=True)
    assert authenticate(config, 'admin', 'qwerty') == ('admin', ROLE_ADMIN)
    old, _ = issue_token(config, 'admin', ROLE_USER)
    assert read_token(config, old)['r'] == ROLE_ADMIN
    config.demo_operator_access = False
    assert authenticate(config, 'admin', 'qwerty') == ('admin', ROLE_USER)


def test_shared_operator_has_full_review_ui_and_gateway_access(tmp_path, monkeypatch):
    monkeypatch.setenv('DEMO_OPERATOR_ACCESS', 'true')
    app = make_app(tmp_path, monkeypatch, demo_operator_access=True)
    with TestClient(ReaderGateway(app, 'https://aitropos.ru'), base_url='https://aitropos.ru', follow_redirects=False) as client:
        assert client.post('/login', data={'username':'admin','password':'qwerty'},
                           headers={'Origin':'https://aitropos.ru'}).status_code == 303
        assert client.get('/api/session').json()['is_admin']
        assert client.get('/review').status_code == 200
        assert client.get('/api/documents/artificial-missing/review').status_code == 404
