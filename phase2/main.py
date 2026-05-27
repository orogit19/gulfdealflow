"""GulfDealFlow FastAPI backend.

Endpoints:
    GET /deals                   - list deals, optional filters: country, sector, stage, year
    GET /deals/{id}              - single deal by deal_id
    GET /stats                   - totals + breakdowns by country / sector / stage
    GET /investors/leaderboard   - ranked lead investors with deal/capital/sector/stage stats
    POST /ingest/url             - fetch and store a raw funding article URL
    POST /ingest/extract/{id}    - extract a structured deal from a raw source
    POST /ingest/portfolio-page  - extract draft VC portfolio relationships
    POST /relationships/{id}/enrich-funding - draft funding extraction for approved relationship

Run:
    uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import os
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
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

# CORS: defaults to "*" (no credentials).
# Set CORS_ORIGINS to a comma-separated list in production to lock to your
# frontend domain, e.g.  CORS_ORIGINS=https://gulfdealflow.vercel.app
_origins_env = os.environ.get("CORS_ORIGINS", "*").strip()
allow_origins = ["*"] if _origins_env == "*" else [
    o.strip() for o in _origins_env.split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class IngestUrlRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)


class PortfolioPageRequest(BaseModel):
    investor_name: str = Field(..., min_length=1, max_length=200)
    url: str = Field(..., min_length=8, max_length=2048)


class FundingEnrichmentRequest(BaseModel):
    source_url: str = Field(..., min_length=8, max_length=2048)


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


@app.get("/")
def root():
    return {"service": "GulfDealFlow API", "version": "0.1.0"}


class ArticleFetchError(Exception):
    pass


_WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")


def _validate_ingest_url(url: str) -> str:
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail="url must be an absolute http or https URL",
        )
    return cleaned


def _source_info_from_url(url: str) -> tuple[str, str]:
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    source_name = domain.split(".")[0].replace("-", " ").title() if domain else None
    return source_name, domain


def _fetch_article_html(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "GulfDealFlowBot/0.1 (+https://gulfdealflow.com)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                raise ArticleFetchError(f"URL did not return HTML: {content_type}")
            raw = response.read(5_000_000)
            charset = response.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except HTTPError as exc:
        raise ArticleFetchError(f"HTTP {exc.code} while fetching URL") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise ArticleFetchError(f"Could not fetch URL: {reason}") from exc
    except TimeoutError as exc:
        raise ArticleFetchError("Timed out while fetching URL") from exc


def _clean_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = _WHITESPACE_RE.sub(" ", line).strip()
        if line:
            lines.append(line)
    return "\n\n".join(lines)


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


def _extract_article(html: str) -> tuple[str | None, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = _extract_title(soup)

    for tag in soup(["script", "style", "noscript", "svg", "form", "nav", "header", "footer"]):
        tag.decompose()

    container = soup.find("article") or soup.find("main") or soup.body or soup
    paragraphs = [
        _clean_text(node.get_text(" "))
        for node in container.find_all(["p", "li"])
    ]
    paragraphs = [p for p in paragraphs if len(p) >= 40]

    if paragraphs:
        return title, "\n\n".join(paragraphs)

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
    if existing:
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
        row = _insert_raw_source(client, {
            "url": url,
            "source_type": "url",
            "source_name": source_name,
            "domain": domain,
            "status": "fetch_failed",
            "error_message": str(exc),
        })
        _create_ingestion_log(
            client,
            raw_source_id=row.get("id"),
            url=url,
            event="fetch_article",
            status="failed",
            message=str(exc),
        )
        return row

    row = _insert_raw_source(client, {
        "url": url,
        "source_type": "url",
        "source_name": source_name,
        "domain": domain,
        "title": title,
        "raw_text": raw_text,
        "extracted_text": raw_text,
        "status": "fetched",
        "error_message": None,
    })
    _create_ingestion_log(
        client,
        raw_source_id=row.get("id"),
        url=url,
        event="ingest_url",
        status="fetched",
        message="Fetched and saved raw source",
        metadata={"domain": domain, "text_length": len(raw_text)},
    )
    return row


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
def ingest_extract(raw_source_id: str):
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
    return extracted_deal


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
