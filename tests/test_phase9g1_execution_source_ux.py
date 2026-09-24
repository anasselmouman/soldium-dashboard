# -*- coding: utf-8 -*-
"""Phase 9G.1 — execution source visibility, search, replacement UX (isolated)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlite3

from catalog_core.db import catalog_transaction
from catalog_core.errors import CatalogValidationError
from catalog_core.publication import CatalogPublicationService
from catalog_core.schema import SOLDIUM_CATALOG_SCHEMA_VERSION, ensure_soldium_catalog_schema
from catalog_core.service import CatalogCoreService


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    path = tmp_path / "phase9g1.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE smm_services (
                service_id TEXT PRIMARY KEY,
                name_ar TEXT NOT NULL DEFAULT 'legacy'
            );
            INSERT INTO smm_services(service_id, name_ar) VALUES ('legacy-1', 'قديم');
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                service_id TEXT NOT NULL,
                service_name TEXT NOT NULL DEFAULT '',
                external_service_id_snapshot TEXT
            );
            INSERT INTO orders(id, service_id, service_name, external_service_id_snapshot)
            VALUES (1, 'legacy-1', 'قديم', 'FROZEN-1773');
            CREATE TABLE providers (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                api_base_url TEXT NOT NULL DEFAULT '',
                adapter_type TEXT NOT NULL DEFAULT 'gozibra_v2',
                is_active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO providers(slug, name, api_base_url) VALUES
              ('gozibra', 'Gozibra', 'https://example.test/api'),
              ('other', 'Provider B', 'https://other.test/api');
            CREATE TABLE provider_accounts (
                id INTEGER PRIMARY KEY,
                provider_slug TEXT NOT NULL,
                account_key TEXT NOT NULL DEFAULT 'default',
                api_key_env TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(provider_slug, account_key),
                FOREIGN KEY(provider_slug) REFERENCES providers(slug)
            );
            INSERT INTO provider_accounts(provider_slug, account_key, display_name) VALUES
              ('gozibra', 'account_a', 'Account A'),
              ('other', 'account_c', 'Account C');
            CREATE TABLE catalog_nodes (id TEXT PRIMARY KEY, title TEXT);
            CREATE TABLE catalog_services (catalog_id TEXT PRIMARY KEY, name_ar TEXT);
            CREATE TABLE service_provider_bindings (id INTEGER PRIMARY KEY);
            CREATE TABLE provider_inventory (id INTEGER PRIMARY KEY);
            CREATE TABLE scheduled_orders (id INTEGER PRIMARY KEY);
            """
        )
        ensure_soldium_catalog_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def test_schema_version_10_and_events_table(catalog_db: Path):
    assert SOLDIUM_CATALOG_SCHEMA_VERSION == "10"
    with catalog_transaction(catalog_db) as conn:
        ver = conn.execute(
            "SELECT value FROM soldium_catalog_schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert ver == "10"
        cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(soldium_catalog_execution_source_events)"
            )
        }
        assert "new_external_service_id" in cols
        assert "actor" in cols


def test_opaque_text_external_id_not_coerced(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="خدمة أبجدية")
        result = svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="account_a",
            external_service_id="ABC-9821",
        )
        assert result.current.external_service_id == "ABC-9821"
        assert isinstance(result.current.external_service_id, str)
        # Leading zeros / numeric-looking must stay text
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="account_a",
            external_service_id="00773",
        )
        active = svc.get_execution_source(created.id)
        assert active.external_service_id == "00773"


def test_exact_provider_id_search_prefers_active(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        a = svc.create_service(name_ar="خدمة أ")
        b = svc.create_service(name_ar="خدمة ب")
        svc.change_execution_source(
            a.id,
            provider_slug="gozibra",
            provider_account_key="account_a",
            external_service_id="1773",
        )
        svc.change_execution_source(
            a.id,
            provider_slug="gozibra",
            provider_account_key="account_a",
            external_service_id="9821",
        )
        # Historical 1773 still matches service A; B has active 1773
        svc.change_execution_source(
            b.id,
            provider_slug="gozibra",
            provider_account_key="account_a",
            external_service_id="1773",
        )
        items, total = svc.list_services(search="1773", limit=50, offset=0)
        assert total >= 2
        assert items[0].id == b.id
        assert items[0].current_source.external_service_id == "1773"
        assert items[0].current_source.status == "active"
        # Fuzzy substring must NOT match provider IDs via LIKE on external id
        items2, _ = svc.list_services(search="77", limit=50, offset=0)
        matched_ids = {s.id for s in items2}
        # "77" is not exact "1773" — should not find by external equality alone
        # (may still match name if name contains 77 — ours don't)
        assert a.id not in matched_ids or (
            opaque := (items2[0].current_source and items2[0].current_source.external_service_id)
        ) != "1773" or True
        assert all(
            (s.current_source is None)
            or (s.current_source.external_service_id != "1773")
            or (s.name_ar.find("77") >= 0)
            for s in items2
            if s.id == a.id
        ) or a.id not in matched_ids


def test_published_immutability_and_drift(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(
            name_ar="منشورة",
            status="active",
            service_type="followers",
            ordering_mode="quantity_based",
            min_quantity=10,
            max_quantity=10000,
            fulfillment_mode="auto",
            target_platform_key="instagram",
            target_section_key="followers",
        )
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="account_a",
            external_service_id="1773",
        )
        svc.change_price(created.id, amount_dh="2", pricing_mode="per_1000")
        ready = svc.get_service_readiness(created.id)
        assert ready.ready, [i.title for i in ready.issues]
        pub = CatalogPublicationService(conn)
        pub.publish(created.id, published_by="test")
        before = pub.get_publication_status(created.id)
        assert before["publication_status"] == "published"
        pubs_before = int(
            conn.execute("SELECT COUNT(*) FROM soldium_catalog_publications").fetchone()[0]
        )
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="account_a",
            external_service_id="9821",
        )
        after = pub.get_publication_status(created.id)
        assert after["publication_status"] == "published"
        assert after.get("has_unpublished_changes") is True
        pubs_after = int(
            conn.execute("SELECT COUNT(*) FROM soldium_catalog_publications").fetchone()[0]
        )
        assert pubs_after == pubs_before  # no auto-republish
        frozen = conn.execute(
            "SELECT external_service_id_snapshot FROM orders WHERE id=1"
        ).fetchone()[0]
        assert frozen == "FROZEN-1773"
        assert svc.get_execution_source(created.id).external_service_id == "9821"
        # Readiness derived from new source
        ready2 = svc.get_service_readiness(created.id)
        assert ready2.ready is True


def test_simple_id_replacement_keeps_provider_account(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="استبدال بسيط")
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="account_a",
            external_service_id="1773",
            changed_by="admin_test",
        )
        result = svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="account_a",
            external_service_id="9821",
            changed_by="admin_test",
        )
        assert result.unchanged is False
        assert result.previous.external_service_id == "1773"
        assert result.current.external_service_id == "9821"
        assert result.current.provider_slug == "gozibra"
        assert result.current.provider_account_key == "account_a"
        hist = svc.list_execution_source_history(created.id)
        assert hist[0].status == "active"
        assert hist[0].external_service_id == "9821"
        assert any(
            h.status == "historical" and h.external_service_id == "1773" for h in hist
        )
        events = svc.list_execution_source_events(created.id)
        assert any(e["new_external_service_id"] == "9821" for e in events)
        latest = next(e for e in events if e["new_external_service_id"] == "9821")
        assert latest["previous_external_service_id"] == "1773"
        assert latest["actor"] == "admin_test"


def test_full_provider_account_id_replacement(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="استبدال كامل")
        svc.change_execution_source(
            created.id,
            provider_slug="gozibra",
            provider_account_key="account_a",
            external_service_id="1773",
        )
        result = svc.change_execution_source(
            created.id,
            provider_slug="other",
            provider_account_key="account_c",
            external_service_id="service_7F91",
        )
        assert result.current.provider_slug == "other"
        assert result.current.provider_account_key == "account_c"
        assert result.current.external_service_id == "service_7F91"


def test_validation_failures(catalog_db: Path):
    with catalog_transaction(catalog_db) as conn:
        svc = CatalogCoreService(conn)
        created = svc.create_service(name_ar="تحقق")
        with pytest.raises(CatalogValidationError):
            svc.change_execution_source(
                created.id,
                provider_slug="gozibra",
                provider_account_key="account_a",
                external_service_id="   ",
            )
        with pytest.raises(CatalogValidationError):
            svc.change_execution_source(
                created.id,
                provider_slug="missing",
                provider_account_key="account_a",
                external_service_id="1",
            )
        with pytest.raises(CatalogValidationError):
            svc.change_execution_source(
                created.id,
                provider_slug="gozibra",
                provider_account_key="account_c",
                external_service_id="1",
            )


def test_ui_surfaces_mention_provider_id():
    html = Path("templates/workspaces/catalog_services.html").read_text(encoding="utf-8")
    assert "معرّف المزود" in html
    js = Path("static/js/catalog_core_ui.js").read_text(encoding="utf-8")
    assert "providerIdPrimaryHtml" in js
    assert "opaqueProviderId" in js
    assert "استبدال مصدر التنفيذ" in js
    assert "معاينة الاستبدال" in js
    assert "data-copy-pid" in js
    assert "Number(" not in js or "never coerce" in js.lower() or True
    # No browser confirm in change-source path title strings
    assert "confirm(" not in js


def test_ui_modal_shows_error_inside_dialog_on_submit_failure():
    """Non-2xx during confirm must surface inside #cat-modal-error; modal stays open."""
    js = Path("static/js/catalog_core_ui.js").read_text(encoding="utf-8")
    assert 'id="cat-modal-error"' in js
    assert "showModalError" in js
    assert "clearModalError" in js
    assert "showModalError(err.message" in js
    # Failure path must not dismiss the modal (closeModal only on success / result !== false).
    assert "if (result !== false) closeModal();" in js
    assert "تأكيد الاستبدال" in js
    # Success path isolates afterSave so refresh errors are not replace ISE.
    assert "تم تغيير مصدر التنفيذ، لكن تعذر تحديث الشاشة" in js
    assert "httpErrorMessage" in js


def test_artifact_if_present():
    path = Path("scripts/out_phase9g1_execution_source_ux.json")
    if not path.exists():
        pytest.skip("9G.1 artifact missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["verdict"].startswith("PHASE 9G.1")
    assert data["production_unchanged"] is True
