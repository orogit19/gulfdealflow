"""GulfDealFlow FastAPI backend.

Endpoints:
    GET /health                  - lightweight service health check
    GET /deals                   - list deals, optional filters: country, sector, stage, year
    GET /deals/{id}              - single deal by deal_id
    GET /stats                   - totals + breakdowns by country / sector / stage
    GET /investors/leaderboard   - ranked lead investors with deal/capital/sector/stage stats
    POST /discover/rss           - discover GCC funding article candidates from RSS
    POST /ingest/url             - fetch and store a raw funding article URL
    POST /ingest/extract/{id}    - extract a structured deal from a raw source
    POST /ingest/portfolio-page  - extract draft VC portfolio relationships
    POST /relationships/{id}/enrich-funding - draft funding extraction for approved relationship

Run:
    uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request as URLRequest, build_opener

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from openai import OpenAI
from pydantic import BaseModel, Field
from supabase import Client, create_client

load_dotenv(Path(__file__).parent / ".env")


@lru_cache(maxsize=1)
def get_client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    # The API uses the anon key — reads only, RLS enforced.
    key = os.environ.get("SUPABASE_ANON_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_ANON_KEY must be set in .env")
    return create_client(url, key)


@lru_cache(maxsize=1)
def get_ingestion_client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env"
        )
    return create_client(url, key)


@lru_cache(maxsize=1)
def get_openai_client() -> OpenAI:
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY must be set in .env")
    return OpenAI()


app = FastAPI(title="GulfDealFlow API", version="0.1.0")

_ADMIN_PATH_PREFIXES = (
    "/admin",
    "/discover",
    "/ingest",
    "/relationships",
    "/raw-sources",
    "/ingestion-logs",
    "/extracted-deals",
)


@app.middleware("http")
async def require_admin_api_key(request: Request, call_next):
    admin_key = os.environ.get("ADMIN_API_KEY")
    if (
        admin_key
        and request.method != "OPTIONS"
        and request.url.path.startswith(_ADMIN_PATH_PREFIXES)
    ):
        provided_key = request.headers.get("x-admin-key")
        if provided_key != admin_key:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid admin API key"},
            )
    return await call_next(request)

# CORS: defaults to "*" (no credentials).
# Set CORS_ORIGINS to a comma-separated list in production to lock to your
# frontend domain, e.g.  CORS_ORIGINS=https://gulfdealflow.vercel.app
_origins_env = os.environ.get("CORS_ORIGINS", "*").strip()
allow_origins = ["*"] if _origins_env == "*" else [
    o.strip() for o in _origins_env.split(",") if o.strip()
]
_EXPOSED_RESPONSE_HEADERS = (
    "X-GDF-Duplicate",
    "X-GDF-Raw-Source-Id",
    "X-GDF-Fetch-Status",
    "X-GDF-Existing-Extraction",
    "X-GDF-Extracted-Deal-Id",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=list(_EXPOSED_RESPONSE_HEADERS),
)


class IngestUrlRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)


class PortfolioPageRequest(BaseModel):
    investor_name: str = Field(..., min_length=1, max_length=200)
    url: str = Field(..., min_length=8, max_length=2048)


class FundingEnrichmentRequest(BaseModel):
    source_url: str = Field(..., min_length=8, max_length=2048)


class RssDiscoveryRequest(BaseModel):
    queries: list[str] | None = None
    limit: int = Field(default=50, ge=1, le=200)


class DealExtraction(BaseModel):
    company_name: str | None
    country: str | None
    amount_usd: int | None
    amount_original: str | None
    currency_original: str | None
    stage: str | None
    announcement_date: str | None
    sector: str | None
    sub_sector: str | None
    lead_investor: str | None
    co_investors: list[str]
    website: str | None
    is_funding_round: bool
    confidence_score: float
    extraction_notes: str


class ExtractedDealUpdate(BaseModel):
    company_name: str | None = None
    country: str | None = None
    amount_usd: int | None = None
    amount_original: str | None = None
    currency_original: str | None = None
    stage: str | None = None
    announcement_date: str | None = None
    sector: str | None = None
    sub_sector: str | None = None
    lead_investor: str | None = None
    co_investors: list[str] | None = None
    website: str | None = None
    is_funding_round: bool | None = None
    confidence_score: float | None = None
    extraction_notes: str | None = None


class ReviewApprovePayload(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class ReviewRejectPayload(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


@app.get("/")
def root():
    return {"service": "GulfDealFlow API", "version": "0.1.0"}


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "GulfDealFlow API",
        "version": "0.1.0",
    }


def _env_config_status() -> dict:
    required = [
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_ANON_KEY",
        "OPENAI_API_KEY",
    ]
    optional = ["ADMIN_API_KEY", "CRON_SECRET", "CORS_ORIGINS", "CSV_PATH"]
    required_status = {name: bool(os.environ.get(name)) for name in required}
    optional_status = {name: bool(os.environ.get(name)) for name in optional}
    cors_origins = (os.environ.get("CORS_ORIGINS") or "").strip()
    production_checks = {
        "admin_api_key": optional_status["ADMIN_API_KEY"],
        "cron_secret": optional_status["CRON_SECRET"],
        "cors_restricted": bool(cors_origins and cors_origins != "*"),
    }
    return {
        "ok": all(required_status.values()),
        "production_ready": (
            all(required_status.values())
            and all(production_checks.values())
        ),
        "required": required_status,
        "optional": optional_status,
        "production_checks": production_checks,
        "article_fetch": {
            "timeout_seconds": _int_env("ARTICLE_FETCH_TIMEOUT_SECONDS", 20, 3, 60),
            "max_bytes": _int_env("ARTICLE_FETCH_MAX_BYTES", 5_000_000, 100_000, 20_000_000),
        },
    }


@app.get("/admin/config-status")
def config_status():
    return _env_config_status()


def _is_database_connection_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    return any(
        marker in name
        for marker in ("connect", "timeout", "network", "dns", "transport")
    )


@app.get("/admin/db-status")
def db_status():
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    audit_columns_available = True
    database_reachable = True
    error_message = None
    try:
        (
            client.table("extracted_deals")
            .select("id,reviewed_at,approved_deal_id")
            .limit(1)
            .execute()
        )
    except Exception as exc:
        audit_columns_available = False
        database_reachable = not _is_database_connection_error(exc)
        error_message = str(exc)

    return {
        "ok": audit_columns_available,
        "database_reachable": database_reachable,
        "migrations": {
            "009_review_audit": {
                "applied": audit_columns_available,
                "required_for": ["reviewed_at", "approved_deal_id"],
                "error": error_message,
            }
        },
    }


class ArticleFetchError(Exception):
    pass


_WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
_DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
_ARTICLE_CONTAINER_RE = re.compile(
    r"(article|post|story|content|entry|body|news|press|release)",
    re.IGNORECASE,
)
_JUNK_TEXT_RE = re.compile(
    r"(subscribe|sign up|cookie|privacy policy|all rights reserved|"
    r"follow us|share this|advertisement)",
    re.IGNORECASE,
)
_FUNDING_DISCOVERY_SIGNALS = (
    "raise",
    "raises",
    "raised",
    "raising",
    "secure",
    "secures",
    "secured",
    "funding",
    "fundraise",
    "series a",
    "series b",
    "series c",
    "seed round",
    "pre-seed",
    "investment",
    "venture capital",
)
_GCC_DISCOVERY_SIGNALS = (
    "uae",
    "united arab emirates",
    "dubai",
    "abu dhabi",
    "saudi arabia",
    "riyadh",
    "jeddah",
    "kuwait",
    "bahrain",
    "oman",
    "muscat",
    "qatar",
    "doha",
    "gcc",
    "gulf",
)
_DEFAULT_RSS_QUERIES = (
    "startup funding UAE",
    "startup funding Saudi Arabia",
    "startup funding Kuwait Bahrain Oman Qatar",
    "GCC venture capital funding round",
)


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _validate_ingest_url(url: str) -> str:
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    try:
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="url is malformed") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not hostname:
        raise HTTPException(
            status_code=400,
            detail="url must be an absolute http or https URL",
        )
    if parsed.username or parsed.password:
        raise HTTPException(
            status_code=400,
            detail="url must not include embedded credentials",
        )
    return cleaned


def _is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _assert_public_fetch_target(url: str) -> None:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if not hostname or hostname == "localhost" or hostname.endswith(".localhost"):
        raise ArticleFetchError("URL target must be a public internet host")

    try:
        direct_ip = ipaddress.ip_address(hostname)
        addresses = {str(direct_ip)}
    except ValueError:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(
                    hostname,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        except socket.gaierror as exc:
            raise ArticleFetchError(f"Could not resolve URL host: {hostname}") from exc

    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise ArticleFetchError("URL target must not resolve to a private or local address")


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_public_fetch_target(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _source_info_from_url(url: str) -> tuple[str, str]:
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    source_name = domain.split(".")[0].replace("-", " ").title() if domain else None
    return source_name, domain


def _fetch_article_html(url: str) -> str:
    timeout_seconds = _int_env("ARTICLE_FETCH_TIMEOUT_SECONDS", 20, 3, 60)
    max_bytes = _int_env("ARTICLE_FETCH_MAX_BYTES", 5_000_000, 100_000, 20_000_000)
    _assert_public_fetch_target(url)
    request = URLRequest(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36 GulfDealFlowBot/0.1"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
        },
    )
    try:
        opener = build_opener(_SafeRedirectHandler())
        with opener.open(request, timeout=timeout_seconds) as response:
            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                raise ArticleFetchError(f"URL did not return HTML: {content_type}")
            raw = response.read(max_bytes)
            charset = response.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except HTTPError as exc:
        raise ArticleFetchError(f"HTTP {exc.code} while fetching URL") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise ArticleFetchError(f"Could not fetch URL: {reason}") from exc
    except TimeoutError as exc:
        raise ArticleFetchError("Timed out while fetching URL") from exc


def _fetch_rss_xml(url: str) -> str:
    timeout_seconds = _int_env("ARTICLE_FETCH_TIMEOUT_SECONDS", 20, 3, 60)
    max_bytes = _int_env("ARTICLE_FETCH_MAX_BYTES", 5_000_000, 100_000, 20_000_000)
    _assert_public_fetch_target(url)
    request = URLRequest(
        url,
        headers={
            "User-Agent": "GulfDealFlowBot/0.1 (+https://gulfdealflow.com)",
            "Accept": "application/rss+xml,application/xml,text/xml",
            "Accept-Encoding": "identity",
        },
    )
    try:
        opener = build_opener(_SafeRedirectHandler())
        with opener.open(request, timeout=timeout_seconds) as response:
            raw = response.read(max_bytes)
            charset = response.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except HTTPError as exc:
        raise ArticleFetchError(f"HTTP {exc.code} while fetching RSS feed") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise ArticleFetchError(f"Could not fetch RSS feed: {reason}") from exc
    except TimeoutError as exc:
        raise ArticleFetchError("Timed out while fetching RSS feed") from exc


def _google_news_rss_url(query: str) -> str:
    return (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}&hl=en&gl=US&ceid=US:en"
    )


def _resolve_google_news_url(google_url: str) -> str:
    parsed = urlparse(google_url)
    if parsed.hostname != "news.google.com" or "/articles/" not in parsed.path:
        return google_url

    token = parsed.path.split("/articles/", 1)[1].split("/", 1)[0]
    wrapper_html = _fetch_article_html(google_url)
    timestamp_match = re.search(r'data-n-a-ts="([^"]+)"', wrapper_html)
    signature_match = re.search(r'data-n-a-sg="([^"]+)"', wrapper_html)
    if not timestamp_match or not signature_match:
        raise ArticleFetchError("Google News publisher URL metadata was not found")

    request_context = [
        [
            "en-US",
            "US",
            ["FINANCE_TOP_INDICES", "WEB_TEST_1_0_0"],
            None,
            None,
            1,
            1,
            "US:en",
            None,
            180,
            None,
            None,
            None,
            None,
            None,
            0,
            None,
            None,
            [1608992183, 723341000],
        ],
        "en-US",
        "US",
        1,
        [2, 3, 4, 8],
        1,
        0,
        "655000234",
        0,
        0,
        None,
        0,
    ]
    decode_request = [
        "garturlreq",
        request_context,
        token,
        int(timestamp_match.group(1)),
        signature_match.group(1),
    ]
    rpc_payload = [
        [
            [
                "Fbv4je",
                json.dumps(decode_request, separators=(",", ":")),
                None,
                "generic",
            ]
        ]
    ]
    body = urlencode(
        {"f.req": json.dumps(rpc_payload, separators=(",", ":"))}
    ).encode("utf-8")
    endpoint = (
        "https://news.google.com/_/DotsSplashUi/data/batchexecute?"
        "rpcids=Fbv4je"
    )
    _assert_public_fetch_target(endpoint)
    request = URLRequest(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "User-Agent": "Mozilla/5.0 GulfDealFlowBot/0.1",
        },
    )
    try:
        opener = build_opener(_SafeRedirectHandler())
        with opener.open(
            request,
            timeout=_int_env("ARTICLE_FETCH_TIMEOUT_SECONDS", 20, 3, 60),
        ) as response:
            response_text = response.read(1_000_000).decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise ArticleFetchError(
            f"HTTP {exc.code} while resolving Google News URL"
        ) from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise ArticleFetchError(
            f"Could not resolve Google News URL: {reason}"
        ) from exc
    except TimeoutError as exc:
        raise ArticleFetchError("Timed out while resolving Google News URL") from exc

    for line in response_text.splitlines():
        if not line.startswith("["):
            continue
        try:
            rows = json.loads(line)
        except json.JSONDecodeError:
            continue
        for row in rows:
            if len(row) < 3 or row[0] != "wrb.fr" or row[1] != "Fbv4je":
                continue
            decoded = json.loads(row[2])
            if (
                isinstance(decoded, list)
                and len(decoded) >= 2
                and decoded[0] == "garturlres"
            ):
                publisher_url = _validate_ingest_url(decoded[1])
                _assert_public_fetch_target(publisher_url)
                if urlparse(publisher_url).hostname == "news.google.com":
                    break
                return publisher_url
    raise ArticleFetchError("Google News publisher URL could not be decoded")


def _rss_item_text(item: ElementTree.Element, tag: str) -> str:
    node = item.find(tag)
    return (node.text or "").strip() if node is not None else ""


def _discover_rss_candidates(xml_text: str, query: str) -> list[dict]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ArticleFetchError("RSS feed returned invalid XML") from exc

    candidates = []
    for item in root.findall(".//item"):
        title = _rss_item_text(item, "title")
        url = _rss_item_text(item, "link")
        description = _rss_item_text(item, "description")
        published_raw = _rss_item_text(item, "pubDate")
        searchable = BeautifulSoup(
            f"{title} {description}",
            "html.parser",
        ).get_text(" ", strip=True).lower()
        if not title or not url:
            continue
        if not any(signal in searchable for signal in _FUNDING_DISCOVERY_SIGNALS):
            continue
        if not any(signal in searchable for signal in _GCC_DISCOVERY_SIGNALS):
            continue
        published_at = None
        if published_raw:
            try:
                published_at = parsedate_to_datetime(published_raw).isoformat()
            except (TypeError, ValueError, OverflowError):
                published_at = None
        candidates.append(
            {
                "url": url,
                "title": title,
                "query": query,
                "published_at": published_at,
            }
        )
    return candidates


def _clean_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = _WHITESPACE_RE.sub(" ", line).strip()
        if line:
            lines.append(line)
    return "\n\n".join(lines)


def _meta_content(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        tag = soup.find("meta", {"property": name}) or soup.find("meta", {"name": name})
        if tag and tag.get("content"):
            return _clean_text(tag["content"])
    return None


def _extract_title(soup: BeautifulSoup) -> str | None:
    for selector in [
        ("meta", {"property": "og:title"}),
        ("meta", {"name": "twitter:title"}),
        ("meta", {"name": "title"}),
    ]:
        tag = soup.find(*selector)
        if tag and tag.get("content"):
            return _clean_text(tag["content"])
    if soup.title and soup.title.string:
        return _clean_text(soup.title.string)
    h1 = soup.find("h1")
    if h1:
        return _clean_text(h1.get_text(" "))
    return None


def _iter_json_values(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _iter_json_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_json_values(child)
    elif isinstance(value, str):
        yield value


def _extract_json_ld_article_text(soup: BeautifulSoup) -> str | None:
    chunks = []
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw or "articleBody" not in raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for value in _iter_json_values(data):
            if len(value) > 120 and "\n" not in value:
                chunks.append(_clean_text(value))
    return "\n\n".join(dict.fromkeys(chunks)) or None


def _paragraphs_from_container(container) -> list[str]:
    paragraphs = []
    for node in container.find_all(["h2", "p", "li", "blockquote"]):
        text = _clean_text(node.get_text(" "))
        if len(text) < 25:
            continue
        if _JUNK_TEXT_RE.search(text) and len(text) < 140:
            continue
        paragraphs.append(text)
    return list(dict.fromkeys(paragraphs))


def _score_paragraphs(paragraphs: list[str]) -> int:
    return sum(len(p) for p in paragraphs) + (len(paragraphs) * 80)


def _extract_article(html: str) -> tuple[str | None, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = _extract_title(soup)
    json_ld_text = _extract_json_ld_article_text(soup)
    if json_ld_text:
        return title, json_ld_text

    for tag in soup(["script", "style", "noscript", "svg", "form", "nav", "header", "footer", "aside"]):
        tag.decompose()

    candidates = []
    for selector in [
        "article",
        "main",
        "[itemprop='articleBody']",
        "[role='main']",
    ]:
        candidates.extend(soup.select(selector))
    candidates.extend(
        soup.find_all(
            ["div", "section"],
            attrs={"class": _ARTICLE_CONTAINER_RE},
        )
    )
    candidates.extend(
        soup.find_all(
            ["div", "section"],
            attrs={"id": _ARTICLE_CONTAINER_RE},
        )
    )
    candidates.append(soup.body or soup)

    best_paragraphs = []
    best_score = 0
    seen_ids = set()
    for container in candidates:
        ident = id(container)
        if ident in seen_ids:
            continue
        seen_ids.add(ident)
        paragraphs = _paragraphs_from_container(container)
        score = _score_paragraphs(paragraphs)
        if score > best_score:
            best_score = score
            best_paragraphs = paragraphs

    if best_paragraphs:
        return title, "\n\n".join(best_paragraphs)

    description = _meta_content(
        soup,
        "og:description",
        "twitter:description",
        "description",
    )
    if description:
        return title, description

    container = soup.body or soup
    return title, _clean_text(container.get_text("\n"))


def _create_ingestion_log(
    client: Client,
    *,
    url: str,
    event: str,
    status: str,
    raw_source_id: str | None = None,
    source_page_id: str | None = None,
    message: str | None = None,
    metadata: dict | None = None,
) -> None:
    try:
        payload = {
            "raw_source_id": raw_source_id,
            "url": url,
            "event": event,
            "status": status,
            "message": message,
            "metadata": metadata,
        }
        if source_page_id is not None:
            payload["source_page_id"] = source_page_id
        client.table("ingestion_logs").insert(payload).execute()
    except Exception:
        # Ingestion should not fail just because logging failed.
        pass


def _insert_raw_source(client: Client, values: dict) -> dict:
    resp = client.table("raw_sources").insert(values).execute()
    if not resp.data:
        raise HTTPException(status_code=500, detail="raw_sources insert returned no data")
    return resp.data[0]


def _update_raw_source_row(client: Client, raw_source_id: str, values: dict) -> dict:
    resp = (
        client.table("raw_sources")
        .update(values)
        .eq("id", raw_source_id)
        .execute()
    )
    if not resp.data:
        raise HTTPException(status_code=404, detail="raw_source not found")
    return resp.data[0]


def _select_raw_source_by_url(client: Client, url: str) -> dict | None:
    rows = (
        client.table("raw_sources")
        .select("*")
        .eq("url", url)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def _select_raw_source_by_id(client: Client, raw_source_id: str) -> dict | None:
    rows = (
        client.table("raw_sources")
        .select("*")
        .eq("id", raw_source_id)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def _save_funding_source_article(client: Client, url: str) -> dict:
    existing = _select_raw_source_by_url(client, url)
    if existing and existing.get("extracted_text"):
        return existing

    source_name, domain = _source_info_from_url(url)
    html = _fetch_article_html(url)
    title, extracted_text = _extract_article(html)
    status = "fetched" if extracted_text else "extraction_failed"
    error_message = None if extracted_text else "No readable article text found"
    values = {
        "url": url,
        "source_type": "funding_announcement",
        "source_name": source_name,
        "domain": domain,
        "title": title,
        "raw_text": extracted_text,
        "extracted_text": extracted_text,
        "status": status,
        "error_message": error_message,
    }

    if existing:
        resp = (
            client.table("raw_sources")
            .update(values)
            .eq("id", existing["id"])
            .execute()
        )
        return resp.data[0] if resp.data else {**existing, **values}
    return _insert_raw_source(client, values)


_COUNTRY_HINTS = {
    "Bahrain",
    "Egypt",
    "France",
    "Jordan",
    "Kingdom of Saudi Arabia",
    "Kuwait",
    "Nigeria",
    "Oman",
    "Qatar",
    "Saudi Arabia",
    "Singapore",
    "Turkey",
    "UAE",
    "United Arab Emirates",
    "USA",
}
_SECTOR_HINTS = {
    "AI",
    "AI and Deeptech",
    "Cloud/AI",
    "Consumer / Marketplace",
    "Digital Health",
    "EdTech",
    "Enterprise SaaS",
    "FinTech",
    "Fintech",
    "Frontier Tech",
    "Growth Stage",
    "HealthTech",
    "Insurtech",
    "MedTech",
    "Platform",
    "PropTech",
    "Proptech",
    "Software / AI",
}
_PORTFOLIO_STOPWORDS = {
    "about",
    "all",
    "apply",
    "back to top",
    "blog",
    "contact",
    "exited",
    "founders",
    "home",
    "insights",
    "learn more",
    "news",
    "portfolio",
    "privacy policy",
    "team",
    "visit website",
}
_SOCIAL_DOMAINS = (
    "facebook.com",
    "forms.gle",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
)
_ASSET_EXTENSIONS = (
    ".css",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".pdf",
    ".png",
    ".svg",
    ".webp",
)


def _normalise_name(name: str) -> str:
    return _WHITESPACE_RE.sub(" ", name).strip(" -|\t\n\r")


def _dedupe_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _looks_like_company_name(name: str) -> bool:
    cleaned = _normalise_name(name)
    if not cleaned or len(cleaned) > 80:
        return False
    if cleaned in _COUNTRY_HINTS or cleaned in _SECTOR_HINTS:
        return False
    if cleaned.lower() in _PORTFOLIO_STOPWORDS:
        return False
    if len(cleaned.split()) > 5:
        return False
    if re.search(r"[$@#]|https?://|www\.", cleaned, re.IGNORECASE):
        return False
    return any(ch.isalpha() for ch in cleaned)


def _company_name_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if not host:
        return None

    path_parts = [p for p in parsed.path.split("/") if p]
    if "global.vc" in host and len(path_parts) >= 2 and path_parts[0] == "founders":
        raw = path_parts[1]
    else:
        raw = host.split(".")[0]

    if raw in {"app", "forms", "mail", "portfolio", "www"}:
        return None
    name = raw.replace("-", " ").replace("_", " ").strip()
    overrides = {
        "abyancapital": "Abyan Capital",
        "intelmatix": "Intelmatix",
        "mrsool": "Mrsool",
        "noonacademy": "Noon Academy",
        "pureharvestfarms": "Pure Harvest",
        "spidersilk": "SpiderSilk",
        "trukker": "TruKKer",
        "uselevers": "Levers",
    }
    return overrides.get(raw, name.title()) if name else None


def _extract_visible_text_and_links(html: str, page_url: str) -> tuple[str, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "form"]):
        tag.decompose()

    links = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        text = _normalise_name(anchor.get_text(" "))
        links.append({"text": text, "url": absolute})

    text = _clean_text(soup.get_text("\n"))
    return text, links


def _is_likely_portfolio_link(link: dict, page_url: str) -> bool:
    url = link["url"]
    parsed = urlparse(url)
    page_host = urlparse(page_url).netloc.lower().removeprefix("www.")
    host = parsed.netloc.lower().removeprefix("www.")
    if not host or any(domain in host for domain in _SOCIAL_DOMAINS):
        return False
    if parsed.path.lower().endswith(_ASSET_EXTENSIONS):
        return False
    if host != page_host:
        return True
    return parsed.path.startswith("/founders/") or parsed.path.startswith("/portfolio/")


def _add_candidate(candidates: dict, candidate: dict) -> None:
    name = _normalise_name(candidate.get("company_name") or "")
    if not _looks_like_company_name(name):
        return
    key = _dedupe_key(name)
    if not key:
        return
    existing = candidates.get(key)
    candidate["company_name"] = name
    if existing:
        for field in ["company_website", "sector", "geography"]:
            existing[field] = existing.get(field) or candidate.get(field)
        existing["confidence_score"] = max(
            existing.get("confidence_score", 0),
            candidate.get("confidence_score", 0),
        )
        return
    candidates[key] = candidate


def _extract_candidates_from_lines(text: str) -> list[dict]:
    lines = [_normalise_name(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    candidates: dict[str, dict] = {}
    for index, line in enumerate(lines):
        name = line.removeprefix("## ").removeprefix("# ").strip()
        if not _looks_like_company_name(name):
            continue

        nearby = lines[index + 1:index + 8]
        geography = next((item for item in nearby if item in _COUNTRY_HINTS), None)
        sector = None
        for item in nearby:
            if item == "|" or item in _COUNTRY_HINTS:
                continue
            if item.lower() not in _PORTFOLIO_STOPWORDS and len(item.split()) <= 4:
                sector = item
                break
        confidence = 0.82 if geography or sector else 0.55
        _add_candidate(candidates, {
            "company_name": name,
            "company_website": None,
            "sector": sector,
            "geography": geography,
            "extraction_method": "visible_text",
            "confidence_score": confidence,
        })
    return list(candidates.values())


def identify_portfolio_companies(text: str, links: list[dict], page_url: str) -> list[dict]:
    candidates: dict[str, dict] = {}
    for candidate in _extract_candidates_from_lines(text):
        _add_candidate(candidates, candidate)

    for link in links:
        if not _is_likely_portfolio_link(link, page_url):
            continue
        name = link["text"] if _looks_like_company_name(link["text"]) else None
        name = name or _company_name_from_url(link["url"])
        if not name:
            continue
        parsed = urlparse(link["url"])
        page_host = urlparse(page_url).netloc.lower().removeprefix("www.")
        host = parsed.netloc.lower().removeprefix("www.")
        website = link["url"] if host != page_host else None
        _add_candidate(candidates, {
            "company_name": name,
            "company_website": website,
            "sector": None,
            "geography": None,
            "extraction_method": "link_heuristic",
            "confidence_score": 0.72 if website else 0.62,
        })

    return sorted(candidates.values(), key=lambda item: item["company_name"].lower())


def _select_first(client: Client, table: str, column: str, value: str) -> dict | None:
    rows = (
        client.table(table)
        .select("*")
        .ilike(column, value)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def _get_or_create_investor(client: Client, investor_name: str) -> dict:
    existing = _select_first(client, "investors", "name", investor_name)
    if existing:
        return existing
    resp = client.table("investors").insert({"name": investor_name}).execute()
    if not resp.data:
        raise HTTPException(status_code=500, detail="investor insert returned no data")
    return resp.data[0]


def _get_or_create_portfolio_company(client: Client, candidate: dict) -> dict:
    existing = _select_first(
        client,
        "portfolio_companies",
        "name",
        candidate["company_name"],
    )
    if existing:
        updates = {}
        for source, target in [
            ("company_website", "website"),
            ("sector", "sector"),
            ("geography", "geography"),
            ("geography", "country"),
        ]:
            if candidate.get(source) and not existing.get(target):
                updates[target] = candidate[source]
        if updates:
            resp = (
                client.table("portfolio_companies")
                .update(updates)
                .eq("id", existing["id"])
                .execute()
            )
            return resp.data[0] if resp.data else {**existing, **updates}
        return existing

    resp = client.table("portfolio_companies").insert({
        "name": candidate["company_name"],
        "website": candidate.get("company_website"),
        "sector": candidate.get("sector"),
        "geography": candidate.get("geography"),
        "country": candidate.get("geography"),
    }).execute()
    if not resp.data:
        raise HTTPException(
            status_code=500,
            detail="portfolio company insert returned no data",
        )
    return resp.data[0]


def _relationship_exists(
    client: Client,
    investor_name: str,
    company_name: str,
) -> bool:
    rows = (
        client.table("investor_company_relationships")
        .select("id")
        .ilike("investor_name", investor_name)
        .ilike("company_name", company_name)
        .limit(1)
        .execute()
        .data
    )
    return bool(rows)


def _insert_source_page(
    client: Client,
    *,
    investor_id: str,
    investor_name: str,
    url: str,
    title: str | None,
    visible_text: str,
    links: list[dict],
) -> dict:
    payload = {
        "investor_id": investor_id,
        "investor_name": investor_name,
        "url": url,
        "title": title,
        "visible_text": visible_text,
        "links": links,
        "status": "fetched",
    }
    resp = client.table("source_pages").upsert(
        payload,
        on_conflict="investor_name,url",
    ).execute()
    if not resp.data:
        raise HTTPException(status_code=500, detail="source page upsert returned no data")
    return resp.data[0]


def _model_to_dict(model: BaseModel) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def extract_deal_from_text(
    raw_text: str,
    *,
    title: str | None = None,
    source_url: str | None = None,
) -> dict:
    """Extract one funding-deal candidate from article text."""
    if not raw_text or not raw_text.strip():
        raise ValueError("raw_text is empty")

    client = get_openai_client()
    model = os.environ.get("OPENAI_EXTRACTION_MODEL", "gpt-4o-mini")
    trimmed_text = raw_text.strip()[:24_000]
    title_text = title or "Unknown title"
    source_text = source_url or "Unknown source URL"

    response = client.responses.parse(
        model=model,
        input=[
            {
                "role": "system",
                "content": (
                    "You extract structured startup funding-round data from "
                    "article text. Only mark is_funding_round=true for an "
                    "actual announced startup funding round. Do not infer a "
                    "round from a VC portfolio page, investor portfolio "
                    "listing, generic company profile, M&A article, "
                    "partnership, product launch, opinion piece, or market "
                    "update. Return only facts supported by the source. If "
                    "amount is undisclosed, amount_usd must be null, "
                    "amount_original must be 'Undisclosed', and "
                    "currency_original must be null. If currency is not USD, "
                    "preserve amount_original and currency_original; estimate "
                    "amount_usd only when conversion is explicitly stated or "
                    "simple and clear. If the lead investor is not explicitly "
                    "stated, set lead_investor=null. co_investors must be an "
                    "array. confidence_score must be between 0 and 1. Use "
                    "extraction_notes to briefly explain uncertainty."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Source URL: {source_text}\n"
                    f"Title: {title_text}\n\n"
                    f"Article text:\n\n{trimmed_text}"
                ),
            },
        ],
        text_format=DealExtraction,
    )

    parsed = response.output_parsed
    if parsed is None:
        raise ValueError("OpenAI returned no parsed extraction")
    result = _model_to_dict(parsed)
    result["confidence_score"] = max(0, min(1, result["confidence_score"]))
    result["co_investors"] = result.get("co_investors") or []
    if result.get("is_funding_round") and result.get("amount_usd") is None:
        result["amount_original"] = result.get("amount_original") or "Undisclosed"
        result["currency_original"] = result.get("currency_original") or None
    return result


def _insert_extracted_deal(client: Client, values: dict) -> dict:
    resp = client.table("extracted_deals").insert(values).execute()
    if not resp.data:
        raise HTTPException(
            status_code=500,
            detail="extracted_deals insert returned no data",
        )
    return resp.data[0]


def _update_raw_source(client: Client, raw_source_id: str, values: dict) -> None:
    client.table("raw_sources").update(values).eq("id", raw_source_id).execute()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _make_ai_deal_id(company: str, stage: str | None, date: str | None) -> str:
    raw = f"{company}|{stage or ''}|{date or ''}"
    return "ai-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:14]


def _co_investors_to_text(value) -> str | None:
    if not value:
        return None
    if isinstance(value, list):
        names = [_compact_text(item) for item in value]
        return ", ".join(name for name in names if name) or None
    return _compact_text(value)


def _normalize_extracted_deal_field(field: str, value):
    if field == "co_investors":
        if not value:
            return []
        if isinstance(value, list):
            return [_compact_text(item) for item in value if _compact_text(item)]
        text = _compact_text(value)
        return [text] if text else []
    if field in {"amount_usd"}:
        return None if value is None else int(value)
    if field == "confidence_score":
        return None if value is None else round(float(value), 6)
    if field == "is_funding_round":
        return None if value is None else bool(value)
    if isinstance(value, str) or value is None:
        return _compact_text(value)
    return value


def _changed_extracted_deal_values(existing: dict, values: dict) -> dict:
    changed = {}
    for field, value in values.items():
        if _normalize_extracted_deal_field(field, existing.get(field)) != (
            _normalize_extracted_deal_field(field, value)
        ):
            changed[field] = value
    return changed


def _approval_validation_errors(extracted_deal: dict) -> list[str]:
    errors = []
    if extracted_deal.get("is_funding_round") is not True:
        errors.append("Draft must be marked as a funding round.")

    if not _compact_text(extracted_deal.get("company_name")):
        errors.append("Company name is required.")
    if not _compact_text(extracted_deal.get("country")):
        errors.append("Country is required.")
    if not _compact_text(extracted_deal.get("stage")):
        errors.append("Stage is required.")

    date = _compact_text(
        extracted_deal.get("announcement_date")
        or extracted_deal.get("announced_date")
    )
    if not date:
        errors.append("Announcement date is required.")
    elif not _DATE_RE.match(date):
        errors.append("Announcement date must use YYYY-MM-DD or YYYY-MM.")

    amount_usd = extracted_deal.get("amount_usd")
    amount_original = _compact_text(extracted_deal.get("amount_original"))
    if amount_usd is None and amount_original != "Undisclosed":
        errors.append('Use amount_original = "Undisclosed" when amount_usd is blank.')
    if amount_usd is not None and amount_usd < 0:
        errors.append("Amount USD cannot be negative.")

    confidence = extracted_deal.get("confidence_score")
    if confidence is None:
        errors.append("Confidence score is required.")
    elif confidence < 0.6:
        errors.append("Confidence score must be at least 0.6 before approval.")

    if not _compact_text(extracted_deal.get("source_url")):
        errors.append("Source URL is required.")
    return errors


def _select_extracted_deal_by_id(client: Client, extracted_deal_id: str) -> dict | None:
    rows = (
        client.table("extracted_deals")
        .select("*")
        .eq("id", extracted_deal_id)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def _require_extracted_deal_status(
    extracted_deal: dict,
    expected_status: str,
    action: str,
) -> None:
    actual_status = extracted_deal.get("status") or "needs_review"
    if actual_status != expected_status:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot {action} extracted_deal with status "
                f"'{actual_status}'. Expected '{expected_status}'."
            ),
        )


def _select_active_extraction_for_raw_source(
    client: Client,
    raw_source_id: str,
) -> dict | None:
    rows = (
        client.table("extracted_deals")
        .select("*")
        .eq("raw_source_id", raw_source_id)
        .order("created_at", desc=True)
        .execute()
        .data
    )
    for row in rows:
        if row.get("status") in {"needs_review", "approved"}:
            return row
    return None


def _update_extracted_deal(
    client: Client,
    extracted_deal_id: str,
    values: dict,
) -> dict:
    payload = {**values, "updated_at": _utc_now_iso()}
    resp = (
        client.table("extracted_deals")
        .update(payload)
        .eq("id", extracted_deal_id)
        .execute()
    )
    if not resp.data:
        raise HTTPException(status_code=404, detail="extracted_deal not found")
    return resp.data[0]


def _mark_extracted_deal_reviewed(
    client: Client,
    extracted_deal_id: str,
    *,
    status: str,
    approved_deal_id: str | None = None,
) -> dict:
    values = {
        "status": status,
        "extraction_status": status,
        "reviewed_at": _utc_now_iso(),
    }
    if approved_deal_id:
        values["approved_deal_id"] = approved_deal_id
    try:
        return _update_extracted_deal(client, extracted_deal_id, values)
    except Exception as exc:
        message = str(exc)
        missing_audit_column = (
            "reviewed_at" in message
            or "approved_deal_id" in message
            or "schema cache" in message
        )
        if not missing_audit_column:
            raise
        # Migration 009 may not be applied yet. Keep the review action working
        # and retry with the original status-only contract.
        return _update_extracted_deal(
            client,
            extracted_deal_id,
            {"status": status, "extraction_status": status},
        )


def _reopen_extracted_deal(client: Client, extracted_deal_id: str) -> dict:
    values = {
        "status": "needs_review",
        "extraction_status": "needs_review",
        "reviewed_at": None,
        "approved_deal_id": None,
    }
    try:
        return _update_extracted_deal(client, extracted_deal_id, values)
    except Exception as exc:
        message = str(exc)
        missing_audit_column = (
            "reviewed_at" in message
            or "approved_deal_id" in message
            or "schema cache" in message
        )
        if not missing_audit_column:
            raise
        return _update_extracted_deal(
            client,
            extracted_deal_id,
            {"status": "needs_review", "extraction_status": "needs_review"},
        )


def _deal_payload_from_extracted(extracted_deal: dict) -> dict:
    errors = _approval_validation_errors(extracted_deal)
    if errors:
        raise HTTPException(
            status_code=400,
            detail={"message": "Draft is not ready for approval", "errors": errors},
        )

    company = _compact_text(extracted_deal.get("company_name"))
    stage = _compact_text(extracted_deal.get("stage"))
    date = _compact_text(
        extracted_deal.get("announcement_date")
        or extracted_deal.get("announced_date")
    )
    amount_usd = extracted_deal.get("amount_usd")
    sector = _compact_text(
        extracted_deal.get("sub_sector") or extracted_deal.get("sector")
    )
    notes = _compact_text(extracted_deal.get("extraction_notes"))
    source_url = _compact_text(extracted_deal.get("source_url"))

    return {
        "deal_id": _make_ai_deal_id(company, stage, date),
        "company_name": company,
        "country": _compact_text(extracted_deal.get("country")),
        "date": date,
        "stage": stage,
        "amount_usd": amount_usd,
        "disclosed": amount_usd is not None,
        "sector": sector,
        "description": notes,
        "website": _compact_text(extracted_deal.get("website")),
        "lead_investor": _compact_text(extracted_deal.get("lead_investor")),
        "co_investors": _co_investors_to_text(extracted_deal.get("co_investors")),
        "source": source_url,
        "notes": notes,
    }


def _find_existing_deal(client: Client, payload: dict) -> dict | None:
    if not payload.get("company_name") or not payload.get("date"):
        return None
    query = (
        client.table("deals")
        .select("*")
        .eq("company_name", payload["company_name"])
        .eq("date", payload["date"])
    )
    if payload.get("stage"):
        query = query.eq("stage", payload["stage"])
    rows = query.limit(1).execute().data
    return rows[0] if rows else None


def _select_deal_by_id(client: Client, deal_id: str) -> dict | None:
    rows = (
        client.table("deals")
        .select("*")
        .eq("deal_id", deal_id)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def _draft_duplicate_key(row: dict) -> tuple | None:
    raw_source_id = _compact_text(row.get("raw_source_id"))
    if raw_source_id:
        return ("raw_source", raw_source_id)
    company = _compact_text(row.get("company_name"))
    if not company:
        return None
    return (
        "deal",
        company.lower(),
        (_compact_text(row.get("stage")) or "").lower(),
        _compact_text(row.get("announcement_date") or row.get("announced_date")) or "",
        _compact_text(row.get("source_url")) or "",
    )


def _extracted_deal_search_text(row: dict) -> str:
    co_investors = row.get("co_investors")
    if isinstance(co_investors, list):
        co_investors_text = " ".join(str(item) for item in co_investors)
    else:
        co_investors_text = str(co_investors or "")
    parts = [
        row.get("company_name"),
        row.get("country"),
        row.get("stage"),
        row.get("sector"),
        row.get("sub_sector"),
        row.get("lead_investor"),
        co_investors_text,
        row.get("website"),
        row.get("source_url"),
        row.get("extraction_notes"),
    ]
    return " ".join(str(part) for part in parts if part).lower()


def _extracted_deal_readiness(row: dict) -> str:
    if row.get("is_funding_round") is False:
        return "non_funding"
    return "ready" if not _approval_validation_errors(row) else "needs_fix"


def _select_relationship_by_id(client: Client, relationship_id: str) -> dict | None:
    rows = (
        client.table("investor_company_relationships")
        .select("*")
        .eq("id", relationship_id)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


@app.post("/ingest/url", status_code=201)
def ingest_url(payload: IngestUrlRequest, response: Response):
    url = _validate_ingest_url(payload.url)
    source_name, domain = _source_info_from_url(url)
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    existing = _select_raw_source_by_url(client, url)
    promote_candidate = bool(
        existing
        and existing.get("source_type") == "rss_candidate"
        and existing.get("status") in {"pending", "fetch_failed"}
        and not (existing.get("raw_text") or existing.get("extracted_text"))
    )
    if existing and not promote_candidate:
        response.headers["X-GDF-Duplicate"] = "true"
        response.headers["X-GDF-Raw-Source-Id"] = str(existing.get("id") or "")
        response.headers["X-GDF-Fetch-Status"] = (
            "fetched" if (existing.get("raw_text") or existing.get("extracted_text")) else "failed"
        )
        _create_ingestion_log(
            client,
            raw_source_id=existing.get("id"),
            url=url,
            event="duplicate_url",
            status="skipped",
            message="URL already exists in raw_sources",
        )
        response.status_code = 200
        return existing

    try:
        html = _fetch_article_html(url)
        title, raw_text = _extract_article(html)
        if not raw_text:
            raise ArticleFetchError("No readable article text found")
    except ArticleFetchError as exc:
        values = {
            "url": url,
            "source_type": existing.get("source_type") if promote_candidate else "url",
            "source_name": source_name,
            "domain": domain,
            "status": "fetch_failed",
            "error_message": str(exc),
        }
        row = (
            _update_raw_source_row(client, existing["id"], values)
            if promote_candidate
            else _insert_raw_source(client, values)
        )
        _create_ingestion_log(
            client,
            raw_source_id=row.get("id"),
            url=url,
            event="fetch_article",
            status="failed",
            message=str(exc),
        )
        response.headers["X-GDF-Duplicate"] = "false"
        response.headers["X-GDF-Raw-Source-Id"] = str(row.get("id") or "")
        response.headers["X-GDF-Fetch-Status"] = "failed"
        return row

    values = {
        "url": url,
        "source_type": existing.get("source_type") if promote_candidate else "url",
        "source_name": source_name,
        "domain": domain,
        "title": title,
        "raw_text": raw_text,
        "extracted_text": raw_text,
        "status": "fetched",
        "error_message": None,
    }
    row = (
        _update_raw_source_row(client, existing["id"], values)
        if promote_candidate
        else _insert_raw_source(client, values)
    )
    _create_ingestion_log(
        client,
        raw_source_id=row.get("id"),
        url=url,
        event="ingest_url",
        status="fetched",
        message="Fetched and saved raw source",
        metadata={"domain": domain, "text_length": len(raw_text)},
    )
    response.headers["X-GDF-Duplicate"] = "false"
    response.headers["X-GDF-Raw-Source-Id"] = str(row.get("id") or "")
    response.headers["X-GDF-Fetch-Status"] = "fetched"
    return row


def _run_rss_discovery(client: Client, queries: list[str], limit: int) -> dict:
    discovered = []
    seen_urls = set()
    query_errors = []
    resolution_errors = []
    for query in queries:
        try:
            xml_text = _fetch_rss_xml(_google_news_rss_url(query))
            candidates = _discover_rss_candidates(xml_text, query)
        except ArticleFetchError as exc:
            query_errors.append({"query": query, "error": str(exc)})
            continue
        for candidate in candidates:
            if len(discovered) >= limit:
                break
            google_news_url = candidate["url"]
            try:
                url = _resolve_google_news_url(google_news_url)
            except ArticleFetchError as exc:
                resolution_errors.append(
                    {
                        "title": candidate["title"],
                        "url": google_news_url,
                        "error": str(exc),
                    }
                )
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            if _select_raw_source_by_url(client, url):
                continue
            row = _insert_raw_source(
                client,
                {
                    "url": url,
                    "source_type": "rss_candidate",
                    "source_name": "Google News",
                    "domain": urlparse(url).netloc.lower().removeprefix("www."),
                    "title": candidate["title"],
                    "status": "pending",
                    "error_message": None,
                },
            )
            discovered.append({**row, "query": query, "published_at": candidate["published_at"]})
            _create_ingestion_log(
                client,
                raw_source_id=row.get("id"),
                url=url,
                event="discover_rss",
                status="pending",
                message="Discovered funding article candidate",
                metadata={
                    "query": query,
                    "published_at": candidate["published_at"],
                    "google_news_url": google_news_url,
                },
            )
        if len(discovered) >= limit:
            break

    return {
        "query_count": len(queries),
        "discovered_count": len(discovered),
        "query_errors": query_errors,
        "resolution_errors": resolution_errors,
        "candidates": discovered,
    }


@app.post("/discover/rss")
def discover_rss(payload: RssDiscoveryRequest):
    queries = payload.queries or list(_DEFAULT_RSS_QUERIES)
    queries = list(dict.fromkeys(query.strip() for query in queries if query.strip()))
    if not queries:
        raise HTTPException(status_code=400, detail="At least one RSS query is required")
    if len(queries) > 12:
        raise HTTPException(status_code=400, detail="A maximum of 12 RSS queries is allowed")

    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _run_rss_discovery(client, queries, payload.limit)


@app.get("/cron/discover")
def cron_discover(request: Request):
    cron_secret = os.environ.get("CRON_SECRET")
    if (
        not cron_secret
        or request.headers.get("authorization") != f"Bearer {cron_secret}"
    ):
        raise HTTPException(status_code=401, detail="Invalid cron authorization")
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _run_rss_discovery(
        client,
        list(_DEFAULT_RSS_QUERIES),
        100,
    )


@app.post("/ingest/portfolio-page", status_code=201)
def ingest_portfolio_page(payload: PortfolioPageRequest):
    investor_name = _normalise_name(payload.investor_name)
    url = _validate_ingest_url(payload.url)
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    investor = _get_or_create_investor(client, investor_name)
    try:
        html = _fetch_article_html(url)
        soup = BeautifulSoup(html, "html.parser")
        title = _extract_title(soup)
        visible_text, links = _extract_visible_text_and_links(html, url)
        source_page = _insert_source_page(
            client,
            investor_id=investor["id"],
            investor_name=investor_name,
            url=url,
            title=title,
            visible_text=visible_text,
            links=links,
        )
    except ArticleFetchError as exc:
        _create_ingestion_log(
            client,
            url=url,
            event="ingest_portfolio_page",
            status="failed",
            message=str(exc),
            metadata={"investor_name": investor_name},
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    candidates = identify_portfolio_companies(visible_text, links, url)
    created_relationships = []
    skipped_duplicates = []

    for candidate in candidates:
        if _relationship_exists(client, investor_name, candidate["company_name"]):
            skipped_duplicates.append(candidate)
            continue

        company = _get_or_create_portfolio_company(client, candidate)
        relationship_payload = {
            "investor_id": investor["id"],
            "company_id": company["id"],
            "source_page_id": source_page["id"],
            "investor_name": investor_name,
            "company_name": candidate["company_name"],
            "company_website": candidate.get("company_website"),
            "sector": candidate.get("sector"),
            "geography": candidate.get("geography"),
            "country": candidate.get("geography"),
            "source_url": url,
            "extraction_method": candidate.get("extraction_method"),
            "confidence_score": candidate.get("confidence_score"),
            "status": "needs_review",
        }
        resp = (
            client.table("investor_company_relationships")
            .insert(relationship_payload)
            .execute()
        )
        if resp.data:
            created_relationships.append(resp.data[0])

    _create_ingestion_log(
        client,
        source_page_id=source_page["id"],
        url=url,
        event="ingest_portfolio_page",
        status="completed",
        message=(
            f"Created {len(created_relationships)} draft relationships; "
            f"skipped {len(skipped_duplicates)} duplicates"
        ),
        metadata={
            "investor_name": investor_name,
            "candidate_count": len(candidates),
            "created_count": len(created_relationships),
            "skipped_duplicate_count": len(skipped_duplicates),
        },
    )
    return {
        "source_page": source_page,
        "candidate_count": len(candidates),
        "created_count": len(created_relationships),
        "skipped_duplicate_count": len(skipped_duplicates),
        "relationships": created_relationships,
        "skipped_duplicates": skipped_duplicates,
    }


@app.post("/relationships/{relationship_id}/enrich-funding", status_code=201)
def enrich_relationship_funding(
    relationship_id: str,
    payload: FundingEnrichmentRequest,
):
    source_url = _validate_ingest_url(payload.source_url)
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    relationship = _select_relationship_by_id(client, relationship_id)
    if not relationship:
        raise HTTPException(status_code=404, detail="relationship not found")
    if relationship.get("status") != "approved":
        raise HTTPException(
            status_code=400,
            detail="relationship must be approved before funding enrichment",
        )

    _create_ingestion_log(
        client,
        url=source_url,
        event="enrich_relationship_funding",
        status="started",
        metadata={
            "relationship_id": relationship_id,
            "investor_name": relationship.get("investor_name"),
            "company_name": relationship.get("company_name"),
        },
    )

    try:
        raw_source = _save_funding_source_article(client, source_url)
    except ArticleFetchError as exc:
        _create_ingestion_log(
            client,
            url=source_url,
            event="enrich_relationship_funding",
            status="failed",
            message=str(exc),
            metadata={"relationship_id": relationship_id},
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    raw_text = raw_source.get("raw_text") or raw_source.get("extracted_text") or ""
    if not raw_text.strip():
        message = "funding source has no raw_text to process"
        _create_ingestion_log(
            client,
            raw_source_id=raw_source.get("id"),
            url=source_url,
            event="enrich_relationship_funding",
            status="failed",
            message=message,
            metadata={"relationship_id": relationship_id},
        )
        raise HTTPException(status_code=400, detail=message)

    try:
        extraction = extract_deal_from_text(
            raw_text,
            title=raw_source.get("title"),
            source_url=source_url,
        )
    except Exception as exc:
        message = f"AI extraction failed: {exc}"
        _update_raw_source(
            client,
            raw_source["id"],
            {"status": "extraction_failed", "error_message": message},
        )
        _create_ingestion_log(
            client,
            raw_source_id=raw_source.get("id"),
            url=source_url,
            event="enrich_relationship_funding",
            status="failed",
            message=message,
            metadata={"relationship_id": relationship_id},
        )
        raise HTTPException(status_code=502, detail=message) from exc

    co_investors = extraction.get("co_investors") or []
    extracted_deal = _insert_extracted_deal(client, {
        "raw_source_id": raw_source.get("id"),
        "investor_company_relationship_id": relationship_id,
        "source_url": source_url,
        "company_name": extraction.get("company_name")
        or relationship.get("company_name"),
        "country": extraction.get("country") or relationship.get("country"),
        "stage": extraction.get("stage"),
        "amount_usd": extraction.get("amount_usd"),
        "amount_original": extraction.get("amount_original"),
        "currency_original": extraction.get("currency_original"),
        "announcement_date": extraction.get("announcement_date"),
        "announced_date": extraction.get("announcement_date"),
        "sector": extraction.get("sector") or relationship.get("sector"),
        "sub_sector": extraction.get("sub_sector"),
        "lead_investor": extraction.get("lead_investor")
        or relationship.get("investor_name"),
        "co_investors": co_investors,
        "website": extraction.get("website")
        or relationship.get("company_website"),
        "is_funding_round": extraction.get("is_funding_round"),
        "confidence_score": extraction.get("confidence_score"),
        "extraction_notes": extraction.get("extraction_notes"),
        "status": "needs_review",
        "extraction_status": "needs_review",
        "extraction_payload": extraction,
    })
    _update_raw_source(
        client,
        raw_source["id"],
        {"status": "extracted", "error_message": None},
    )
    _create_ingestion_log(
        client,
        raw_source_id=raw_source.get("id"),
        url=source_url,
        event="enrich_relationship_funding",
        status="needs_review",
        message="Funding extraction saved as needs_review",
        metadata={
            "relationship_id": relationship_id,
            "extracted_deal_id": extracted_deal.get("id"),
            "is_funding_round": extraction.get("is_funding_round"),
            "confidence_score": extraction.get("confidence_score"),
        },
    )
    return {
        "relationship": relationship,
        "raw_source": raw_source,
        "extracted_deal": extracted_deal,
    }


@app.post("/ingest/extract/{raw_source_id}", status_code=201)
def ingest_extract(raw_source_id: str, response: Response):
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    raw_rows = (
        client.table("raw_sources")
        .select("*")
        .eq("id", raw_source_id)
        .limit(1)
        .execute()
        .data
    )
    if not raw_rows:
        raise HTTPException(status_code=404, detail="raw_source not found")

    raw_source = raw_rows[0]
    url = raw_source.get("url") or ""

    existing_extraction = _select_active_extraction_for_raw_source(
        client,
        raw_source_id,
    )
    if existing_extraction:
        response.headers["X-GDF-Existing-Extraction"] = "true"
        response.headers["X-GDF-Extracted-Deal-Id"] = str(existing_extraction.get("id") or "")
        _create_ingestion_log(
            client,
            raw_source_id=raw_source_id,
            url=url,
            event="extract_deal",
            status="skipped_duplicate",
            message="Active extracted_deals row already exists for raw_source",
            metadata={"extracted_deal_id": existing_extraction.get("id")},
        )
        response.status_code = 200
        return existing_extraction

    raw_text = raw_source.get("raw_text") or raw_source.get("extracted_text") or ""
    if not raw_text.strip():
        message = "raw_source has no raw_text to process"
        _update_raw_source(
            client,
            raw_source_id,
            {"status": "extraction_failed", "error_message": message},
        )
        _create_ingestion_log(
            client,
            raw_source_id=raw_source_id,
            url=url,
            event="extract_deal",
            status="failed",
            message=message,
        )
        raise HTTPException(status_code=400, detail=message)

    _create_ingestion_log(
        client,
        raw_source_id=raw_source_id,
        url=url,
        event="extract_deal",
        status="started",
        metadata={"text_length": len(raw_text)},
    )

    try:
        extraction = extract_deal_from_text(
            raw_text,
            title=raw_source.get("title"),
            source_url=url,
        )
    except Exception as exc:
        message = f"AI extraction failed: {exc}"
        _update_raw_source(
            client,
            raw_source_id,
            {"status": "extraction_failed", "error_message": message},
        )
        _create_ingestion_log(
            client,
            raw_source_id=raw_source_id,
            url=url,
            event="extract_deal",
            status="failed",
            message=message,
        )
        raise HTTPException(status_code=502, detail=message) from exc

    extraction_status = (
        "extracted" if extraction["is_funding_round"] else "not_a_funding_round"
    )
    co_investors = extraction.get("co_investors") or []
    try:
        extracted_deal = _insert_extracted_deal(client, {
            "raw_source_id": raw_source_id,
            "source_url": url,
            "company_name": extraction.get("company_name"),
            "country": extraction.get("country"),
            "stage": extraction.get("stage"),
            "amount_usd": extraction.get("amount_usd"),
            "amount_original": extraction.get("amount_original"),
            "currency_original": extraction.get("currency_original"),
            "announcement_date": extraction.get("announcement_date"),
            "announced_date": extraction.get("announcement_date"),
            "sector": extraction.get("sector"),
            "sub_sector": extraction.get("sub_sector"),
            "lead_investor": extraction.get("lead_investor"),
            "co_investors": co_investors,
            "website": extraction.get("website"),
            "is_funding_round": extraction.get("is_funding_round"),
            "confidence_score": extraction.get("confidence_score"),
            "extraction_notes": extraction.get("extraction_notes"),
            "status": "needs_review",
            "extraction_status": "needs_review",
            "extraction_payload": extraction,
        })
        _update_raw_source(
            client,
            raw_source_id,
            {"status": extraction_status, "error_message": None},
        )
    except Exception as exc:
        message = f"Saving extraction failed: {exc}"
        _update_raw_source(
            client,
            raw_source_id,
            {"status": "extraction_failed", "error_message": message},
        )
        _create_ingestion_log(
            client,
            raw_source_id=raw_source_id,
            url=url,
            event="save_extraction",
            status="failed",
            message=message,
            metadata=extraction,
        )
        raise HTTPException(status_code=500, detail=message) from exc

    _create_ingestion_log(
        client,
        raw_source_id=raw_source_id,
        url=url,
        event="extract_deal",
        status=extraction_status,
        message=extraction.get("extraction_notes"),
        metadata={
            "extracted_deal_id": extracted_deal.get("id"),
            "confidence_score": extraction.get("confidence_score"),
            "is_funding_round": extraction.get("is_funding_round"),
        },
    )
    response.headers["X-GDF-Existing-Extraction"] = "false"
    response.headers["X-GDF-Extracted-Deal-Id"] = str(extracted_deal.get("id") or "")
    return extracted_deal


@app.get("/raw-sources/{raw_source_id}")
def get_raw_source(raw_source_id: str):
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    rows = (
        client.table("raw_sources")
        .select("*")
        .eq("id", raw_source_id)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        raise HTTPException(status_code=404, detail="raw_source not found")
    return rows[0]


@app.get("/raw-sources")
def list_raw_sources(
    status: str | None = Query(None, max_length=40),
    source_type: str | None = Query(None, max_length=80),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    query = client.table("raw_sources").select("*")
    if status:
        statuses = list(dict.fromkeys(item.strip() for item in status.split(",") if item.strip()))
        query = query.eq("status", statuses[0]) if len(statuses) == 1 else query.in_("status", statuses)
    if source_type:
        query = query.eq("source_type", source_type)
    rows = (
        query.order("created_at", desc=True)
        .range(offset, offset + limit)
        .execute()
        .data
    )
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    return {
        "count": len(page_rows),
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "next_offset": offset + len(page_rows) if has_more else None,
        "raw_sources": page_rows,
    }


@app.post("/raw-sources/{raw_source_id}/refetch")
def refetch_raw_source(raw_source_id: str, response: Response):
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    raw_source = _select_raw_source_by_id(client, raw_source_id)
    if not raw_source:
        raise HTTPException(status_code=404, detail="raw_source not found")

    url = raw_source.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="raw_source has no url")

    try:
        html = _fetch_article_html(url)
        title, raw_text = _extract_article(html)
        if not raw_text:
            raise ArticleFetchError("No readable article text found")
    except ArticleFetchError as exc:
        updated = (
            client.table("raw_sources")
            .update({"status": "fetch_failed", "error_message": str(exc)})
            .eq("id", raw_source_id)
            .execute()
            .data
        )
        _create_ingestion_log(
            client,
            raw_source_id=raw_source_id,
            url=url,
            event="refetch_raw_source",
            status="failed",
            message=str(exc),
        )
        response.headers["X-GDF-Fetch-Status"] = "failed"
        return updated[0] if updated else {**raw_source, "status": "fetch_failed"}

    source_name, domain = _source_info_from_url(url)
    values = {
        "source_name": source_name,
        "domain": domain,
        "title": title,
        "raw_text": raw_text,
        "extracted_text": raw_text,
        "status": "fetched",
        "error_message": None,
    }
    updated = (
        client.table("raw_sources")
        .update(values)
        .eq("id", raw_source_id)
        .execute()
        .data
    )
    row = updated[0] if updated else {**raw_source, **values}
    _create_ingestion_log(
        client,
        raw_source_id=raw_source_id,
        url=url,
        event="refetch_raw_source",
        status="fetched",
        message="Refetched and refreshed raw source text",
        metadata={"text_length": len(raw_text), "previous_text_length": len(raw_source.get("raw_text") or "")},
    )
    response.headers["X-GDF-Fetch-Status"] = "fetched"
    return row


@app.get("/ingestion-logs")
def list_ingestion_logs(
    raw_source_id: str | None = Query(None),
    url: str | None = Query(None, max_length=2048),
    limit: int = Query(25, ge=1, le=100),
):
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    query = client.table("ingestion_logs").select("*")
    if raw_source_id:
        query = query.eq("raw_source_id", raw_source_id)
    if url:
        query = query.eq("url", url)
    rows = (
        query.order("created_at", desc=True)
        .range(0, limit - 1)
        .execute()
        .data
    )
    return {"count": len(rows), "logs": rows}


@app.get("/extracted-deals")
def list_extracted_deals(
    status: str | None = Query("needs_review", description="Review status filter"),
    q: str | None = Query(
        None,
        max_length=120,
        description="Search company, investor, sector, source, or notes",
    ),
    readiness: str = Query(
        "all",
        pattern="^(all|ready|needs_fix|non_funding)$",
        description="Approval readiness filter",
    ),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    query = client.table("extracted_deals").select("*")
    if status:
        query = query.eq("status", status)
    cleaned_q = _compact_text(q)
    if cleaned_q or readiness != "all":
        rows = (
            query.order("created_at", desc=True)
            .range(0, 4999)
            .execute()
            .data
        )
        matched_rows = rows
        if cleaned_q:
            needle = cleaned_q.lower()
            matched_rows = [
                row for row in matched_rows
                if needle in _extracted_deal_search_text(row)
            ]
        if readiness != "all":
            matched_rows = [
                row for row in matched_rows
                if _extracted_deal_readiness(row) == readiness
            ]
        page_rows = matched_rows[offset:offset + limit]
        next_offset = offset + len(page_rows)
        has_more = next_offset < len(matched_rows)
        return {
            "count": len(page_rows),
            "total_matched": len(matched_rows),
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
            "extracted_deals": page_rows,
        }
    rows = (
        query.order("created_at", desc=True)
        .range(offset, offset + limit)
        .execute()
        .data
    )
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    return {
        "count": len(page_rows),
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "next_offset": offset + len(page_rows) if has_more else None,
        "extracted_deals": page_rows,
    }


@app.get("/extracted-deals/stats")
def extracted_deal_stats():
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    rows = (
        client.table("extracted_deals")
        .select(
            "id,status,company_name,country,stage,amount_usd,amount_original,"
            "announcement_date,announced_date,confidence_score,is_funding_round,"
            "source_url"
        )
        .range(0, 4999)
        .execute()
        .data
    )
    by_status = {"needs_review": 0, "approved": 0, "rejected": 0}
    by_readiness = {
        "needs_review": {"all": 0, "ready": 0, "needs_fix": 0, "non_funding": 0},
        "approved": {"all": 0, "ready": 0, "needs_fix": 0, "non_funding": 0},
        "rejected": {"all": 0, "ready": 0, "needs_fix": 0, "non_funding": 0},
    }
    funding_rounds = 0
    non_funding_rounds = 0
    confidence_scores = []
    for row in rows:
        status = row.get("status") or "needs_review"
        by_status[status] = by_status.get(status, 0) + 1
        readiness = _extracted_deal_readiness(row)
        if status not in by_readiness:
            by_readiness[status] = {
                "all": 0,
                "ready": 0,
                "needs_fix": 0,
                "non_funding": 0,
            }
        by_readiness[status]["all"] += 1
        by_readiness[status][readiness] += 1
        if row.get("is_funding_round") is True:
            funding_rounds += 1
        elif row.get("is_funding_round") is False:
            non_funding_rounds += 1
        if row.get("confidence_score") is not None:
            confidence_scores.append(float(row["confidence_score"]))

    avg_confidence = (
        round(sum(confidence_scores) / len(confidence_scores), 3)
        if confidence_scores
        else None
    )
    return {
        "total": len(rows),
        "by_status": by_status,
        "by_readiness": by_readiness,
        "funding_rounds": funding_rounds,
        "non_funding_rounds": non_funding_rounds,
        "avg_confidence": avg_confidence,
    }


@app.get("/extracted-deals/{extracted_deal_id}/validation")
def validate_extracted_deal(extracted_deal_id: str):
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    extracted_deal = _select_extracted_deal_by_id(client, extracted_deal_id)
    if not extracted_deal:
        raise HTTPException(status_code=404, detail="extracted_deal not found")
    errors = _approval_validation_errors(extracted_deal)
    return {"ready": len(errors) == 0, "errors": errors}


@app.get("/extracted-deals/{extracted_deal_id}/approval-preview")
def approval_preview(extracted_deal_id: str):
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    extracted_deal = _select_extracted_deal_by_id(client, extracted_deal_id)
    if not extracted_deal:
        raise HTTPException(status_code=404, detail="extracted_deal not found")

    errors = _approval_validation_errors(extracted_deal)
    if errors:
        return {
            "ready": False,
            "errors": errors,
            "deal_payload": None,
            "existing_deal": None,
            "will_insert": False,
        }

    payload = _deal_payload_from_extracted(extracted_deal)
    existing = _find_existing_deal(client, payload)
    return {
        "ready": True,
        "errors": [],
        "deal_payload": payload,
        "existing_deal": existing,
        "will_insert": existing is None,
    }


@app.post("/extracted-deals/actions/reject-non-funding")
def reject_non_funding_drafts():
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    rows = (
        client.table("extracted_deals")
        .select("*")
        .eq("status", "needs_review")
        .eq("is_funding_round", False)
        .execute()
        .data
    )
    rejected = []
    for row in rows:
        reviewed = _mark_extracted_deal_reviewed(
            client,
            row["id"],
            status="rejected",
        )
        rejected.append(reviewed)
        _create_ingestion_log(
            client,
            raw_source_id=row.get("raw_source_id"),
            url=row.get("source_url") or "",
            event="reject_non_funding_drafts",
            status="rejected",
            message="Rejected non-funding draft",
            metadata={"extracted_deal_id": row.get("id")},
        )
    return {"rejected_count": len(rejected), "rejected": rejected}


@app.post("/extracted-deals/actions/reject-duplicates")
def reject_duplicate_drafts():
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    rows = (
        client.table("extracted_deals")
        .select("*")
        .eq("status", "needs_review")
        .order("created_at", desc=True)
        .execute()
        .data
    )
    seen = set()
    rejected = []
    for row in rows:
        key = _draft_duplicate_key(row)
        if key is None:
            continue
        if key not in seen:
            seen.add(key)
            continue
        reviewed = _mark_extracted_deal_reviewed(
            client,
            row["id"],
            status="rejected",
        )
        rejected.append(reviewed)
        _create_ingestion_log(
            client,
            raw_source_id=row.get("raw_source_id"),
            url=row.get("source_url") or "",
            event="reject_duplicate_drafts",
            status="rejected",
            message="Rejected duplicate draft",
            metadata={"extracted_deal_id": row.get("id"), "duplicate_key": list(key)},
        )
    return {"rejected_count": len(rejected), "rejected": rejected}


@app.patch("/extracted-deals/{extracted_deal_id}")
def update_extracted_deal(
    extracted_deal_id: str,
    payload: ExtractedDealUpdate,
):
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if hasattr(payload, "model_dump"):
        values = payload.model_dump(exclude_unset=True)
    else:
        values = payload.dict(exclude_unset=True)
    deal = _select_extracted_deal_by_id(client, extracted_deal_id)
    if not deal:
        raise HTTPException(status_code=404, detail="extracted_deal not found")
    _require_extracted_deal_status(deal, "needs_review", "update")
    if "confidence_score" in values:
        score = values["confidence_score"]
        values["confidence_score"] = None if score is None else max(0, min(1, score))
    changed_values = _changed_extracted_deal_values(deal, values)
    if not changed_values:
        return deal
    updated = _update_extracted_deal(client, extracted_deal_id, changed_values)
    _create_ingestion_log(
        client,
        raw_source_id=deal.get("raw_source_id"),
        url=deal.get("source_url") or "",
        event="update_extracted_deal",
        status="needs_review",
        message="Updated extracted draft fields",
        metadata={
            "extracted_deal_id": extracted_deal_id,
            "changed_fields": sorted(changed_values.keys()),
        },
    )
    return updated


@app.post("/extracted-deals/{extracted_deal_id}/reextract")
def reextract_extracted_deal(extracted_deal_id: str):
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    extracted_deal = _select_extracted_deal_by_id(client, extracted_deal_id)
    if not extracted_deal:
        raise HTTPException(status_code=404, detail="extracted_deal not found")
    _require_extracted_deal_status(extracted_deal, "needs_review", "re-extract")

    raw_source_id = extracted_deal.get("raw_source_id")
    if not raw_source_id:
        raise HTTPException(status_code=400, detail="extracted_deal has no raw_source_id")

    raw_source = _select_raw_source_by_id(client, raw_source_id)
    if not raw_source:
        raise HTTPException(status_code=404, detail="raw_source not found")

    raw_text = raw_source.get("raw_text") or raw_source.get("extracted_text") or ""
    url = raw_source.get("url") or extracted_deal.get("source_url") or ""
    if not raw_text.strip():
        message = "raw_source has no raw_text to process"
        _create_ingestion_log(
            client,
            raw_source_id=raw_source_id,
            url=url,
            event="reextract_deal",
            status="failed",
            message=message,
            metadata={"extracted_deal_id": extracted_deal_id},
        )
        raise HTTPException(status_code=400, detail=message)

    _create_ingestion_log(
        client,
        raw_source_id=raw_source_id,
        url=url,
        event="reextract_deal",
        status="started",
        metadata={"extracted_deal_id": extracted_deal_id, "text_length": len(raw_text)},
    )

    try:
        extraction = extract_deal_from_text(
            raw_text,
            title=raw_source.get("title"),
            source_url=url,
        )
    except Exception as exc:
        message = f"AI re-extraction failed: {exc}"
        _update_raw_source(
            client,
            raw_source_id,
            {"status": "extraction_failed", "error_message": message},
        )
        _create_ingestion_log(
            client,
            raw_source_id=raw_source_id,
            url=url,
            event="reextract_deal",
            status="failed",
            message=message,
            metadata={"extracted_deal_id": extracted_deal_id},
        )
        raise HTTPException(status_code=502, detail=message) from exc

    extraction_status = (
        "extracted" if extraction["is_funding_round"] else "not_a_funding_round"
    )
    values = {
        "source_url": url,
        "company_name": extraction.get("company_name"),
        "country": extraction.get("country"),
        "stage": extraction.get("stage"),
        "amount_usd": extraction.get("amount_usd"),
        "amount_original": extraction.get("amount_original"),
        "currency_original": extraction.get("currency_original"),
        "announcement_date": extraction.get("announcement_date"),
        "announced_date": extraction.get("announcement_date"),
        "sector": extraction.get("sector"),
        "sub_sector": extraction.get("sub_sector"),
        "lead_investor": extraction.get("lead_investor"),
        "co_investors": extraction.get("co_investors") or [],
        "website": extraction.get("website"),
        "is_funding_round": extraction.get("is_funding_round"),
        "confidence_score": extraction.get("confidence_score"),
        "extraction_notes": extraction.get("extraction_notes"),
        "status": "needs_review",
        "extraction_status": "needs_review",
        "extraction_payload": extraction,
    }
    updated = _update_extracted_deal(client, extracted_deal_id, values)
    _update_raw_source(
        client,
        raw_source_id,
        {"status": extraction_status, "error_message": None},
    )
    _create_ingestion_log(
        client,
        raw_source_id=raw_source_id,
        url=url,
        event="reextract_deal",
        status=extraction_status,
        message=extraction.get("extraction_notes"),
        metadata={
            "extracted_deal_id": extracted_deal_id,
            "confidence_score": extraction.get("confidence_score"),
            "is_funding_round": extraction.get("is_funding_round"),
        },
    )
    return updated


@app.post("/extracted-deals/{extracted_deal_id}/approve", status_code=201)
def approve_extracted_deal(
    extracted_deal_id: str,
    response: Response,
    review_payload: ReviewApprovePayload | None = None,
):
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    extracted_deal = _select_extracted_deal_by_id(client, extracted_deal_id)
    if not extracted_deal:
        raise HTTPException(status_code=404, detail="extracted_deal not found")
    if extracted_deal.get("status") == "approved":
        deal = None
        approved_deal_id = _compact_text(extracted_deal.get("approved_deal_id"))
        if approved_deal_id:
            deal = _select_deal_by_id(client, approved_deal_id)
        if deal is None:
            deal = _find_existing_deal(
                client,
                _deal_payload_from_extracted(extracted_deal),
            )
        if deal is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Draft is marked approved, but its canonical deal could not "
                    "be found. Check migration 009 and the deals table."
                ),
            )
        response.status_code = 200
        return {
            "extracted_deal": extracted_deal,
            "deal": deal,
            "inserted": False,
            "already_approved": True,
        }
    _require_extracted_deal_status(extracted_deal, "needs_review", "approve")
    note = _compact_text(review_payload.note if review_payload else None)

    deal_payload = _deal_payload_from_extracted(extracted_deal)
    existing = _find_existing_deal(client, deal_payload)
    inserted = False
    if existing:
        deal = existing
    else:
        resp = (
            client.table("deals")
            .upsert(deal_payload, on_conflict="deal_id")
            .execute()
        )
        if not resp.data:
            raise HTTPException(status_code=500, detail="deals insert returned no data")
        deal = resp.data[0]
        inserted = True

    reviewed = _mark_extracted_deal_reviewed(
        client,
        extracted_deal_id,
        status="approved",
        approved_deal_id=deal.get("deal_id"),
    )
    _create_ingestion_log(
        client,
        raw_source_id=extracted_deal.get("raw_source_id"),
        url=extracted_deal.get("source_url") or "",
        event="approve_extracted_deal",
        status="approved",
        message=note or "Approved extracted draft into deals",
        metadata={
            "extracted_deal_id": extracted_deal_id,
            "deal_id": deal.get("deal_id"),
            "inserted": inserted,
            "note": note,
        },
    )
    return {
        "extracted_deal": reviewed,
        "deal": deal,
        "inserted": inserted,
        "already_approved": False,
    }


@app.post("/extracted-deals/{extracted_deal_id}/reject")
def reject_extracted_deal(
    extracted_deal_id: str,
    payload: ReviewRejectPayload | None = None,
):
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    extracted_deal = _select_extracted_deal_by_id(client, extracted_deal_id)
    if not extracted_deal:
        raise HTTPException(status_code=404, detail="extracted_deal not found")
    _require_extracted_deal_status(extracted_deal, "needs_review", "reject")
    reason = _compact_text(payload.reason if payload else None)
    reviewed = _mark_extracted_deal_reviewed(
        client,
        extracted_deal_id,
        status="rejected",
    )
    _create_ingestion_log(
        client,
        raw_source_id=extracted_deal.get("raw_source_id"),
        url=extracted_deal.get("source_url") or "",
        event="reject_extracted_deal",
        status="rejected",
        message=reason or "Rejected extracted draft",
        metadata={
            "extracted_deal_id": extracted_deal_id,
            "reason": reason,
        },
    )
    return reviewed


@app.post("/extracted-deals/{extracted_deal_id}/reopen")
def reopen_extracted_deal(extracted_deal_id: str):
    try:
        client = get_ingestion_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    extracted_deal = _select_extracted_deal_by_id(client, extracted_deal_id)
    if not extracted_deal:
        raise HTTPException(status_code=404, detail="extracted_deal not found")
    if extracted_deal.get("status") != "rejected":
        raise HTTPException(
            status_code=400,
            detail="Only rejected extracted deals can be reopened",
        )

    reopened = _reopen_extracted_deal(client, extracted_deal_id)
    _create_ingestion_log(
        client,
        raw_source_id=extracted_deal.get("raw_source_id"),
        url=extracted_deal.get("source_url") or "",
        event="reopen_extracted_deal",
        status="needs_review",
        message="Reopened rejected draft",
        metadata={"extracted_deal_id": extracted_deal_id},
    )
    return reopened


# Chars that would break the PostgREST `or=()` filter syntax if a user typed
# them in the search box (commas separate filters, asterisks are the ilike
# wildcard, parentheses group, backslashes escape). Strip them defensively.
_SEARCH_SCRUB = re.compile(r"[*,()\\]+")


@app.get("/deals")
def list_deals(
    country: str | None = Query(None, description="Exact country match, e.g. 'UAE'"),
    sector: str | None = Query(None, description="Exact sector match"),
    stage: str | None = Query(None, description="Exact stage match, e.g. 'Seed'"),
    year: int | None = Query(None, ge=1900, le=2100,
                             description="Filter on date prefix YYYY-"),
    search: str | None = Query(None, max_length=120,
                               description="Case-insensitive substring match "
                                           "against company_name, lead_investor, sector"),
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    query = get_client().table("deals").select("*")
    if country:
        query = query.eq("country", country)
    if sector:
        query = query.eq("sector", sector)
    if stage:
        query = query.eq("stage", stage)
    if year is not None:
        query = query.like("date", f"{year}-%")
    if search:
        cleaned = _SEARCH_SCRUB.sub("", search).strip()
        if cleaned:
            # PostgREST uses `*` as the ilike wildcard inside `or=()` filters.
            pattern = f"*{cleaned}*"
            query = query.or_(
                f"company_name.ilike.{pattern},"
                f"lead_investor.ilike.{pattern},"
                f"sector.ilike.{pattern}"
            )
    # Newest first; rows with no date sink to the bottom. Postgres defaults
    # NULLs first under DESC, and postgrest-py has no `.nullslast` switch —
    # so we sort in Python. Cheap at this scale (≤ a few hundred rows).
    rows = query.execute().data
    rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    paginated = rows[offset:offset + limit]
    return {"count": len(paginated), "deals": paginated}


@app.get("/deals/{deal_id}")
def get_deal(deal_id: str):
    resp = get_client().table("deals").select("*").eq("deal_id", deal_id).execute()
    if not resp.data:
        raise HTTPException(status_code=404, detail=f"Deal '{deal_id}' not found")
    return resp.data[0]


@app.get("/stats")
def get_stats():
    # 119 rows — pulling everything and aggregating in-process is fine.
    # Swap to a Postgres view/RPC if the table grows past a few thousand.
    rows = get_client().table("deals").select(
        "country, sector, stage, date, amount_usd, disclosed"
    ).execute().data

    total_capital = sum(r["amount_usd"] or 0 for r in rows)
    disclosed_count = sum(1 for r in rows if r.get("disclosed"))

    def breakdown(field: str) -> list[dict]:
        counts = Counter(r.get(field) or "Unknown" for r in rows)
        amounts: dict[str, int] = {}
        for r in rows:
            key = r.get(field) or "Unknown"
            amounts[key] = amounts.get(key, 0) + (r["amount_usd"] or 0)
        return [
            {"key": k, "deal_count": c, "total_capital_usd": amounts[k]}
            for k, c in counts.most_common()
        ]

    # Year breakdown: derive year from the YYYY-MM date string. Rows without
    # a date are reported under "Unknown" so the dashboard can disclose how
    # many deals are unattributed.
    def year_of(date_str: str | None) -> str:
        if not date_str or len(date_str) < 4 or not date_str[:4].isdigit():
            return "Unknown"
        return date_str[:4]

    year_counts: dict[str, int] = {}
    year_amounts: dict[str, int] = {}
    for r in rows:
        y = year_of(r.get("date"))
        year_counts[y] = year_counts.get(y, 0) + 1
        year_amounts[y] = year_amounts.get(y, 0) + (r["amount_usd"] or 0)
    by_year = sorted(
        ({"key": y, "deal_count": c, "total_capital_usd": year_amounts[y]}
         for y, c in year_counts.items()),
        key=lambda x: x["key"],  # ascending by year; "Unknown" sorts to the end
    )

    return {
        "total_deals": len(rows),
        "total_capital_usd": total_capital,
        "disclosed_deals": disclosed_count,
        "undisclosed_deals": len(rows) - disclosed_count,
        "by_country": breakdown("country"),
        "by_sector":  breakdown("sector"),
        "by_stage":   breakdown("stage"),
        "by_year":    by_year,
    }


@app.get("/investors/leaderboard")
def investors_leaderboard(
    stage: str | None = Query(None, description="Restrict to deals at this stage"),
    country: str | None = Query(None, description="Restrict to deals in this country"),
):
    """Ranked lead investors. Filters apply to the underlying deals — they
    determine which deals count toward each investor's totals, not which
    investors are shown."""
    query = get_client().table("deals").select(
        "lead_investor, country, sector, stage, date, amount_usd, disclosed"
    )
    if country:
        query = query.eq("country", country)
    if stage:
        query = query.eq("stage", stage)
    rows = query.execute().data

    # Group by lead_investor, skipping null/blank and placeholder values used
    # in the source data when a lead wasn't publicly reported.
    SENTINELS = {"undisclosed", "unknown", "n/a", "na", "tbd", "-"}
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        name = (r.get("lead_investor") or "").strip()
        if not name or name.lower() in SENTINELS:
            continue
        grouped.setdefault(name, []).append(r)

    leaderboard = []
    for name, deals in grouped.items():
        # "Capital deployed" only counts deals where the amount was disclosed;
        # otherwise totals would be silently understated by undisclosed rounds.
        capital = sum(
            (d.get("amount_usd") or 0)
            for d in deals
            if d.get("disclosed") and d.get("amount_usd")
        )
        sectors = Counter(d["sector"] for d in deals if d.get("sector"))
        stages  = Counter(d["stage"]  for d in deals if d.get("stage"))
        dates   = [d["date"] for d in deals if d.get("date")]
        leaderboard.append({
            "investor": name,
            "deal_count": len(deals),
            "capital_deployed_usd": capital,
            "top_sector": sectors.most_common(1)[0][0] if sectors else None,
            "top_stage":  stages.most_common(1)[0][0]  if stages  else None,
            "last_deal_date": max(dates) if dates else None,
        })

    # Sort by deal_count desc; tiebreak on capital deployed so a more-active
    # investor isn't outranked by a one-deal-big-cheque outlier.
    leaderboard.sort(
        key=lambda x: (x["deal_count"], x["capital_deployed_usd"]),
        reverse=True,
    )
    for i, row in enumerate(leaderboard, start=1):
        row["rank"] = i

    return {"count": len(leaderboard), "investors": leaderboard}
