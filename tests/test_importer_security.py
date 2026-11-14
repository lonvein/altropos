"""Public DNS pinning contract with artificial responses and no network I/O."""
import socket

import httpx
import pytest

from navigator.documents.site_importer import fetch_mipt_bytes


def _dns(*addresses):
    return [(socket.AF_INET6 if ':' in address else socket.AF_INET, socket.SOCK_STREAM,
             socket.IPPROTO_TCP, '', (address, 443)) for address in addresses]


@pytest.mark.parametrize('addresses', [
    ('93.184.216.34', '127.0.0.1'), ('93.184.216.34', '10.0.0.1'),
    ('93.184.216.34', '169.254.169.254'), ('2606:4700:4700::1111', '::1'),
    ('2606:4700:4700::1111', 'fd00::1'), (),
])
def test_every_dns_answer_must_be_public(monkeypatch, addresses):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *_args, **_kwargs: _dns(*addresses))
    class NeverConnect(httpx.BaseTransport):
        def handle_request(self, request):
            pytest.fail('A mixed public/private DNS answer must not connect anywhere')
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **_kwargs: NeverConnect())
    with pytest.raises(ValueError, match='непубличных'):
        fetch_mipt_bytes('https://mipt.ru/artificial', max_bytes=100)


def test_dns_failure_is_explicit_and_never_connects(monkeypatch):
    def failed(*_args, **_kwargs):
        raise socket.gaierror('artificial resolver failure')
    monkeypatch.setattr(socket, 'getaddrinfo', failed)
    class NeverConnect(httpx.BaseTransport):
        def handle_request(self, request):
            pytest.fail('Resolver failure must not use an unchecked client fallback')
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **_kwargs: NeverConnect())
    with pytest.raises(ValueError, match='публичный адрес'):
        fetch_mipt_bytes('https://mipt.ru/artificial', max_bytes=100)


def test_redirect_rechecks_dns_without_losing_tls_hostname(monkeypatch):
    lookups, requests, config = [], [], []
    def resolve(host, port, **_kwargs):
        lookups.append((host, port))
        return _dns('93.184.216.34' if len(lookups) == 1 else '127.0.0.1')
    class PinRecorder(httpx.BaseTransport):
        def handle_request(self, request):
            requests.append(request)
            return httpx.Response(302, headers={'location': '/artificial-next'})
    def transport(**kwargs):
        config.append(kwargs)
        return PinRecorder()
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(httpx, 'HTTPTransport', transport)
    # Proxy environment must never override the explicitly pinned connection.
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:9')
    with pytest.raises(ValueError, match='непубличных'):
        fetch_mipt_bytes('https://mipt.ru/artificial', max_bytes=100)
    assert lookups == [('mipt.ru', 443), ('mipt.ru', 443)]
    assert len(requests) == 1
    request = requests[0]
    assert request.url.host == '93.184.216.34'
    assert request.headers['host'] == 'mipt.ru'
    assert request.extensions['sni_hostname'] == 'mipt.ru'
    assert request.url.scheme == 'https'
    assert len(config) == 1 and config[0]['trust_env'] is False
    assert config[0]['limits'].max_keepalive_connections == 0


def test_real_httpcore_connects_numeric_ip_and_verifies_each_tls_hostname(monkeypatch):
    # Keep the real HTTPX/HTTPCore code path through connection and TLS setup,
    # replacing only the network backend with a byte-stream test double.
    import ssl
    from httpcore._backends.mock import MockBackend, MockStream
    from navigator.documents.site_importer import PublicMiptTransport

    dns, connections, tls, writes = [], [], [], []
    def resolve(host, port, **_kwargs):
        dns.append(host)
        return _dns('93.184.216.34')
    class Stream(MockStream):
        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            tls.append(server_hostname)
            assert ssl_context.check_hostname
            assert ssl_context.verify_mode == ssl.CERT_REQUIRED
            return self
        def write(self, buffer, timeout=None):
            writes.append(buffer)
    class Backend(MockBackend):
        def connect_tcp(self, host, port, **_kwargs):
            connections.append((host, port))
            response = (b'HTTP/1.1 302 Found\r\nLocation: https://docs.mipt.ru/final\r\nContent-Length: 0\r\n\r\n'
                if len(connections) == 1 else b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
            return Stream([response])
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    public = PublicMiptTransport()
    public.inner._pool._network_backend = Backend([])
    final, content, _ = fetch_mipt_bytes('https://mipt.ru/artificial', max_bytes=100, transport=public)
    assert final == 'https://docs.mipt.ru/final' and content == b'ok'
    assert dns == ['mipt.ru', 'docs.mipt.ru']
    assert connections == [('93.184.216.34', 443)] * 2
    assert tls == ['mipt.ru', 'docs.mipt.ru']
    wire = b''.join(writes).lower()
    assert b'host: mipt.ru\r\n' in wire and b'host: docs.mipt.ru\r\n' in wire


@pytest.mark.parametrize('headers', [{}, {'content-length': '1'}])
def test_stream_limit_uses_actual_bytes_not_declared_length(headers):
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b'x' * 16
            yield b'x' * 16
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, stream=Chunks()))
    with pytest.raises(ValueError, match='Размер ответа'):
        fetch_mipt_bytes('https://mipt.ru/artificial', max_bytes=20, transport=transport)


def test_connection_fallback_uses_only_validated_addresses(monkeypatch):
    attempted = []
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *_a, **_k: _dns('2606:4700:4700::1111', '93.184.216.34'))
    class Connections(httpx.BaseTransport):
        def handle_request(self, request):
            attempted.append(request.url.host)
            assert request.headers['host'] == request.extensions['sni_hostname'] == 'mipt.ru'
            if len(attempted) == 1:
                raise httpx.ConnectError('artificial unreachable IPv6')
            return httpx.Response(200, content=b'ok')
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **_k: Connections())
    _, content, _ = fetch_mipt_bytes('https://mipt.ru/artificial', max_bytes=100)
    assert content == b'ok'
    assert attempted == ['2606:4700:4700::1111', '93.184.216.34']
