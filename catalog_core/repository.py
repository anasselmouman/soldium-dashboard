# -*- coding: utf-8 -*-
"""Catalog core persistence."""

from __future__ import annotations

import sqlite3
from typing import Any

from catalog_core.commercial import (
    DEFAULT_FULFILLMENT_MODE,
    DEFAULT_MAX_QUANTITY,
    DEFAULT_MIN_QUANTITY,
    DEFAULT_ORDERING_MODE,
    DEFAULT_SERVICE_TYPE,
)
from catalog_core.models import (
    CatalogEntry,
    CatalogNode,
    CatalogPrice,
    CatalogService,
    ExecutionSource,
)


def _row_int(row: sqlite3.Row, key: str, default: int) -> int:
    if key not in row.keys() or row[key] is None:
        return default
    return int(row[key])


def _row_service(row: sqlite3.Row) -> CatalogService:
    keys = set(row.keys())

    def _opt_key(name: str) -> str | None:
        if name not in keys:
            return None
        raw = row[name]
        if raw is None:
            return None
        text = str(raw).strip()
        return text or None

    return CatalogService(
        id=str(row["id"]),
        name_ar=str(row["name_ar"] or ""),
        note_ar=str(row["note_ar"] or ""),
        status=str(row["status"] or "draft"),  # type: ignore[arg-type]
        service_type=str(row["service_type"] if "service_type" in keys and row["service_type"] else DEFAULT_SERVICE_TYPE),
        ordering_mode=str(
            row["ordering_mode"]
            if "ordering_mode" in keys and row["ordering_mode"]
            else DEFAULT_ORDERING_MODE
        ),
        min_quantity=_row_int(row, "min_quantity", DEFAULT_MIN_QUANTITY),
        max_quantity=_row_int(row, "max_quantity", DEFAULT_MAX_QUANTITY),
        fulfillment_mode=str(
            row["fulfillment_mode"]
            if "fulfillment_mode" in keys and row["fulfillment_mode"]
            else DEFAULT_FULFILLMENT_MODE
        ),
        target_platform_key=_opt_key("target_platform_key"),
        target_section_key=_opt_key("target_section_key"),
        target_subsection_key=_opt_key("target_subsection_key"),
        target_link_prompt_key=_opt_key("target_link_prompt_key"),
        target_link_type=_opt_key("target_link_type"),
        created_at=row["created_at"] if "created_at" in keys else None,
        updated_at=row["updated_at"] if "updated_at" in keys else None,
    )


def _row_node(row: sqlite3.Row) -> CatalogNode:
    return CatalogNode(
        id=str(row["id"]),
        name_ar=str(row["name_ar"] or ""),
        note_ar=str(row["note_ar"] or ""),
        status=str(row["status"] or "active"),  # type: ignore[arg-type]
        created_at=row["created_at"] if "created_at" in row.keys() else None,
        updated_at=row["updated_at"] if "updated_at" in row.keys() else None,
    )


def _row_entry(row: sqlite3.Row) -> CatalogEntry:
    return CatalogEntry(
        id=str(row["id"]),
        parent_entry_id=str(row["parent_entry_id"]) if row["parent_entry_id"] else None,
        entry_type=str(row["entry_type"]),  # type: ignore[arg-type]
        node_id=str(row["node_id"]) if row["node_id"] else None,
        service_id=str(row["service_id"]) if row["service_id"] else None,
        sort_order=int(row["sort_order"] or 0),
        created_at=row["created_at"] if "created_at" in row.keys() else None,
        updated_at=row["updated_at"] if "updated_at" in row.keys() else None,
        name_ar=str(row["name_ar"]) if "name_ar" in row.keys() and row["name_ar"] is not None else "",
        status=str(row["status"]) if "status" in row.keys() and row["status"] is not None else "",
    )


class CatalogRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ── services ──

    def get_service(self, service_id: str) -> CatalogService | None:
        row = self.connection.execute(
            "SELECT * FROM soldium_catalog_services WHERE id = ?",
            (service_id,),
        ).fetchone()
        return _row_service(row) if row else None

    def insert_service(self, service: CatalogService) -> None:
        self.connection.execute(
            """
            INSERT INTO soldium_catalog_services (
                id, name_ar, note_ar, status,
                service_type, ordering_mode, min_quantity, max_quantity,
                fulfillment_mode,
                target_platform_key, target_section_key, target_subsection_key,
                target_link_prompt_key, target_link_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                service.id,
                service.name_ar,
                service.note_ar,
                service.status,
                service.service_type,
                service.ordering_mode,
                service.min_quantity,
                service.max_quantity,
                service.fulfillment_mode,
                service.target_platform_key,
                service.target_section_key,
                service.target_subsection_key,
                service.target_link_prompt_key,
                service.target_link_type,
            ),
        )

    def update_service_fields(
        self,
        service_id: str,
        *,
        name_ar: str | None = None,
        note_ar: str | None = None,
        status: str | None = None,
        service_type: str | None = None,
        ordering_mode: str | None = None,
        min_quantity: int | None = None,
        max_quantity: int | None = None,
        fulfillment_mode: str | None = None,
        target_platform_key: str | None = ...,  # type: ignore[assignment]
        target_section_key: str | None = ...,  # type: ignore[assignment]
        target_subsection_key: str | None = ...,  # type: ignore[assignment]
        target_link_prompt_key: str | None = ...,  # type: ignore[assignment]
        target_link_type: str | None = ...,  # type: ignore[assignment]
    ) -> bool:
        sets: list[str] = []
        params: list[Any] = []
        if name_ar is not None:
            sets.append("name_ar = ?")
            params.append(name_ar)
        if note_ar is not None:
            sets.append("note_ar = ?")
            params.append(note_ar)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if service_type is not None:
            sets.append("service_type = ?")
            params.append(service_type)
        if ordering_mode is not None:
            sets.append("ordering_mode = ?")
            params.append(ordering_mode)
        if min_quantity is not None:
            sets.append("min_quantity = ?")
            params.append(min_quantity)
        if max_quantity is not None:
            sets.append("max_quantity = ?")
            params.append(max_quantity)
        if fulfillment_mode is not None:
            sets.append("fulfillment_mode = ?")
            params.append(fulfillment_mode)
        # Ellipsis sentinel: omit field; None clears optional keys.
        if target_platform_key is not ...:
            sets.append("target_platform_key = ?")
            params.append(target_platform_key)
        if target_section_key is not ...:
            sets.append("target_section_key = ?")
            params.append(target_section_key)
        if target_subsection_key is not ...:
            sets.append("target_subsection_key = ?")
            params.append(target_subsection_key)
        if target_link_prompt_key is not ...:
            sets.append("target_link_prompt_key = ?")
            params.append(target_link_prompt_key)
        if target_link_type is not ...:
            sets.append("target_link_type = ?")
            params.append(target_link_type)
        if not sets:
            return True
        sets.append("updated_at = CURRENT_TIMESTAMP")
        params.append(service_id)
        cur = self.connection.execute(
            f"UPDATE soldium_catalog_services SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        return cur.rowcount > 0

    def list_services(
        self,
        *,
        status: str | None = None,
        search: str | None = None,
        source: str | None = None,
        service_type: str | None = None,
        ordering_mode: str | None = None,
        price: str | None = None,
        pricing_mode: str | None = None,
        under_entry_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[CatalogService], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if under_entry_id:
            # Placement filter: service entry must be a descendant of the node entry.
            # Uses Catalog Structure (entries tree) — not name/emoji/target_policy.
            under = str(under_entry_id).strip()
            descendants = self.descendant_entry_ids(under)
            if not descendants:
                clauses.append("1 = 0")
            else:
                placeholders = ", ".join("?" for _ in descendants)
                clauses.append(
                    f"""
                    EXISTS (
                      SELECT 1 FROM soldium_catalog_entries e_place
                      WHERE e_place.service_id = s.id
                        AND e_place.id IN ({placeholders})
                    )
                    """
                )
                params.extend(sorted(descendants))
        if status:
            clauses.append("s.status = ?")
            params.append(status)
        if search:
            term = search.strip()
            # Exact Provider Service ID (opaque TEXT) OR name/note substring.
            # Never fuzzy-match external IDs — equality only.
            clauses.append(
                """
                (
                  EXISTS (
                    SELECT 1 FROM soldium_catalog_execution_sources xs
                    WHERE xs.service_id = s.id
                      AND xs.external_service_id = ?
                  )
                  OR s.name_ar LIKE ?
                  OR s.note_ar LIKE ?
                )
                """
            )
            like = f"%{term}%"
            params.extend([term, like, like])
            search_term = term
        else:
            search_term = ""
        if service_type:
            clauses.append("s.service_type = ?")
            params.append(service_type)
        if ordering_mode:
            clauses.append("s.ordering_mode = ?")
            params.append(ordering_mode)
        if source == "none":
            clauses.append(
                """
                NOT EXISTS (
                  SELECT 1 FROM soldium_catalog_execution_sources xs
                  WHERE xs.service_id = s.id AND xs.status = 'active'
                )
                """
            )
        elif source in {"assigned", "active"}:
            clauses.append(
                """
                EXISTS (
                  SELECT 1 FROM soldium_catalog_execution_sources xs
                  WHERE xs.service_id = s.id AND xs.status = 'active'
                )
                """
            )
        if price == "none":
            clauses.append(
                """
                NOT EXISTS (
                  SELECT 1 FROM soldium_catalog_prices xp
                  WHERE xp.service_id = s.id AND xp.status = 'active'
                )
                """
            )
        elif price in {"assigned", "active"}:
            clauses.append(
                """
                EXISTS (
                  SELECT 1 FROM soldium_catalog_prices xp
                  WHERE xp.service_id = s.id AND xp.status = 'active'
                )
                """
            )
        if pricing_mode:
            clauses.append(
                """
                EXISTS (
                  SELECT 1 FROM soldium_catalog_prices xp
                  WHERE xp.service_id = s.id
                    AND xp.status = 'active'
                    AND xp.pricing_mode = ?
                )
                """
            )
            params.append(pricing_mode)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = int(
            self.connection.execute(
                f"SELECT COUNT(*) FROM soldium_catalog_services s {where}",
                params,
            ).fetchone()[0]
        )
        # Prefer active exact Provider ID matches, then any historical exact match,
        # then name/note hits — never fuzzy on external IDs.
        if search_term:
            order_sql = """
            ORDER BY
              CASE
                WHEN EXISTS (
                  SELECT 1 FROM soldium_catalog_execution_sources xs
                  WHERE xs.service_id = s.id
                    AND xs.status = 'active'
                    AND xs.external_service_id = ?
                ) THEN 0
                WHEN EXISTS (
                  SELECT 1 FROM soldium_catalog_execution_sources xs
                  WHERE xs.service_id = s.id
                    AND xs.external_service_id = ?
                ) THEN 1
                ELSE 2
              END,
              s.updated_at DESC,
              s.name_ar
            """
            order_params: list[Any] = [search_term, search_term]
        else:
            order_sql = "ORDER BY s.updated_at DESC, s.name_ar"
            order_params = []
        rows = self.connection.execute(
            f"""
            SELECT s.*, e.id AS entry_id, e.parent_entry_id, e.sort_order
            FROM soldium_catalog_services s
            LEFT JOIN soldium_catalog_entries e ON e.service_id = s.id
            {where}
            {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*params, *order_params, limit, offset],
        ).fetchall()
        out: list[CatalogService] = []
        for row in rows:
            svc = _row_service(row)
            svc.entry_id = str(row["entry_id"]) if row["entry_id"] else None
            svc.parent_entry_id = (
                str(row["parent_entry_id"]) if row["parent_entry_id"] else None
            )
            svc.sort_order = int(row["sort_order"]) if row["sort_order"] is not None else None
            out.append(svc)
        return out, total

    # ── nodes ──

    def get_node(self, node_id: str) -> CatalogNode | None:
        row = self.connection.execute(
            "SELECT * FROM soldium_catalog_nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        return _row_node(row) if row else None

    def insert_node(self, node: CatalogNode) -> None:
        self.connection.execute(
            """
            INSERT INTO soldium_catalog_nodes (id, name_ar, note_ar, status)
            VALUES (?, ?, ?, ?)
            """,
            (node.id, node.name_ar, node.note_ar, node.status),
        )

    def update_node_fields(
        self,
        node_id: str,
        *,
        name_ar: str | None = None,
        note_ar: str | None = None,
        status: str | None = None,
    ) -> bool:
        sets: list[str] = []
        params: list[Any] = []
        if name_ar is not None:
            sets.append("name_ar = ?")
            params.append(name_ar)
        if note_ar is not None:
            sets.append("note_ar = ?")
            params.append(note_ar)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if not sets:
            return True
        sets.append("updated_at = CURRENT_TIMESTAMP")
        params.append(node_id)
        cur = self.connection.execute(
            f"UPDATE soldium_catalog_nodes SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        return cur.rowcount > 0

    # ── entries ──

    def get_entry(self, entry_id: str) -> CatalogEntry | None:
        row = self.connection.execute(
            "SELECT * FROM soldium_catalog_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        return _row_entry(row) if row else None

    def get_entry_for_service(self, service_id: str) -> CatalogEntry | None:
        row = self.connection.execute(
            "SELECT * FROM soldium_catalog_entries WHERE service_id = ?",
            (service_id,),
        ).fetchone()
        return _row_entry(row) if row else None

    def get_entry_for_node(self, node_id: str) -> CatalogEntry | None:
        row = self.connection.execute(
            "SELECT * FROM soldium_catalog_entries WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        return _row_entry(row) if row else None

    def insert_entry(self, entry: CatalogEntry) -> None:
        self.connection.execute(
            """
            INSERT INTO soldium_catalog_entries (
                id, parent_entry_id, entry_type, node_id, service_id, sort_order
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                entry.id,
                entry.parent_entry_id,
                entry.entry_type,
                entry.node_id,
                entry.service_id,
                entry.sort_order,
            ),
        )

    def delete_entry(self, entry_id: str) -> None:
        self.connection.execute(
            "DELETE FROM soldium_catalog_entries WHERE id = ?",
            (entry_id,),
        )

    def next_sort_order(self, parent_entry_id: str | None) -> int:
        row = self.connection.execute(
            """
            SELECT COALESCE(MAX(sort_order), -1) + 1 AS nxt
            FROM soldium_catalog_entries
            WHERE IFNULL(parent_entry_id, '') = IFNULL(?, '')
            """,
            (parent_entry_id,),
        ).fetchone()
        return int(row["nxt"])

    def list_children(self, parent_entry_id: str | None) -> list[CatalogEntry]:
        rows = self.connection.execute(
            """
            SELECT e.*,
                   CASE
                     WHEN e.entry_type = 'node' THEN n.name_ar
                     ELSE s.name_ar
                   END AS name_ar,
                   CASE
                     WHEN e.entry_type = 'node' THEN n.status
                     ELSE s.status
                   END AS status
            FROM soldium_catalog_entries e
            LEFT JOIN soldium_catalog_nodes n ON n.id = e.node_id
            LEFT JOIN soldium_catalog_services s ON s.id = e.service_id
            WHERE IFNULL(e.parent_entry_id, '') = IFNULL(?, '')
            ORDER BY e.sort_order ASC, e.id ASC
            """,
            (parent_entry_id,),
        ).fetchall()
        return [_row_entry(r) for r in rows]

    def list_all_entries_hydrated(self) -> list[CatalogEntry]:
        rows = self.connection.execute(
            """
            SELECT e.*,
                   CASE
                     WHEN e.entry_type = 'node' THEN n.name_ar
                     ELSE s.name_ar
                   END AS name_ar,
                   CASE
                     WHEN e.entry_type = 'node' THEN n.status
                     ELSE s.status
                   END AS status
            FROM soldium_catalog_entries e
            LEFT JOIN soldium_catalog_nodes n ON n.id = e.node_id
            LEFT JOIN soldium_catalog_services s ON s.id = e.service_id
            ORDER BY e.parent_entry_id, e.sort_order, e.id
            """
        ).fetchall()
        return [_row_entry(r) for r in rows]

    def set_entry_parent_and_order(
        self,
        entry_id: str,
        parent_entry_id: str | None,
        sort_order: int,
    ) -> None:
        self.connection.execute(
            """
            UPDATE soldium_catalog_entries
            SET parent_entry_id = ?,
                sort_order = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (parent_entry_id, sort_order, entry_id),
        )

    def update_sort_order(self, entry_id: str, sort_order: int) -> None:
        self.connection.execute(
            """
            UPDATE soldium_catalog_entries
            SET sort_order = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (sort_order, entry_id),
        )

    def count_children(self, parent_entry_id: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS c FROM soldium_catalog_entries WHERE parent_entry_id = ?",
            (parent_entry_id,),
        ).fetchone()
        return int(row["c"])

    def ancestor_entry_ids(self, entry_id: str | None) -> list[str]:
        """Walk parents upward; returns [entry_id, parent, ..., root]."""
        if not entry_id:
            return []
        chain: list[str] = []
        current: str | None = entry_id
        seen: set[str] = set()
        while current:
            if current in seen:
                break
            seen.add(current)
            chain.append(current)
            row = self.connection.execute(
                "SELECT parent_entry_id FROM soldium_catalog_entries WHERE id = ?",
                (current,),
            ).fetchone()
            if not row:
                break
            current = str(row["parent_entry_id"]) if row["parent_entry_id"] else None
        return chain

    def descendant_entry_ids(self, entry_id: str) -> set[str]:
        """All entry ids under entry_id (not including itself)."""
        result: set[str] = set()
        stack = [entry_id]
        while stack:
            parent = stack.pop()
            rows = self.connection.execute(
                "SELECT id FROM soldium_catalog_entries WHERE parent_entry_id = ?",
                (parent,),
            ).fetchall()
            for row in rows:
                cid = str(row["id"])
                if cid not in result:
                    result.add(cid)
                    stack.append(cid)
        return result

    def breadcrumb_names(self, entry_id: str | None) -> list[str]:
        if not entry_id:
            return []
        names: list[str] = []
        for eid in reversed(self.ancestor_entry_ids(entry_id)):
            row = self.connection.execute(
                """
                SELECT
                  CASE
                    WHEN e.entry_type = 'node' THEN n.name_ar
                    ELSE s.name_ar
                  END AS name_ar
                FROM soldium_catalog_entries e
                LEFT JOIN soldium_catalog_nodes n ON n.id = e.node_id
                LEFT JOIN soldium_catalog_services s ON s.id = e.service_id
                WHERE e.id = ?
                """,
                (eid,),
            ).fetchone()
            if row and row["name_ar"]:
                names.append(str(row["name_ar"]))
        return names

    # ── execution sources (Phase 3) ──

    def _row_execution_source(self, row: sqlite3.Row) -> ExecutionSource:
        provider_slug = str(row["provider_slug"])
        account_key = str(row["provider_account_key"])
        provider_name = provider_slug
        account_display = account_key
        try:
            prow = self.connection.execute(
                "SELECT name FROM providers WHERE slug = ?",
                (provider_slug,),
            ).fetchone()
            if prow and prow["name"]:
                provider_name = str(prow["name"])
        except sqlite3.OperationalError:
            pass
        try:
            cols = {
                str(r[1])
                for r in self.connection.execute(
                    "PRAGMA table_info(provider_accounts)"
                ).fetchall()
            }
            if "display_name" in cols:
                arow = self.connection.execute(
                    """
                    SELECT display_name FROM provider_accounts
                    WHERE provider_slug = ? AND account_key = ?
                    """,
                    (provider_slug, account_key),
                ).fetchone()
                if arow and str(arow["display_name"] or "").strip():
                    account_display = str(arow["display_name"]).strip()
        except sqlite3.OperationalError:
            pass
        return ExecutionSource(
            id=str(row["id"]),
            service_id=str(row["service_id"]),
            provider_slug=provider_slug,
            provider_account_key=account_key,
            external_service_id=str(row["external_service_id"]),
            status=str(row["status"] or "active"),  # type: ignore[arg-type]
            assigned_at=row["assigned_at"] if "assigned_at" in row.keys() else None,
            ended_at=row["ended_at"] if "ended_at" in row.keys() else None,
            created_at=row["created_at"] if "created_at" in row.keys() else None,
            updated_at=row["updated_at"] if "updated_at" in row.keys() else None,
            provider_name=provider_name,
            account_display_name=account_display,
        )

    _SOURCE_SELECT = """
        SELECT es.*
        FROM soldium_catalog_execution_sources es
    """

    def get_active_execution_source(self, service_id: str) -> ExecutionSource | None:
        row = self.connection.execute(
            self._SOURCE_SELECT
            + " WHERE es.service_id = ? AND es.status = 'active' LIMIT 1",
            (service_id,),
        ).fetchone()
        return self._row_execution_source(row) if row else None

    def list_execution_sources(self, service_id: str) -> list[ExecutionSource]:
        rows = self.connection.execute(
            self._SOURCE_SELECT
            + """
            WHERE es.service_id = ?
            ORDER BY
              CASE WHEN es.status = 'active' THEN 0 ELSE 1 END,
              es.assigned_at DESC,
              es.id DESC
            """,
            (service_id,),
        ).fetchall()
        return [self._row_execution_source(r) for r in rows]

    def insert_execution_source(self, source: ExecutionSource) -> None:
        self.connection.execute(
            """
            INSERT INTO soldium_catalog_execution_sources (
                id, service_id, provider_slug, provider_account_key,
                external_service_id, status, assigned_at, ended_at
            ) VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?)
            """,
            (
                source.id,
                source.service_id,
                source.provider_slug,
                source.provider_account_key,
                source.external_service_id,
                source.status,
                source.assigned_at,
                source.ended_at,
            ),
        )

    def end_execution_source(self, source_id: str) -> None:
        self.connection.execute(
            """
            UPDATE soldium_catalog_execution_sources
            SET status = 'historical',
                ended_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'active'
            """,
            (source_id,),
        )

    def insert_execution_source_event(
        self,
        *,
        event_id: str,
        service_id: str,
        previous: ExecutionSource | None,
        new_provider_slug: str,
        new_provider_account_key: str,
        new_external_service_id: str,
        actor: str | None = None,
        operation: str = "change_execution_source",
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO soldium_catalog_execution_source_events (
                id, service_id, operation,
                previous_provider_slug, previous_provider_account_key,
                previous_external_service_id,
                new_provider_slug, new_provider_account_key,
                new_external_service_id, actor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                service_id,
                operation,
                previous.provider_slug if previous else None,
                previous.provider_account_key if previous else None,
                previous.external_service_id if previous else None,
                new_provider_slug,
                new_provider_account_key,
                new_external_service_id,
                actor,
            ),
        )

    def list_execution_source_events(
        self, service_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM soldium_catalog_execution_source_events
            WHERE service_id = ?
            ORDER BY datetime(created_at) DESC, rowid DESC
            LIMIT ?
            """,
            (service_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def find_provider(self, provider_slug: str) -> tuple[str, str] | None:
        """Return (slug, name) if provider row exists."""
        try:
            row = self.connection.execute(
                "SELECT slug, name FROM providers WHERE slug = ?",
                (provider_slug,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if not row:
            return None
        return str(row["slug"]), str(row["name"] or row["slug"])

    def find_provider_account(
        self, provider_slug: str, account_key: str
    ) -> tuple[str, str, str] | None:
        """Return (provider_slug, account_key, display_name) if account exists."""
        try:
            cols = {
                str(r[1])
                for r in self.connection.execute(
                    "PRAGMA table_info(provider_accounts)"
                ).fetchall()
            }
            select_display = "display_name" if "display_name" in cols else "'' AS display_name"
            row = self.connection.execute(
                f"""
                SELECT provider_slug, account_key, {select_display}
                FROM provider_accounts
                WHERE provider_slug = ? AND account_key = ?
                """,
                (provider_slug, account_key),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if not row:
            return None
        display = str(row["display_name"] or "").strip()
        return str(row["provider_slug"]), str(row["account_key"]), display

    def get_active_sources_for_services(
        self, service_ids: list[str]
    ) -> dict[str, ExecutionSource]:
        if not service_ids:
            return {}
        placeholders = ",".join("?" for _ in service_ids)
        rows = self.connection.execute(
            self._SOURCE_SELECT
            + f" WHERE es.service_id IN ({placeholders}) AND es.status = 'active'",
            service_ids,
        ).fetchall()
        return {str(r["service_id"]): self._row_execution_source(r) for r in rows}

    # ── prices (Phase 4B) ──

    def _row_price(self, row: sqlite3.Row) -> CatalogPrice:
        return CatalogPrice(
            id=str(row["id"]),
            service_id=str(row["service_id"]),
            amount_millimes=int(row["amount_millimes"]),
            currency=str(row["currency"] or "MAD"),
            pricing_mode=str(row["pricing_mode"]),
            status=str(row["status"] or "active"),  # type: ignore[arg-type]
            effective_from=row["effective_from"] if "effective_from" in row.keys() else None,
            effective_to=row["effective_to"] if "effective_to" in row.keys() else None,
            created_at=row["created_at"] if "created_at" in row.keys() else None,
            updated_at=row["updated_at"] if "updated_at" in row.keys() else None,
        )

    def get_active_price(self, service_id: str) -> CatalogPrice | None:
        row = self.connection.execute(
            """
            SELECT * FROM soldium_catalog_prices
            WHERE service_id = ? AND status = 'active'
            LIMIT 1
            """,
            (service_id,),
        ).fetchone()
        return self._row_price(row) if row else None

    def list_prices(self, service_id: str) -> list[CatalogPrice]:
        rows = self.connection.execute(
            """
            SELECT * FROM soldium_catalog_prices
            WHERE service_id = ?
            ORDER BY
              CASE WHEN status = 'active' THEN 0 ELSE 1 END,
              effective_from DESC,
              id DESC
            """,
            (service_id,),
        ).fetchall()
        return [self._row_price(r) for r in rows]

    def insert_price(self, price: CatalogPrice) -> None:
        self.connection.execute(
            """
            INSERT INTO soldium_catalog_prices (
                id, service_id, amount_millimes, currency, pricing_mode,
                status, effective_from, effective_to
            ) VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?)
            """,
            (
                price.id,
                price.service_id,
                price.amount_millimes,
                price.currency,
                price.pricing_mode,
                price.status,
                price.effective_from,
                price.effective_to,
            ),
        )

    def end_price(self, price_id: str) -> None:
        self.connection.execute(
            """
            UPDATE soldium_catalog_prices
            SET status = 'historical',
                effective_to = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'active'
            """,
            (price_id,),
        )

    def get_active_prices_for_services(
        self, service_ids: list[str]
    ) -> dict[str, CatalogPrice]:
        if not service_ids:
            return {}
        placeholders = ",".join("?" for _ in service_ids)
        rows = self.connection.execute(
            f"""
            SELECT * FROM soldium_catalog_prices
            WHERE service_id IN ({placeholders}) AND status = 'active'
            """,
            service_ids,
        ).fetchall()
        return {str(r["service_id"]): self._row_price(r) for r in rows}

    def list_latest_publication_rows(self) -> list[sqlite3.Row]:
        """Latest publication history row per service (published_at DESC, rowid DESC).

        Does not interpret publish vs unpublish — callers filter event_type.
        """
        return list(
            self.connection.execute(
                """
                SELECT * FROM (
                    SELECT
                        p.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY p.service_id
                            ORDER BY p.published_at DESC, p.rowid DESC
                        ) AS _rn
                    FROM soldium_catalog_publications AS p
                )
                WHERE _rn = 1
                ORDER BY service_id ASC
                """
            ).fetchall()
        )
