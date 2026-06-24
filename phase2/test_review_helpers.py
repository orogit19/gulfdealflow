import json
import os
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from fastapi import Response
from fastapi.testclient import TestClient

import main


class ReviewHelperTests(unittest.TestCase):
    def test_noop_draft_update_normalizes_blank_values(self):
        existing = {
            "company_name": "MealPlanet",
            "website": None,
            "co_investors": ["MEVP", "Faith Capital"],
            "confidence_score": 0.95,
        }
        incoming = {
            "company_name": " MealPlanet ",
            "website": "",
            "co_investors": ["MEVP", "Faith Capital"],
            "confidence_score": 0.9500001,
        }

        self.assertEqual(main._changed_extracted_deal_values(existing, incoming), {})

    def test_draft_update_reports_only_changed_fields(self):
        existing = {
            "company_name": "MealPlanet",
            "stage": "Seed",
            "amount_usd": 6000000,
        }
        incoming = {
            "company_name": "MealPlanet",
            "stage": "Series A",
            "amount_usd": 6000000,
        }

        self.assertEqual(
            main._changed_extracted_deal_values(existing, incoming),
            {"stage": "Series A"},
        )

    def test_status_guard_blocks_non_review_drafts(self):
        with self.assertRaises(HTTPException) as ctx:
            main._require_extracted_deal_status(
                {"status": "approved"},
                "needs_review",
                "update",
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("approved", ctx.exception.detail)

    def test_readiness_classifies_ready_and_non_funding(self):
        ready = {
            "company_name": "MealPlanet",
            "country": "UAE",
            "stage": "Seed",
            "announcement_date": "2024-08",
            "amount_usd": 6000000,
            "confidence_score": 0.95,
            "is_funding_round": True,
            "source_url": "https://example.com/article",
        }
        non_funding = {**ready, "is_funding_round": False}

        self.assertEqual(main._extracted_deal_readiness(ready), "ready")
        self.assertEqual(main._extracted_deal_readiness(non_funding), "non_funding")


class AdminKeyMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.original_admin_key = os.environ.get("ADMIN_API_KEY")
        self.client = TestClient(main.app)

    def tearDown(self):
        if self.original_admin_key is None:
            os.environ.pop("ADMIN_API_KEY", None)
        else:
            os.environ["ADMIN_API_KEY"] = self.original_admin_key

    def test_public_routes_do_not_require_admin_key(self):
        os.environ["ADMIN_API_KEY"] = "test-admin-key"

        root_response = self.client.get("/")
        health_response = self.client.get("/health")

        self.assertEqual(root_response.status_code, 200)
        self.assertEqual(health_response.status_code, 200)
        self.assertTrue(health_response.json()["ok"])

    def test_protected_routes_require_admin_key_when_configured(self):
        os.environ["ADMIN_API_KEY"] = "test-admin-key"

        missing_key = self.client.get("/admin/not-a-real-route")
        wrong_key = self.client.get(
            "/admin/not-a-real-route",
            headers={"X-Admin-Key": "wrong"},
        )

        self.assertEqual(missing_key.status_code, 401)
        self.assertEqual(wrong_key.status_code, 401)

    def test_protected_routes_allow_correct_admin_key(self):
        os.environ["ADMIN_API_KEY"] = "test-admin-key"

        response = self.client.get(
            "/admin/not-a-real-route",
            headers={"X-Admin-Key": "test-admin-key"},
        )

        self.assertEqual(response.status_code, 404)

    def test_protected_routes_are_open_when_admin_key_is_not_configured(self):
        os.environ.pop("ADMIN_API_KEY", None)

        response = self.client.get("/admin/not-a-real-route")

        self.assertEqual(response.status_code, 404)

    def test_protected_route_preflight_does_not_require_admin_key(self):
        os.environ["ADMIN_API_KEY"] = "test-admin-key"

        response = self.client.options(
            "/ingest/url",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-admin-key",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("POST", response.headers.get("access-control-allow-methods", ""))
        self.assertIn("x-admin-key", response.headers.get("access-control-allow-headers", "").lower())


class CorsHeaderTests(unittest.TestCase):
    def test_custom_ingestion_headers_are_exposed_to_browsers(self):
        client = TestClient(main.app)

        response = client.get("/health", headers={"Origin": "http://localhost:5173"})
        exposed = response.headers.get("access-control-expose-headers", "")

        self.assertEqual(response.status_code, 200)
        for header in main._EXPOSED_RESPONSE_HEADERS:
            self.assertIn(header, exposed)


class IngestUrlTests(unittest.TestCase):
    def test_duplicate_url_sets_duplicate_response_headers(self):
        existing = {
            "id": "raw-source-1",
            "url": "https://example.com/article",
            "status": "fetched",
            "raw_text": "Existing readable text",
        }
        response = Response()

        with (
            patch.object(main, "get_ingestion_client", return_value=Mock()),
            patch.object(main, "_select_raw_source_by_url", return_value=existing),
            patch.object(main, "_create_ingestion_log") as create_log,
        ):
            result = main.ingest_url(
                main.IngestUrlRequest(url="https://example.com/article"),
                response,
            )

        self.assertEqual(result, existing)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("X-GDF-Duplicate"), "true")
        self.assertEqual(response.headers.get("X-GDF-Raw-Source-Id"), "raw-source-1")
        self.assertEqual(response.headers.get("X-GDF-Fetch-Status"), "fetched")
        create_log.assert_called_once()

    def test_duplicate_failed_url_sets_failure_fetch_status_header(self):
        existing = {
            "id": "raw-source-failed",
            "url": "https://example.com/blocked",
            "status": "fetch_failed",
            "raw_text": None,
            "extracted_text": None,
        }
        response = Response()

        with (
            patch.object(main, "get_ingestion_client", return_value=Mock()),
            patch.object(main, "_select_raw_source_by_url", return_value=existing),
            patch.object(main, "_create_ingestion_log") as create_log,
        ):
            result = main.ingest_url(
                main.IngestUrlRequest(url="https://example.com/blocked"),
                response,
            )

        self.assertEqual(result, existing)
        self.assertEqual(response.headers.get("X-GDF-Duplicate"), "true")
        self.assertEqual(response.headers.get("X-GDF-Raw-Source-Id"), "raw-source-failed")
        self.assertEqual(response.headers.get("X-GDF-Fetch-Status"), "failed")
        create_log.assert_called_once()

    def test_new_url_sets_created_response_headers(self):
        inserted = {
            "id": "raw-source-2",
            "url": "https://example.com/new-article",
            "title": "Funding News",
            "raw_text": "A startup raised a seed round.",
            "status": "fetched",
        }
        response = Response()

        with (
            patch.object(main, "get_ingestion_client", return_value=Mock()),
            patch.object(main, "_select_raw_source_by_url", return_value=None),
            patch.object(main, "_fetch_article_html", return_value="<html></html>"),
            patch.object(
                main,
                "_extract_article",
                return_value=("Funding News", "A startup raised a seed round."),
            ),
            patch.object(main, "_insert_raw_source", return_value=inserted),
            patch.object(main, "_create_ingestion_log") as create_log,
        ):
            result = main.ingest_url(
                main.IngestUrlRequest(url="https://example.com/new-article"),
                response,
            )

        self.assertEqual(result, inserted)
        self.assertEqual(response.headers.get("X-GDF-Duplicate"), "false")
        self.assertEqual(response.headers.get("X-GDF-Raw-Source-Id"), "raw-source-2")
        self.assertEqual(response.headers.get("X-GDF-Fetch-Status"), "fetched")
        create_log.assert_called_once()

    def test_fetch_failure_sets_failure_response_headers(self):
        inserted = {
            "id": "raw-source-3",
            "url": "https://example.com/bad-article",
            "status": "fetch_failed",
            "error_message": "HTTP 403",
        }
        response = Response()

        with (
            patch.object(main, "get_ingestion_client", return_value=Mock()),
            patch.object(main, "_select_raw_source_by_url", return_value=None),
            patch.object(main, "_fetch_article_html", side_effect=main.ArticleFetchError("HTTP 403")),
            patch.object(main, "_insert_raw_source", return_value=inserted),
            patch.object(main, "_create_ingestion_log") as create_log,
        ):
            result = main.ingest_url(
                main.IngestUrlRequest(url="https://example.com/bad-article"),
                response,
            )

        self.assertEqual(result, inserted)
        self.assertEqual(response.headers.get("X-GDF-Duplicate"), "false")
        self.assertEqual(response.headers.get("X-GDF-Raw-Source-Id"), "raw-source-3")
        self.assertEqual(response.headers.get("X-GDF-Fetch-Status"), "failed")
        create_log.assert_called_once()

    def test_pending_discovery_candidate_is_fetched_and_updated(self):
        existing = {
            "id": "raw-source-pending",
            "url": "https://example.com/discovered",
            "source_type": "rss_candidate",
            "status": "pending",
            "raw_text": None,
            "extracted_text": None,
        }
        updated = {
            **existing,
            "status": "fetched",
            "raw_text": "Fresh funding article text",
            "extracted_text": "Fresh funding article text",
        }
        response = Response()

        with (
            patch.object(main, "get_ingestion_client", return_value=Mock()),
            patch.object(main, "_select_raw_source_by_url", return_value=existing),
            patch.object(main, "_fetch_article_html", return_value="<html></html>"),
            patch.object(
                main,
                "_extract_article",
                return_value=("Funding News", "Fresh funding article text"),
            ),
            patch.object(main, "_update_raw_source_row", return_value=updated) as update_row,
            patch.object(main, "_insert_raw_source") as insert_row,
            patch.object(main, "_create_ingestion_log"),
        ):
            result = main.ingest_url(
                main.IngestUrlRequest(url=existing["url"]),
                response,
            )

        self.assertEqual(result, updated)
        self.assertEqual(response.headers.get("X-GDF-Duplicate"), "false")
        update_row.assert_called_once()
        self.assertEqual(update_row.call_args.args[2]["source_type"], "rss_candidate")
        insert_row.assert_not_called()


class IngestExtractTests(unittest.TestCase):
    def _client_with_raw_source(self, raw_source):
        client = Mock()
        execute_result = Mock()
        execute_result.data = [raw_source]
        (
            client.table.return_value
            .select.return_value
            .eq.return_value
            .limit.return_value
            .execute.return_value
        ) = execute_result
        return client

    def test_existing_extraction_sets_existing_response_headers(self):
        raw_source = {
            "id": "raw-source-1",
            "url": "https://example.com/article",
            "raw_text": "Funding article text",
        }
        existing = {"id": "extracted-deal-1", "status": "needs_review"}
        response = Response()

        with (
            patch.object(main, "get_ingestion_client", return_value=self._client_with_raw_source(raw_source)),
            patch.object(main, "_select_active_extraction_for_raw_source", return_value=existing),
            patch.object(main, "_create_ingestion_log") as create_log,
        ):
            result = main.ingest_extract("raw-source-1", response)

        self.assertEqual(result, existing)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("X-GDF-Existing-Extraction"), "true")
        self.assertEqual(response.headers.get("X-GDF-Extracted-Deal-Id"), "extracted-deal-1")
        create_log.assert_called_once()

    def test_new_extraction_sets_created_response_headers(self):
        raw_source = {
            "id": "raw-source-2",
            "url": "https://example.com/new-article",
            "title": "Funding News",
            "raw_text": "Startup raised a seed round.",
        }
        extraction = {
            "company_name": "TestCo",
            "country": "UAE",
            "amount_usd": 1000000,
            "amount_original": "$1 million",
            "currency_original": "USD",
            "stage": "Seed",
            "announcement_date": "2026-05-01",
            "sector": "Fintech",
            "sub_sector": "Payments",
            "lead_investor": "Lead VC",
            "co_investors": ["Co VC"],
            "website": "https://testco.example",
            "is_funding_round": True,
            "confidence_score": 0.92,
            "extraction_notes": "Clear funding announcement.",
        }
        inserted = {"id": "extracted-deal-2", "status": "needs_review"}
        response = Response()

        with (
            patch.object(main, "get_ingestion_client", return_value=self._client_with_raw_source(raw_source)),
            patch.object(main, "_select_active_extraction_for_raw_source", return_value=None),
            patch.object(main, "extract_deal_from_text", return_value=extraction),
            patch.object(main, "_insert_extracted_deal", return_value=inserted),
            patch.object(main, "_update_raw_source"),
            patch.object(main, "_create_ingestion_log") as create_log,
        ):
            result = main.ingest_extract("raw-source-2", response)

        self.assertEqual(result, inserted)
        self.assertEqual(response.headers.get("X-GDF-Existing-Extraction"), "false")
        self.assertEqual(response.headers.get("X-GDF-Extracted-Deal-Id"), "extracted-deal-2")
        self.assertEqual(create_log.call_count, 2)

    def test_raw_source_without_text_fails_before_ai_extraction(self):
        raw_source = {
            "id": "raw-source-empty",
            "url": "https://example.com/empty",
            "raw_text": "",
            "extracted_text": None,
        }
        response = Response()

        with (
            patch.object(main, "get_ingestion_client", return_value=self._client_with_raw_source(raw_source)),
            patch.object(main, "_select_active_extraction_for_raw_source", return_value=None),
            patch.object(main, "extract_deal_from_text") as extract_deal,
            patch.object(main, "_update_raw_source") as update_raw_source,
            patch.object(main, "_create_ingestion_log") as create_log,
        ):
            with self.assertRaises(HTTPException) as ctx:
                main.ingest_extract("raw-source-empty", response)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("no raw_text", ctx.exception.detail)
        extract_deal.assert_not_called()
        update_raw_source.assert_called_once()
        create_log.assert_called_once()


class RawSourceRefetchTests(unittest.TestCase):
    def _client_with_update(self, updated):
        client = Mock()
        execute_result = Mock()
        execute_result.data = [updated]
        (
            client.table.return_value
            .update.return_value
            .eq.return_value
            .execute.return_value
        ) = execute_result
        return client

    def test_refetch_success_sets_fetched_header(self):
        raw_source = {
            "id": "raw-source-1",
            "url": "https://example.com/article",
            "raw_text": "Old text",
        }
        updated = {
            **raw_source,
            "raw_text": "Fresh readable article text",
            "extracted_text": "Fresh readable article text",
            "status": "fetched",
            "error_message": None,
        }
        response = Response()

        with (
            patch.object(main, "get_ingestion_client", return_value=self._client_with_update(updated)),
            patch.object(main, "_select_raw_source_by_id", return_value=raw_source),
            patch.object(main, "_fetch_article_html", return_value="<html></html>"),
            patch.object(main, "_extract_article", return_value=("Fresh Title", "Fresh readable article text")),
            patch.object(main, "_create_ingestion_log") as create_log,
        ):
            result = main.refetch_raw_source("raw-source-1", response)

        self.assertEqual(result, updated)
        self.assertEqual(response.headers.get("X-GDF-Fetch-Status"), "fetched")
        create_log.assert_called_once()

    def test_refetch_failure_sets_failed_header(self):
        raw_source = {
            "id": "raw-source-2",
            "url": "https://example.com/blocked",
            "raw_text": "Old text",
        }
        updated = {
            **raw_source,
            "status": "fetch_failed",
            "error_message": "HTTP 403",
        }
        response = Response()

        with (
            patch.object(main, "get_ingestion_client", return_value=self._client_with_update(updated)),
            patch.object(main, "_select_raw_source_by_id", return_value=raw_source),
            patch.object(main, "_fetch_article_html", side_effect=main.ArticleFetchError("HTTP 403")),
            patch.object(main, "_create_ingestion_log") as create_log,
        ):
            result = main.refetch_raw_source("raw-source-2", response)

        self.assertEqual(result, updated)
        self.assertEqual(response.headers.get("X-GDF-Fetch-Status"), "failed")
        create_log.assert_called_once()


class ApproveExtractedDealTests(unittest.TestCase):
    def test_retry_returns_already_approved_deal_without_writing(self):
        draft = {
            "id": "draft-1",
            "status": "approved",
            "approved_deal_id": "deal-1",
        }
        deal = {"deal_id": "deal-1", "company_name": "TestCo"}
        response = Response()

        with (
            patch.object(main, "get_ingestion_client", return_value=Mock()),
            patch.object(main, "_select_extracted_deal_by_id", return_value=draft),
            patch.object(main, "_select_deal_by_id", return_value=deal),
            patch.object(main, "_find_existing_deal") as find_existing,
            patch.object(main, "_mark_extracted_deal_reviewed") as mark_reviewed,
            patch.object(main, "_create_ingestion_log") as create_log,
        ):
            result = main.approve_extracted_deal("draft-1", response)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(result["already_approved"])
        self.assertFalse(result["inserted"])
        self.assertEqual(result["deal"], deal)
        find_existing.assert_not_called()
        mark_reviewed.assert_not_called()
        create_log.assert_not_called()

    def test_retry_without_audit_link_finds_existing_deal(self):
        draft = {
            "id": "draft-2",
            "status": "approved",
            "approved_deal_id": None,
        }
        payload = {"deal_id": "ai-test", "company_name": "TestCo"}
        deal = {"deal_id": "existing-1", "company_name": "TestCo"}
        response = Response()

        with (
            patch.object(main, "get_ingestion_client", return_value=Mock()),
            patch.object(main, "_select_extracted_deal_by_id", return_value=draft),
            patch.object(main, "_deal_payload_from_extracted", return_value=payload),
            patch.object(main, "_find_existing_deal", return_value=deal),
        ):
            result = main.approve_extracted_deal("draft-2", response)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(result["already_approved"])
        self.assertEqual(result["deal"], deal)

    def test_retry_reports_inconsistent_approved_state(self):
        draft = {
            "id": "draft-3",
            "status": "approved",
            "approved_deal_id": "missing-deal",
        }
        response = Response()

        with (
            patch.object(main, "get_ingestion_client", return_value=Mock()),
            patch.object(main, "_select_extracted_deal_by_id", return_value=draft),
            patch.object(main, "_select_deal_by_id", return_value=None),
            patch.object(main, "_deal_payload_from_extracted", return_value={"company_name": "TestCo"}),
            patch.object(main, "_find_existing_deal", return_value=None),
        ):
            with self.assertRaises(HTTPException) as ctx:
                main.approve_extracted_deal("draft-3", response)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("canonical deal", ctx.exception.detail)


class ConfigStatusTests(unittest.TestCase):
    tracked_env = [
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_ANON_KEY",
        "OPENAI_API_KEY",
        "ADMIN_API_KEY",
        "CRON_SECRET",
        "CORS_ORIGINS",
        "CSV_PATH",
    ]

    def setUp(self):
        self.original_values = {name: os.environ.get(name) for name in self.tracked_env}

    def tearDown(self):
        for name, value in self.original_values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_config_status_fails_when_required_env_missing(self):
        for name in self.tracked_env:
            os.environ.pop(name, None)

        status = main._env_config_status()

        self.assertFalse(status["ok"])
        self.assertFalse(status["required"]["SUPABASE_URL"])

    def test_config_status_passes_when_required_env_present(self):
        for name in [
            "SUPABASE_URL",
            "SUPABASE_SERVICE_ROLE_KEY",
            "SUPABASE_ANON_KEY",
            "OPENAI_API_KEY",
        ]:
            os.environ[name] = "configured"

        status = main._env_config_status()

        self.assertTrue(status["ok"])
        self.assertFalse(status["production_ready"])
        self.assertEqual(
            set(status["optional"]),
            {"ADMIN_API_KEY", "CRON_SECRET", "CORS_ORIGINS", "CSV_PATH"},
        )
        self.assertEqual(status["article_fetch"]["timeout_seconds"], 20)
        self.assertEqual(status["article_fetch"]["max_bytes"], 5_000_000)

    def test_config_status_requires_production_hardening(self):
        for name in [
            "SUPABASE_URL",
            "SUPABASE_SERVICE_ROLE_KEY",
            "SUPABASE_ANON_KEY",
            "OPENAI_API_KEY",
            "ADMIN_API_KEY",
            "CRON_SECRET",
        ]:
            os.environ[name] = "configured"
        os.environ["CORS_ORIGINS"] = "https://gulfdealflow.vercel.app"

        status = main._env_config_status()

        self.assertTrue(status["ok"])
        self.assertTrue(status["production_ready"])
        self.assertTrue(all(status["production_checks"].values()))


class EnvParsingTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("TEST_INT_ENV", None)

    def test_int_env_uses_default_for_invalid_values(self):
        os.environ["TEST_INT_ENV"] = "not-an-int"

        self.assertEqual(main._int_env("TEST_INT_ENV", 20, 3, 60), 20)

    def test_int_env_clamps_values_to_bounds(self):
        os.environ["TEST_INT_ENV"] = "999"
        self.assertEqual(main._int_env("TEST_INT_ENV", 20, 3, 60), 60)

        os.environ["TEST_INT_ENV"] = "1"
        self.assertEqual(main._int_env("TEST_INT_ENV", 20, 3, 60), 3)

    def test_database_connection_error_classification(self):
        class ConnectError(Exception):
            pass

        class ApiError(Exception):
            pass

        self.assertTrue(main._is_database_connection_error(ConnectError()))
        self.assertFalse(main._is_database_connection_error(ApiError()))


class SafeFetchTargetTests(unittest.TestCase):
    def test_rejects_localhost_and_private_ip_literals(self):
        for url in [
            "http://localhost/admin",
            "http://127.0.0.1/admin",
            "http://10.0.0.5/internal",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/admin",
        ]:
            with self.subTest(url=url):
                with self.assertRaises(main.ArticleFetchError):
                    main._assert_public_fetch_target(url)

    def test_accepts_public_ip_literal(self):
        main._assert_public_fetch_target("https://8.8.8.8/article")

    def test_rejects_hostname_when_any_resolved_address_is_private(self):
        resolved = [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ]

        with patch.object(main.socket, "getaddrinfo", return_value=resolved):
            with self.assertRaises(main.ArticleFetchError):
                main._assert_public_fetch_target("https://example.com/article")

    def test_validate_url_rejects_embedded_credentials(self):
        with self.assertRaises(HTTPException) as ctx:
            main._validate_ingest_url("https://user:secret@example.com/article")

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("credentials", ctx.exception.detail)


class UrlFetchConstructionTests(unittest.TestCase):
    class FakeHeaders:
        def __init__(self, content_type):
            self.content_type = content_type

        def get(self, name, default=None):
            if name.lower() == "content-type":
                return self.content_type
            return default

        def get_content_charset(self):
            return "utf-8"

    class FakeResponse:
        def __init__(self, body, content_type):
            self.body = body
            self.headers = UrlFetchConstructionTests.FakeHeaders(content_type)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, _limit):
            return self.body

    def test_article_fetch_builds_urllib_request(self):
        opener = Mock()
        opener.open.return_value = self.FakeResponse(
            b"<html><body><p>Readable funding article text.</p></body></html>",
            "text/html; charset=utf-8",
        )

        with (
            patch.object(main, "_assert_public_fetch_target"),
            patch.object(main, "build_opener", return_value=opener),
        ):
            html = main._fetch_article_html("https://example.com/article")

        request = opener.open.call_args.args[0]
        self.assertIsInstance(request, main.URLRequest)
        self.assertIn("Readable funding", html)

    def test_rss_fetch_builds_urllib_request(self):
        opener = Mock()
        opener.open.return_value = self.FakeResponse(
            b"<?xml version='1.0'?><rss><channel></channel></rss>",
            "application/rss+xml; charset=utf-8",
        )

        with (
            patch.object(main, "_assert_public_fetch_target"),
            patch.object(main, "build_opener", return_value=opener),
        ):
            xml = main._fetch_rss_xml("https://news.google.com/rss/search?q=test")

        request = opener.open.call_args.args[0]
        self.assertIsInstance(request, main.URLRequest)
        self.assertIn("<rss>", xml)

    def test_google_news_url_resolves_to_publisher(self):
        token = "test-token"
        google_url = f"https://news.google.com/rss/articles/{token}?oc=5"
        wrapper_html = (
            '<div data-n-a-ts="1782129839" '
            'data-n-a-sg="test-signature"></div>'
        )
        decoded_line = json.dumps(
            [
                [
                    "wrb.fr",
                    "Fbv4je",
                    json.dumps(
                        [
                            "garturlres",
                            "https://publisher.example/funding-story",
                            1,
                        ]
                    ),
                    None,
                    None,
                    None,
                    "generic",
                ]
            ]
        )
        opener = Mock()
        opener.open.return_value = self.FakeResponse(
            decoded_line.encode("utf-8"),
            "application/json; charset=utf-8",
        )

        with (
            patch.object(main, "_fetch_article_html", return_value=wrapper_html),
            patch.object(main, "_assert_public_fetch_target"),
            patch.object(main, "build_opener", return_value=opener),
        ):
            resolved = main._resolve_google_news_url(google_url)

        self.assertEqual(
            resolved,
            "https://publisher.example/funding-story",
        )
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url.split("?", 1)[0], "https://news.google.com/_/DotsSplashUi/data/batchexecute")
        self.assertIn(b"test-token", request.data)

    def test_non_google_news_url_is_unchanged(self):
        url = "https://publisher.example/funding-story"
        self.assertEqual(main._resolve_google_news_url(url), url)


class RssDiscoveryTests(unittest.TestCase):
    def test_parser_keeps_only_gcc_funding_candidates(self):
        xml = """<?xml version="1.0"?>
        <rss><channel>
          <item>
            <title>Dubai fintech TestCo raises $5m seed round</title>
            <link>https://news.google.com/articles/test-1</link>
            <description>UAE startup funding announcement</description>
            <pubDate>Mon, 22 Jun 2026 10:00:00 GMT</pubDate>
          </item>
          <item>
            <title>Dubai startup launches a new product</title>
            <link>https://news.google.com/articles/test-2</link>
            <description>No financing announced</description>
          </item>
          <item>
            <title>London startup raises a seed round</title>
            <link>https://news.google.com/articles/test-3</link>
            <description>UK venture capital news</description>
          </item>
        </channel></rss>"""

        candidates = main._discover_rss_candidates(xml, "startup funding UAE")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["url"], "https://news.google.com/articles/test-1")
        self.assertEqual(candidates[0]["published_at"], "2026-06-22T10:00:00+00:00")

    def test_invalid_rss_xml_is_reported_as_fetch_error(self):
        with self.assertRaises(main.ArticleFetchError):
            main._discover_rss_candidates("<rss><broken>", "test query")


class CronDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.original_secret = os.environ.get("CRON_SECRET")
        self.client = TestClient(main.app)

    def tearDown(self):
        if self.original_secret is None:
            os.environ.pop("CRON_SECRET", None)
        else:
            os.environ["CRON_SECRET"] = self.original_secret

    def test_cron_requires_matching_bearer_secret(self):
        os.environ["CRON_SECRET"] = "test-cron-secret"

        missing = self.client.get("/cron/discover")
        wrong = self.client.get(
            "/cron/discover",
            headers={"Authorization": "Bearer wrong"},
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)

    def test_cron_runs_default_discovery_when_authorized(self):
        os.environ["CRON_SECRET"] = "test-cron-secret"
        expected = {
            "query_count": 4,
            "discovered_count": 0,
            "query_errors": [],
            "candidates": [],
        }

        with (
            patch.object(main, "get_ingestion_client", return_value=Mock()),
            patch.object(main, "_run_rss_discovery", return_value=expected) as run_discovery,
        ):
            response = self.client.get(
                "/cron/discover",
                headers={"Authorization": "Bearer test-cron-secret"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        run_discovery.assert_called_once()


if __name__ == "__main__":
    unittest.main()
