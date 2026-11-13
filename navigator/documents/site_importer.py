"""Защищённый импорт страниц и документов с сайта mipt.ru и его поддоменов."""
from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_DOC_EXTENSIONS = {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx"}
DEFAULT_TIMEOUT = 10.0
MAX_REDIRECTS = 5
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


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
    clean, _ = urldefrag(raw)
    return clean


def filename_from_url(url: str, fallback: str = "imported-document") -> str:
    """Извлекает безопасное имя файла из URL."""
    path_name = unquote(Path(urlparse(url).path).name or "")
    name = Path(path_name).name.strip()
    return name or fallback


def fetch_mipt_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, bytes, str]:
    """Безопасно скачивает данные с mipt.ru с проверкой каждого редиректа и лимита размера."""
    current_url = validate_mipt_url(url)
    headers = {"User-Agent": "AltroposMIPTNavigator/1.0 (+https://mipt.ru)"}

    with httpx.Client(
        follow_redirects=False,
        timeout=timeout,
        transport=transport,
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

                content_type = response.headers.get("content-type", "")
                return current_url, b"".join(chunks), content_type

    raise ValueError("Превышено допустимое число перенаправлений.")


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
    final_url, raw_bytes, content_type = fetch_mipt_bytes(
        url,
        max_bytes=max_bytes,
        timeout=timeout,
        transport=transport,
    )
    html_text = _decode_html(raw_bytes, content_type)
    soup = BeautifulSoup(html_text, "lxml")

    title_tag = soup.find("title")
    h1_tag = soup.find("h1")
    page_title = " ".join(((title_tag.get_text(" ", strip=True) if title_tag else "") or (h1_tag.get_text(" ", strip=True) if h1_tag else "") or final_url).split())[:300]

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text_body = " ".join(soup.get_text(" ", strip=True).split())
    page_excerpt = text_body[:400] + ("..." if len(text_body) > 400 else "")

    links: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_allowed: set[str] = set()
    seen_rejected: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:")):
            continue

        abs_url, _ = urldefrag(urljoin(final_url, href))
        link_title = " ".join(anchor.get_text(" ", strip=True).split())
        parsed = urlparse(abs_url)
        ext = Path(parsed.path).suffix.lower()
        doc_kind = ALLOWED_DOC_EXTENSIONS.get(ext)
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
                links.append({
                    "url": clean_url,
                    "title": display_title,
                    "kind": doc_kind,
                    "allowed": True,
                })
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
