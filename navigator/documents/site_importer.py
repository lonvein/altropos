"""Защищённый импорт страниц и документов с сайта mipt.ru и его поддоменов."""
from __future__ import annotations

import ipaddress
import json
import socket
from dataclasses import dataclass
from email.message import Message
from email.utils import collapse_rfc2231_value
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urldefrag, urljoin, urlparse
from zipfile import BadZipFile, ZipFile

import httpx
from bs4 import BeautifulSoup

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_DOC_EXTENSIONS = {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx"}
DEFAULT_TIMEOUT = 10.0
MAX_REDIRECTS = 5
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
OFFICE_PARTS = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml"}


@dataclass(frozen=True)
class Download:
    url: str
    content: bytes
    headers: httpx.Headers


def is_mipt_host(hostname: str | None) -> bool:
    """Разрешает только mipt.ru и его поддомены; отклоняет IP-адреса и локальные хосты."""
    if not hostname:
        return False
    host = hostname.strip().rstrip(".").lower()
    if not host or host in {"localhost", "localhost.localdomain"}:
        return False
    try:
        ipaddress.ip_address(host.strip("[]"))
        return False
    except ValueError:
        pass
    return host == "mipt.ru" or host.endswith(".mipt.ru")


def validate_mipt_url(url: str) -> str:
    """Проверяет URL для импорта с mipt.ru. Возвращает очищенный URL или бросает ValueError."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("Укажите URL страницы mipt.ru.")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError("Управляющие символы в URL не разрешены.")
    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise ValueError("Некорректный формат URL.") from exc

    if parsed.scheme.lower() not in ALLOWED_SCHEMES or not parsed.hostname:
        raise ValueError("Разрешены только http(s)-адреса.")
    if parsed.username or parsed.password:
        raise ValueError("URL с логином или паролем не разрешён.")
    if not is_mipt_host(parsed.hostname):
        raise ValueError("Разрешены только ссылки на mipt.ru и его поддомены.")
    try:
        if parsed.port not in {None, 80 if parsed.scheme.lower() == "http" else 443}:
            raise ValueError("Разрешены только стандартные порты HTTP и HTTPS.")
    except ValueError as exc:
        raise ValueError("Некорректный или недопустимый порт URL.") from exc
    clean, _ = urldefrag(raw)
    return clean


class PublicMiptTransport(httpx.BaseTransport):
    """Pin each connection to checked public DNS results, retaining TLS identity.

    Checking DNS then asking an HTTP client to resolve again permits rebinding.
    Connect to the selected numeric address and keep the original Host and SNI.
    """
    def __init__(self):
        # Pool origins would otherwise be numeric IPs: two different hostnames
        # on one IP must not reuse a TLS session verified for only the first.
        self.inner = httpx.HTTPTransport(trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=0))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        validate_mipt_url(str(request.url))
        host = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        try:
            addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(
                host, port, type=socket.SOCK_STREAM)))
        except OSError as exc:
            raise ValueError("Не удалось определить публичный адрес сервера МФТИ.") from exc
        if not addresses or any(not ipaddress.ip_address(address).is_global
                                or ipaddress.ip_address(address).is_multicast for address in addresses):
            raise ValueError("Импорт с непубличных сетевых адресов запрещён.")
        for address in addresses:
            pinned = httpx.Request(request.method, request.url.copy_with(host=address),
                headers=request.headers, stream=request.stream,
                extensions={**request.extensions, "sni_hostname": host})
            try:
                return self.inner.handle_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if address == addresses[-1]:
                    raise

    def close(self):
        self.inner.close()


def filename_from_url(url: str, fallback: str = "imported-document") -> str:
    """Извлекает безопасное имя файла из URL."""
    path_name = unquote(Path(urlparse(url).path).name or "")
    name = Path(path_name).name.strip()
    return name or fallback


def _fetch_mipt(
    url: str,
    *,
    max_bytes: int,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> Download:
    """Безопасно скачивает данные с mipt.ru с проверкой каждого редиректа и лимита размера."""
    current_url = validate_mipt_url(url)
    headers = {"User-Agent": "AltroposMIPTNavigator/1.0 (+https://mipt.ru)", "Accept-Encoding": "identity"}

    with httpx.Client(
        follow_redirects=False,
        timeout=timeout,
        transport=transport if transport is not None else PublicMiptTransport(),
        trust_env=False,
        headers=headers,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            with client.stream("GET", current_url) as response:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Сервер вернул перенаправление без заголовка Location.")
                    next_url = urljoin(current_url, location)
                    current_url = validate_mipt_url(next_url)
                    continue

                if response.status_code >= 400:
                    raise ValueError(f"Сервер вернул ошибку HTTP {response.status_code}.")

                if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
                    # Avoid decoding a compressed bomb into one unbounded chunk
                    # before the byte limit below can inspect it.
                    raise ValueError("Сервер прислал сжатый ответ вместо запрошенного несжатого файла.")

                content_length = response.headers.get("content-length")
                if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                    raise ValueError("Размер файла превышает допустимый лимит.")

                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("Размер ответа превышает допустимый лимит.")
                    chunks.append(chunk)

                return Download(current_url, b"".join(chunks), response.headers)

    raise ValueError("Превышено допустимое число перенаправлений.")


def fetch_mipt_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, bytes, str]:
    download = _fetch_mipt(url, max_bytes=max_bytes, timeout=timeout, transport=transport)
    return download.url, download.content, download.headers.get("content-type", "")


def _document_kind(content: bytes) -> str | None:
    if content[:1024].lstrip().startswith(b"%PDF-"):
        return "pdf"
    if content.startswith(b"PK\x03\x04"):
        try:
            with ZipFile(BytesIO(content)) as archive:
                entries = set(archive.namelist())
                for kind, part in OFFICE_PARTS.items():
                    if part in entries and "[Content_Types].xml" in entries:
                        return kind
        except BadZipFile:
            pass
    return None


def _download_code(url: str) -> str:
    parsed = urlparse(url)
    if parsed.path.rstrip("/") == "/docs/download.php":
        return parse_qs(parsed.query).get("code", [""])[0].strip()
    return ""


def _document_name(download: Download, source_url: str, kind: str) -> str:
    headers = Message()
    # httpx falls back to Latin-1 for non-ASCII headers. The MIPT endpoint
    # currently sends raw UTF-8 bytes in its legacy filename parameter.
    raw_disposition = next(
        (value for key, value in download.headers.raw if key.lower() == b"content-disposition"), b"",
    )
    try:
        disposition = raw_disposition.decode("utf-8")
    except UnicodeDecodeError:
        disposition = raw_disposition.decode("latin-1")
    headers["Content-Disposition"] = disposition
    extended_name = next(
        (collapse_rfc2231_value(value) for key, value in
         (headers.get_params(header="Content-Disposition") or [])
         if key == "filename" and isinstance(value, tuple)),
        None,
    )
    for candidate in (
        extended_name,
        headers.get_filename(),
        filename_from_url(download.url),
        filename_from_url(source_url),
    ):
        if candidate:
            name = Path(candidate.replace("\\", "/")).name.strip()
            if Path(name).suffix.lower() == f".{kind}" and not any(ord(char) < 32 for char in name):
                return f"{Path(name).stem[:490]}.{kind}"
    code = Path(_download_code(source_url).replace("\\", "/")).name
    safe_code = "".join(char for char in code if char.isalnum() or char in "-_")[:490]
    return f"{safe_code or 'imported-document'}.{kind}"


def fetch_mipt_document(
    url: str,
    *,
    max_bytes: int,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, str, bytes]:
    download = _fetch_mipt(url, max_bytes=max_bytes, timeout=timeout, transport=transport)
    kind = _document_kind(download.content)
    if not kind:
        raise ValueError("По ссылке не получен файл PDF, DOCX или XLSX.")
    return download.url, _document_name(download, url, kind), download.content


def _block_page_html(url: str, soup: BeautifulSoup, fetch) -> str | None:
    """Resolve the Angular page through the same public API as the MIPT frontend."""
    if urlparse(url).hostname not in {"mipt.ru", "www.mipt.ru"} or not soup.find("app-root"):
        return None
    languages = ("ru", "en", "cn")
    parts = [part for part in unquote(urlparse(url).path).split("/") if part]
    lang = parts.pop(0) if parts and parts[0] in languages else "ru"

    def api(action: str, **params):
        query = urlencode({"action": f"blockPage.{action}", "lang": lang, **params})
        try:
            value = json.loads(fetch(urljoin(url, f"/api/v1/?{query}")).content)
        except ValueError as exc:
            raise ValueError("Не удалось прочитать список документов из API МФТИ.") from exc
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ValueError("API МФТИ вернул неожиданный формат списка документов.")
        return value

    sections = {}
    for item in api("getList"):
        parent, code, item_id = item.get("sectionId"), item.get("code"), item.get("id")
        if (parent is not None and type(parent) is not int) or not isinstance(code, str) or type(item_id) is not int:
            raise ValueError("API МФТИ вернул некорректное дерево разделов.")
        sections[(parent, code)] = item_id
    section_id = None
    for code in [lang, *parts]:
        section_id = sections.get((section_id, code))
        if not isinstance(section_id, int):
            return None

    blocks = []
    for item in api("getItems", sectionId=section_id):
        lead = item.get("previewText")
        if isinstance(lead, str):
            blocks.append(lead)
        details = item.get("detailText")
        if isinstance(details, list):
            blocks.extend(
                block["html"] for block in details
                if isinstance(block, dict) and isinstance(block.get("html"), str)
            )
    return "\n".join(blocks)


def _decode_html(raw: bytes, content_type: str) -> str:
    charset = None
    if "charset=" in content_type.lower():
        part = content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip().strip("\"'")
        if part:
            charset = part
    for enc in ([charset] if charset else []) + ["utf-8", "cp1251"]:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def inspect_mipt_page(
    url: str,
    *,
    max_bytes: int,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Загружает HTML-страницу mipt.ru и классифицирует ссылки на документы (links / rejected)."""
    def fetch(target: str) -> Download:
        return _fetch_mipt(target, max_bytes=max_bytes, timeout=timeout, transport=transport)

    download = fetch(url)
    final_url = download.url
    kind = _document_kind(download.content)
    if kind:
        name = _document_name(download, url, kind)
        return {
            "url": final_url,
            "page_title": name,
            "page_excerpt": "",
            "links": [{"url": validate_mipt_url(url), "title": name, "kind": kind, "allowed": True}],
            "rejected": [],
        }

    if _download_code(url) or Path(urlparse(url).path).suffix.lower() in ALLOWED_DOC_EXTENSIONS:
        raise ValueError("По ссылке не получен файл PDF, DOCX или XLSX.")

    html_text = _decode_html(download.content, download.headers.get("content-type", ""))
    soup = BeautifulSoup(html_text, "lxml")
    blocks = _block_page_html(final_url, soup, fetch)
    if blocks is not None:
        soup = BeautifulSoup(blocks, "lxml")

    title_tag = soup.find("title")
    h1_tag = soup.find("h1")
    page_title = " ".join(((title_tag.get_text(" ", strip=True) if title_tag else "") or (h1_tag.get_text(" ", strip=True) if h1_tag else "") or final_url).split())[:300]

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text_body = " ".join(soup.get_text(" ", strip=True).split())
    page_excerpt = text_body[:400] + ("..." if len(text_body) > 400 else "")

    links: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    untitled_links: dict[str, dict[str, Any]] = {}
    seen_allowed: set[str] = set()
    seen_rejected: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:")):
            continue

        try:
            abs_url, _ = urldefrag(urljoin(final_url, href))
        except ValueError:
            continue
        link_title = " ".join(anchor.get_text(" ", strip=True).split())
        parsed = urlparse(abs_url)
        ext = Path(parsed.path).suffix.lower()
        doc_kind = ALLOWED_DOC_EXTENSIONS.get(ext) or ("file" if _download_code(abs_url) else None)
        fallback_name = filename_from_url(abs_url, abs_url)
        display_title = (link_title or fallback_name)[:200]

        try:
            clean_url = validate_mipt_url(abs_url)
        except ValueError as exc:
            if abs_url not in seen_rejected:
                seen_rejected.add(abs_url)
                rejected.append({
                    "url": abs_url,
                    "title": display_title,
                    "allowed": False,
                    "reason": str(exc),
                })
            continue

        if doc_kind is not None:
            if clean_url not in seen_allowed:
                seen_allowed.add(clean_url)
                link = {
                    "url": clean_url,
                    "title": display_title,
                    "kind": doc_kind,
                    "allowed": True,
                }
                links.append(link)
                if not link_title:
                    untitled_links[clean_url] = link
            elif link_title and clean_url in untitled_links:
                untitled_links.pop(clean_url)["title"] = display_title
        elif ext and ext not in {".html", ".htm", ".php", ".aspx"}:
            if clean_url not in seen_rejected:
                seen_rejected.add(clean_url)
                rejected.append({
                    "url": clean_url,
                    "title": display_title,
                    "allowed": False,
                    "reason": f"Формат файла {ext} не поддерживается (нужен PDF, DOCX или XLSX).",
                })

    return {
        "url": final_url,
        "page_title": page_title,
        "page_excerpt": page_excerpt,
        "links": links,
        "rejected": rejected,
    }
