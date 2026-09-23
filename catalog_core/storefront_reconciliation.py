# -*- coding: utf-8 -*-
"""Legacy → Catalog customer storefront reconciliation.

Corrects Catalog *data* (labels, sort_order, publication) so the Catalog
customer storefront reproduces the established Legacy customer storefront,
while Catalog remains the sole SoT for Telegram when STOREFRONT_BACKEND=catalog.

Rules:
- Never mutate ``smm_services`` / Legacy storefront code.
- Never invent execution sources, prices, or provider identities.
- Publish only Legacy-customer-visible services that pass Catalog readiness.
- Do not publish Catalog-only / Legacy-hidden services for coverage cosmetics.
- Placement SoT remains the Catalog entries tree (no platform_key routing).
"""

from __future__ import annotations

import shutil
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from catalog_core.errors import CatalogPublishError
from catalog_core.legacy_migration import (
    legacy_node_key_platform,
    legacy_node_key_section,
    legacy_node_key_subsection,
)
from catalog_core.phase9g_business_decision_pack import _published_ids
from catalog_core.publication import CatalogPublicationService
from catalog_core.readiness import evaluate_service_readiness
from catalog_core.repository import CatalogRepository
from catalog_core.service import CatalogCoreService
from catalog_core.storefront_gateway import CatalogStorefrontBackend
from catalog_core.storefront_projection import PublishedStorefrontProjection

# Customer-facing platform button labels — must match
# ``soldium-bot/keyboards/orders.py`` ``_LEGACY_PLATFORM_LABELS`` + BTN_SUBSCRIPTIONS.
LEGACY_CUSTOMER_PLATFORM_LABELS: dict[str, str] = {
    "instagram": "📸 إنستغرام 📸",
    "facebook": "🔵 فيسبوك 🔵",
    "tiktok": "🎵 تيك توك 🎵",
    "youtube": "🔴 يوتيوب 🔴",
    "telegram": "✈️ تيليجرام ✈️",
    "x": "𝕏 تويتر",
    "subscriptions": "🔄 إشتراكات",
}

# Root button order in Legacy Telegram platforms menu.
LEGACY_PLATFORM_BUTTON_ORDER: list[str] = [
    "instagram",
    "facebook",
    "tiktok",
    "youtube",
    "telegram",
    "x",
    "subscriptions",
]

# Mirrored from soldium-bot/services_catalog_db.py (ordering contract).
PLATFORM_SECTION_ORDER: dict[str, list[str]] = {
    "instagram": ["likes", "views", "followers", "interaction"],
    "facebook": ["reactions", "video_reels_views", "followers_members", "live_stream_views"],
    "tiktok": ["likes", "views", "followers"],
    "telegram": [
        "post_interactions",
        "post_views",
        "channel_members",
        "post_share",
        "start_bot",
        "automatic_interactions",
    ],
}

PLATFORM_SECTION_TITLES: dict[str, dict[str, str]] = {
    "telegram": {
        "post_interactions": "⚡ تفاعلات المنشورات 👍❤️🔥 ⚡",
        "post_views": "👁️ مشاهدة منشور 👁️",
        "channel_members": "👥 أعضاء القنوات والمجموعات 👥",
        "post_share": "📤 مشاركة المنشور (Share) 📤",
        "start_bot": "🤖 بدء البوت (Start Bot) 🤖",
        "automatic_interactions": "🔄 تفاعلات تلقائية للمنشورات القادمة 🔄",
    },
}

PLATFORM_SUBSECTION_TITLES: dict[str, dict[tuple[str, str], str]] = {
    "telegram": {
        ("channel_members", "global_members"): "👥 أعضاء عالمي (اقتصادي/عالي الجودة) 👥",
        ("channel_members", "targeted_members"): "🌍 أعضاء مستهدفين (دول) 🌍",
        ("channel_members", "premium_members"): "⭐ أعضاء بريميوم (لدعم الرانك) ⭐",
        ("channel_members", "member_bundles"): "📦 باقات متكاملة (أعضاء + مشاهدات) 📦",
        ("channel_members", "arab_mix_members"): "🇲🇦 أعضاء عرب (Mix) 🇲🇦",
        ("post_views", "past_posts"): "🔙 منشورات سابقة (Auto) 🔙",
        ("post_views", "future_posts"): "🔮 منشورات قادمة (Future) 🔮",
        ("post_views", "premium_views"): "🌟 مشاهدات بريميوم (تلقائية) 🌟",
        ("post_interactions", "normal_interactions"): "⚡ تفاعلات حسابات عادية (الفورية) ⚡",
        ("post_interactions", "premium_interactions"): "🌟 تفاعلات حسابات بريميوم (الفورية) 🌟",
        ("post_interactions", "automatic_interactions"): "🔄 تفاعلات تلقائية للمنشورات القادمة 🔄",
        ("automatic_interactions", "positive_mix"): "✨ ميكس تفاعلات إيجابي (👍❤️🔥🥰👏🎉💯) ✨",
        ("automatic_interactions", "negative_mix"): "💀 ميكس تفاعلات سلبي (👎💔🤨🙄🤬🖕💩🤡🤮) 💀",
        ("automatic_interactions", "specific_emoji"): "🎯 تفاعل محدد (إيموجي واحد) 🎯",
    },
}

SUBSCRIPTIONS_SECTION_TITLES: dict[str, str] = {
    "iptv_wc2026": "⚽🌍 حسابات ايبتيفي - كأس العالم 2026",
    "iptv_panel": "لوحة - ايبتيفي بانل [ إربح من بيع حسابات ايبتيفي]",
}

SUBSCRIPTIONS_SECTION_ORDER: list[str] = ["iptv_wc2026", "iptv_panel"]

TELEGRAM_POST_INTERACTIONS_SUBSECTION_ORDER: list[str] = [
    "normal_interactions",
    "premium_interactions",
    "automatic_interactions",
]

PUBLISHED_BY = "storefront_reconciliation"


@dataclass
class ReconciliationReport:
    dry_run: bool
    timestamp: str
    backup_path: str | None
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    label_updates: list[dict[str, Any]] = field(default_factory=list)
    sort_updates: list[dict[str, Any]] = field(default_factory=list)
    placement_updates: list[dict[str, Any]] = field(default_factory=list)
    execution_repairs: list[dict[str, Any]] = field(default_factory=list)
    publish_results: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    parity: dict[str, Any] = field(default_factory=dict)
    safe_to_switch_telegram: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "timestamp": self.timestamp,
            "backup_path": self.backup_path,
            "before": self.before,
            "after": self.after,
            "label_updates": self.label_updates,
            "sort_updates": self.sort_updates,
            "placement_updates": self.placement_updates,
            "execution_repairs": self.execution_repairs,
            "publish_results": self.publish_results,
            "blocked": self.blocked,
            "parity": self.parity,
            "safe_to_switch_telegram": self.safe_to_switch_telegram,
            "notes": self.notes,
        }


def _legacy_active_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT catalog_id, name_ar, platform_key, section_key, subsection_key,
                   local_price_dh, is_active
            FROM smm_services
            WHERE is_active = 1 AND platform_key != ''
            ORDER BY platform_key, section_key, subsection_key, name_ar
            """
        )
    )


def _bridge_maps(conn: sqlite3.Connection) -> tuple[dict[str, str], dict[str, str]]:
    legacy_to_svc: dict[str, str] = {}
    svc_to_legacy: dict[str, str] = {}
    for row in conn.execute(
        "SELECT legacy_catalog_id, soldium_service_id FROM soldium_catalog_legacy_bridge"
    ):
        lid = str(row["legacy_catalog_id"])
        sid = str(row["soldium_service_id"])
        legacy_to_svc[lid] = sid
        svc_to_legacy[sid] = lid
    return legacy_to_svc, svc_to_legacy


def _node_bridge_map(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in conn.execute(
        """
        SELECT legacy_node_key, soldium_node_id, soldium_entry_id
        FROM soldium_catalog_legacy_node_bridge
        """
    ):
        out[str(row["legacy_node_key"])] = {
            "node_id": str(row["soldium_node_id"]),
            "entry_id": str(row["soldium_entry_id"]),
        }
    return out


def capture_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    published = _published_ids(conn)
    proj = PublishedStorefrontProjection(conn).build()
    tree = CatalogStorefrontBackend(conn).navigation_tree()
    legacy_n = conn.execute(
        "SELECT COUNT(*) AS c FROM smm_services "
        "WHERE is_active = 1 AND platform_key != ''"
    ).fetchone()["c"]
    return {
        "legacy_active": int(legacy_n),
        "catalog_services": int(
            conn.execute("SELECT COUNT(*) AS c FROM soldium_catalog_services").fetchone()[
                "c"
            ]
        ),
        "bridges": int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM soldium_catalog_legacy_bridge"
            ).fetchone()["c"]
        ),
        "nodes": int(
            conn.execute("SELECT COUNT(*) AS c FROM soldium_catalog_nodes").fetchone()[
                "c"
            ]
        ),
        "entries": int(
            conn.execute("SELECT COUNT(*) AS c FROM soldium_catalog_entries").fetchone()[
                "c"
            ]
        ),
        "publication_rows": int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM soldium_catalog_publications"
            ).fetchone()["c"]
        ),
        "published_services": len(published),
        "projected_services": len(proj.services),
        "catalog_roots": len(tree),
        "catalog_root_titles": {
            k: str((v or {}).get("title") or "") for k, v in tree.items()
        },
    }


def backup_database(db_path: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = db_path.with_name(f"{db_path.name}.bak.storefront_recon_{ts}")
    shutil.copy2(db_path, dest)
    return dest


def reconcile_platform_labels(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Write Legacy customer button labels onto Catalog platform root nodes."""
    repo = CatalogRepository(conn)
    nodes = _node_bridge_map(conn)
    updates: list[dict[str, Any]] = []
    for platform_key, label in LEGACY_CUSTOMER_PLATFORM_LABELS.items():
        key = legacy_node_key_platform(platform_key)
        bridge = nodes.get(key)
        if not bridge:
            updates.append(
                {
                    "legacy_node_key": key,
                    "action": "missing_node",
                    "desired_name_ar": label,
                }
            )
            continue
        node = repo.get_node(bridge["node_id"])
        current = str(getattr(node, "name_ar", "") or "") if node else ""
        row = {
            "legacy_node_key": key,
            "node_id": bridge["node_id"],
            "entry_id": bridge["entry_id"],
            "before": current,
            "after": label,
            "changed": current != label,
        }
        if current != label and not dry_run:
            repo.update_node_fields(bridge["node_id"], name_ar=label)
            row["applied"] = True
        else:
            row["applied"] = False
        updates.append(row)
    return updates


def reconcile_section_labels(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Apply Legacy display-title overrides onto Catalog section/subsection nodes."""
    repo = CatalogRepository(conn)
    nodes = _node_bridge_map(conn)
    updates: list[dict[str, Any]] = []

    desired: dict[str, str] = {}
    for pk, mapping in PLATFORM_SECTION_TITLES.items():
        for sk, title in mapping.items():
            desired[legacy_node_key_section(pk, sk)] = title
    for pk, mapping in PLATFORM_SUBSECTION_TITLES.items():
        for (sk, ssk), title in mapping.items():
            desired[legacy_node_key_subsection(pk, sk, ssk)] = title
    for sk, title in SUBSCRIPTIONS_SECTION_TITLES.items():
        desired[legacy_node_key_section("subscriptions", sk)] = title

    for key, label in desired.items():
        bridge = nodes.get(key)
        if not bridge:
            updates.append(
                {
                    "legacy_node_key": key,
                    "action": "missing_node",
                    "desired_name_ar": label,
                }
            )
            continue
        node = repo.get_node(bridge["node_id"])
        current = str(getattr(node, "name_ar", "") or "") if node else ""
        row = {
            "legacy_node_key": key,
            "node_id": bridge["node_id"],
            "before": current,
            "after": label,
            "changed": current != label,
        }
        if current != label and not dry_run:
            repo.update_node_fields(bridge["node_id"], name_ar=label)
            row["applied"] = True
        else:
            row["applied"] = False
        updates.append(row)
    return updates


def _preferred_child_keys(
    platform_key: str,
    *,
    level: str,
    section_key: str | None = None,
) -> list[str] | None:
    if level == "platform":
        return list(LEGACY_PLATFORM_BUTTON_ORDER)
    if level == "section":
        if platform_key == "subscriptions":
            return list(SUBSCRIPTIONS_SECTION_ORDER)
        return list(PLATFORM_SECTION_ORDER.get(platform_key) or [])
    if level == "subsection" and platform_key == "telegram" and section_key:
        if section_key == "post_interactions":
            return list(TELEGRAM_POST_INTERACTIONS_SUBSECTION_ORDER)
    return None


def _reorder_siblings(
    conn: sqlite3.Connection,
    *,
    parent_entry_id: str | None,
    preferred_entry_ids: list[str],
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Assign contiguous sort_order under a parent: preferred first, then leftovers."""
    repo = CatalogRepository(conn)
    children = [
        e
        for e in repo.list_children(parent_entry_id)
        if e.entry_type == "node" or e.entry_type == "service"
    ]
    by_id = {e.id: e for e in children}
    ordered_ids: list[str] = []
    for eid in preferred_entry_ids:
        if eid in by_id and eid not in ordered_ids:
            ordered_ids.append(eid)
    for e in sorted(children, key=lambda x: (x.sort_order, x.id)):
        if e.id not in ordered_ids:
            ordered_ids.append(e.id)

    updates: list[dict[str, Any]] = []
    for idx, eid in enumerate(ordered_ids):
        before = int(by_id[eid].sort_order or 0)
        row = {
            "entry_id": eid,
            "parent_entry_id": parent_entry_id,
            "before": before,
            "after": idx,
            "changed": before != idx,
        }
        if before != idx and not dry_run:
            repo.update_sort_order(eid, idx)
            row["applied"] = True
        else:
            row["applied"] = False
        updates.append(row)
    return updates


def reconcile_ordering(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
    legacy_tree_loader: Callable[[], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Align Catalog entry.sort_order with Legacy customer ordering."""
    nodes = _node_bridge_map(conn)
    legacy_to_svc, _ = _bridge_maps(conn)
    repo = CatalogRepository(conn)
    updates: list[dict[str, Any]] = []

    # 1) Platform root order
    preferred_roots: list[str] = []
    for pk in LEGACY_PLATFORM_BUTTON_ORDER:
        bridge = nodes.get(legacy_node_key_platform(pk))
        if bridge:
            preferred_roots.append(bridge["entry_id"])
    updates.extend(
        _reorder_siblings(
            conn,
            parent_entry_id=None,
            preferred_entry_ids=preferred_roots,
            dry_run=dry_run,
        )
    )

    # 2) Section order under each platform
    for pk in LEGACY_PLATFORM_BUTTON_ORDER:
        plat = nodes.get(legacy_node_key_platform(pk))
        if not plat:
            continue
        preferred_secs: list[str] = []
        for sk in _preferred_child_keys(pk, level="section") or []:
            b = nodes.get(legacy_node_key_section(pk, sk))
            if b:
                preferred_secs.append(b["entry_id"])
        updates.extend(
            _reorder_siblings(
                conn,
                parent_entry_id=plat["entry_id"],
                preferred_entry_ids=preferred_secs,
                dry_run=dry_run,
            )
        )

        # 3) Subsection order under known sections
        section_keys = list((_preferred_child_keys(pk, level="section") or []))
        # Also include any section bridges under this platform
        for key, bridge in nodes.items():
            if key.startswith(f"section:{pk}/"):
                sk = key.split("/", 1)[-1]
                if sk not in section_keys:
                    section_keys.append(sk)
        for sk in section_keys:
            sec = nodes.get(legacy_node_key_section(pk, sk))
            if not sec:
                continue
            preferred_subs: list[str] = []
            for ssk in _preferred_child_keys(pk, level="subsection", section_key=sk) or []:
                b = nodes.get(legacy_node_key_subsection(pk, sk, ssk))
                if b:
                    preferred_subs.append(b["entry_id"])
            # Prefer remaining subsection bridges in key order
            for key, bridge in sorted(nodes.items()):
                if key.startswith(f"subsection:{pk}/{sk}/"):
                    if bridge["entry_id"] not in preferred_subs:
                        preferred_subs.append(bridge["entry_id"])
            updates.extend(
                _reorder_siblings(
                    conn,
                    parent_entry_id=sec["entry_id"],
                    preferred_entry_ids=preferred_subs,
                    dry_run=dry_run,
                )
            )

    # 4) Service order from Legacy tree walk (price / SECTION_ITEM_ORDER already applied)
    if legacy_tree_loader is not None:
        tree = legacy_tree_loader()
        updates.extend(
            _reconcile_service_sort_from_legacy_tree(
                conn,
                tree=tree,
                legacy_to_svc=legacy_to_svc,
                repo=repo,
                dry_run=dry_run,
            )
        )
    return updates


def _iter_legacy_services(
    tree: dict[str, Any],
) -> list[tuple[str, str | None, str | None, list[str]]]:
    """Yield (platform_key, section_key, subsection_key, ordered_legacy_ids)."""
    out: list[tuple[str, str | None, str | None, list[str]]] = []

    def _ids(items: list) -> list[str]:
        return [str(it.get("id")) for it in items if it and it.get("id") is not None]

    for pk, platform in tree.items():
        if not isinstance(platform, dict):
            continue
        direct = list(platform.get("direct_items") or [])
        if direct:
            out.append((str(pk), "direct", None, _ids(direct)))
        root_items = list(platform.get("items") or [])
        if root_items:
            out.append((str(pk), None, None, _ids(root_items)))
        for sk, section in (platform.get("sections") or {}).items():
            if not isinstance(section, dict):
                continue
            s_items = list(section.get("items") or [])
            if s_items:
                out.append((str(pk), str(sk), None, _ids(s_items)))
            for ssk, sub in (section.get("subsections") or {}).items():
                if not isinstance(sub, dict):
                    continue
                sub_items = list(sub.get("items") or [])
                if sub_items:
                    out.append((str(pk), str(sk), str(ssk), _ids(sub_items)))
    return out


def _reconcile_service_sort_from_legacy_tree(
    conn: sqlite3.Connection,
    *,
    tree: dict[str, Any],
    legacy_to_svc: dict[str, str],
    repo: CatalogRepository,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Reorder only service entries under each parent (nodes keep their orders).

    Telegram Catalog menus render nodes and services in separate loops, so
    service sort_order is independent of sibling node sort_order.
    """
    updates: list[dict[str, Any]] = []
    by_parent: dict[str | None, list[str]] = defaultdict(list)
    seen_entry: set[str] = set()
    for _pk, _sk, _ssk, legacy_ids in _iter_legacy_services(tree):
        for lid in legacy_ids:
            sid = legacy_to_svc.get(str(lid))
            if not sid:
                continue
            entry = repo.get_entry_for_service(sid)
            if not entry or entry.id in seen_entry:
                continue
            seen_entry.add(entry.id)
            by_parent[entry.parent_entry_id].append(entry.id)

    for parent, preferred_service_entry_ids in by_parent.items():
        # Only touch service-type siblings; leave node sort_orders unchanged.
        service_children = [
            e for e in repo.list_children(parent) if e.entry_type == "service"
        ]
        by_id = {e.id: e for e in service_children}
        ordered: list[str] = []
        for eid in preferred_service_entry_ids:
            if eid in by_id and eid not in ordered:
                ordered.append(eid)
        for e in sorted(service_children, key=lambda x: (x.sort_order, x.id)):
            if e.id not in ordered:
                ordered.append(e.id)
        for idx, eid in enumerate(ordered):
            before = int(by_id[eid].sort_order or 0)
            row = {
                "entry_id": eid,
                "parent_entry_id": parent,
                "kind": "service",
                "before": before,
                "after": idx,
                "changed": before != idx,
            }
            if before != idx and not dry_run:
                repo.update_sort_order(eid, idx)
                row["applied"] = True
            else:
                row["applied"] = False
            updates.append(row)
    return updates


def _service_is_catalog_ready(conn: sqlite3.Connection, service_id: str) -> tuple[bool, list[str]]:
    repo = CatalogRepository(conn)
    svc = repo.get_service(service_id)
    if not svc:
        return False, ["service_missing"]
    if str(svc.status or "") == "archived":
        return False, ["service_archived"]
    entry = repo.get_entry_for_service(service_id)
    if entry:
        svc.entry_id = entry.id
        svc.parent_entry_id = entry.parent_entry_id
    source = repo.get_active_execution_source(service_id)
    price = repo.get_active_price(service_id)
    rd = evaluate_service_readiness(repo, svc, source=source, price=price)
    return bool(rd.ready), [i.code for i in rd.issues]


def repair_missing_execution_from_legacy(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Backfill missing execution sources from Legacy identity when registry allows.

    Uses ``smm_services.provider_slug`` / ``provider_api_account`` /
    ``external_service_id`` only when the Catalog provider registry already
    contains that provider+account. Does not invent identities.
    """
    legacy_to_svc, _ = _bridge_maps(conn)
    repo = CatalogRepository(conn)
    core = CatalogCoreService(conn)
    repairs: list[dict[str, Any]] = []

    for row in _legacy_active_rows(conn):
        lid = str(row["catalog_id"])
        sid = legacy_to_svc.get(lid)
        if not sid:
            continue
        if repo.get_active_execution_source(sid) is not None:
            continue
        full = conn.execute(
            """
            SELECT provider_slug, provider_api_account, external_service_id
            FROM smm_services WHERE catalog_id = ?
            """,
            (lid,),
        ).fetchone()
        if not full:
            repairs.append(
                {
                    "legacy_catalog_id": lid,
                    "soldium_service_id": sid,
                    "outcome": "skipped",
                    "reason": "legacy_row_missing",
                }
            )
            continue
        slug = str(full["provider_slug"] or "").strip()
        account = str(full["provider_api_account"] or "").strip()
        external = str(full["external_service_id"] or "").strip()
        if not slug or not account or not external:
            repairs.append(
                {
                    "legacy_catalog_id": lid,
                    "soldium_service_id": sid,
                    "outcome": "skipped",
                    "reason": "incomplete_legacy_identity",
                    "provider_slug": slug or None,
                    "provider_api_account": account or None,
                    "external_service_id": external or None,
                }
            )
            continue
        if not repo.find_provider(slug) or not repo.find_provider_account(slug, account):
            repairs.append(
                {
                    "legacy_catalog_id": lid,
                    "soldium_service_id": sid,
                    "outcome": "skipped",
                    "reason": "provider_registry_missing",
                    "provider_slug": slug,
                    "provider_api_account": account,
                }
            )
            continue
        row_out = {
            "legacy_catalog_id": lid,
            "soldium_service_id": sid,
            "provider_slug": slug,
            "provider_api_account": account,
            "external_service_id": external,
        }
        if dry_run:
            row_out["outcome"] = "would_repair"
            repairs.append(row_out)
            continue
        try:
            core.change_execution_source(
                sid,
                provider_slug=slug,
                provider_account_key=account,
                external_service_id=external,
                changed_by=PUBLISHED_BY,
            )
            row_out["outcome"] = "repaired"
            repairs.append(row_out)
        except Exception as exc:  # noqa: BLE001 — capture and continue
            row_out["outcome"] = "error"
            row_out["error"] = str(exc)
            repairs.append(row_out)
    return repairs


def reconcile_direct_placement(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Move Legacy ``section_key='direct'`` services under the platform root entry.

    Legacy renders these as platform ``direct_items``. Migration often placed them
    under a synthetic ``section:.../direct`` node. Catalog SoT stays the entries
    tree — we only correct parent_entry_id.
    """
    nodes = _node_bridge_map(conn)
    legacy_to_svc, _ = _bridge_maps(conn)
    repo = CatalogRepository(conn)
    core = CatalogCoreService(conn)
    updates: list[dict[str, Any]] = []

    rows = conn.execute(
        """
        SELECT catalog_id, platform_key
        FROM smm_services
        WHERE is_active = 1 AND section_key = 'direct' AND platform_key != ''
        """
    ).fetchall()
    for row in rows:
        lid = str(row["catalog_id"])
        pk = str(row["platform_key"])
        sid = legacy_to_svc.get(lid)
        plat = nodes.get(legacy_node_key_platform(pk))
        if not sid or not plat:
            updates.append(
                {
                    "legacy_catalog_id": lid,
                    "outcome": "skipped",
                    "reason": "missing_bridge_or_platform",
                }
            )
            continue
        entry = repo.get_entry_for_service(sid)
        if not entry:
            updates.append(
                {
                    "legacy_catalog_id": lid,
                    "soldium_service_id": sid,
                    "outcome": "skipped",
                    "reason": "missing_entry",
                }
            )
            continue
        if entry.parent_entry_id == plat["entry_id"]:
            updates.append(
                {
                    "legacy_catalog_id": lid,
                    "soldium_service_id": sid,
                    "outcome": "already_at_platform_root",
                }
            )
            continue
        row_out = {
            "legacy_catalog_id": lid,
            "soldium_service_id": sid,
            "from_parent": entry.parent_entry_id,
            "to_parent": plat["entry_id"],
            "platform_key": pk,
        }
        if dry_run:
            row_out["outcome"] = "would_move"
            updates.append(row_out)
            continue
        core.move_service(sid, new_parent_entry_id=plat["entry_id"])
        row_out["outcome"] = "moved"
        updates.append(row_out)
    return updates


def reconcile_publications(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Publish Legacy-customer-visible services that are Catalog-ready.

    Does not publish Catalog-only services. Does not invent execution data.
    """
    legacy_to_svc, _ = _bridge_maps(conn)
    published = _published_ids(conn)
    pub = CatalogPublicationService(conn)
    results: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    active_rows = _legacy_active_rows(conn)
    for row in active_rows:
        lid = str(row["catalog_id"])
        sid = legacy_to_svc.get(lid)
        if not sid:
            blocked.append(
                {
                    "legacy_catalog_id": lid,
                    "reason": "missing_bridge",
                    "platform_key": row["platform_key"],
                }
            )
            continue
        if sid in published:
            results.append(
                {
                    "legacy_catalog_id": lid,
                    "soldium_service_id": sid,
                    "outcome": "already_published",
                }
            )
            continue
        ready, issues = _service_is_catalog_ready(conn, sid)
        if not ready:
            blocked.append(
                {
                    "legacy_catalog_id": lid,
                    "soldium_service_id": sid,
                    "reason": "not_ready",
                    "issues": issues,
                    "platform_key": row["platform_key"],
                    "section_key": row["section_key"],
                    "name_ar": row["name_ar"],
                }
            )
            continue
        if dry_run:
            results.append(
                {
                    "legacy_catalog_id": lid,
                    "soldium_service_id": sid,
                    "outcome": "would_publish",
                    "platform_key": row["platform_key"],
                }
            )
            continue
        try:
            pr = pub.publish(sid, published_by=PUBLISHED_BY)
            results.append(
                {
                    "legacy_catalog_id": lid,
                    "soldium_service_id": sid,
                    "outcome": pr.outcome,
                    "message_ar": pr.message_ar,
                    "platform_key": row["platform_key"],
                }
            )
        except CatalogPublishError as exc:
            blocked.append(
                {
                    "legacy_catalog_id": lid,
                    "soldium_service_id": sid,
                    "reason": "publish_error",
                    "code": getattr(exc, "code", None),
                    "message": str(exc),
                    "platform_key": row["platform_key"],
                }
            )
    return results, blocked


def _count_tree_services(tree: dict[str, Any]) -> int:
    n = 0

    def walk(node: dict[str, Any]) -> None:
        nonlocal n
        n += len(node.get("items") or [])
        n += len(node.get("direct_items") or [])
        for child in (node.get("sections") or {}).values():
            if isinstance(child, dict):
                walk(child)
        for child in (node.get("subsections") or {}).values():
            if isinstance(child, dict):
                walk(child)

    for root in tree.values():
        if isinstance(root, dict):
            walk(root)
    return n


def _flatten_service_ids(tree: dict[str, Any]) -> set[str]:
    ids: set[str] = set()

    def walk(node: dict[str, Any]) -> None:
        for it in list(node.get("items") or []) + list(node.get("direct_items") or []):
            if it and it.get("id") is not None:
                ids.add(str(it["id"]))
        for child in (node.get("sections") or {}).values():
            if isinstance(child, dict):
                walk(child)
        for child in (node.get("subsections") or {}).values():
            if isinstance(child, dict):
                walk(child)

    for root in tree.values():
        if isinstance(root, dict):
            walk(root)
    return ids


def build_parity_report(
    conn: sqlite3.Connection,
    *,
    legacy_tree: dict[str, Any],
) -> dict[str, Any]:
    """Compare Legacy customer tree vs Catalog customer navigation_tree via bridge."""
    legacy_to_svc, svc_to_legacy = _bridge_maps(conn)
    catalog_tree = CatalogStorefrontBackend(conn).navigation_tree()

    legacy_ids = _flatten_service_ids(legacy_tree)
    catalog_svc_ids = _flatten_service_ids(catalog_tree)

    legacy_as_svc = {legacy_to_svc[i] for i in legacy_ids if i in legacy_to_svc}
    missing_in_catalog = sorted(legacy_as_svc - catalog_svc_ids)
    extra_in_catalog = sorted(
        sid for sid in catalog_svc_ids if svc_to_legacy.get(sid) not in legacy_ids
    )
    # Catalog-only (no bridge) that appear — allowed, but noted
    catalog_only_visible = sorted(sid for sid in catalog_svc_ids if sid not in svc_to_legacy)

    # Root label check
    nodes = _node_bridge_map(conn)
    root_label_diffs: list[dict[str, Any]] = []
    for pk, want in LEGACY_CUSTOMER_PLATFORM_LABELS.items():
        bridge = nodes.get(legacy_node_key_platform(pk))
        if not bridge:
            root_label_diffs.append({"platform_key": pk, "status": "missing_node"})
            continue
        # Title as shown in Catalog tree (only if root present)
        shown = None
        if bridge["entry_id"] in catalog_tree:
            shown = str(catalog_tree[bridge["entry_id"]].get("title") or "")
        root_label_diffs.append(
            {
                "platform_key": pk,
                "desired": want,
                "catalog_title": shown,
                "match": shown == want if shown is not None else False,
                "present_in_catalog_tree": bridge["entry_id"] in catalog_tree,
            }
        )

    # Root presence
    legacy_roots = list(legacy_tree.keys())
    expected_roots_present = []
    for pk in LEGACY_PLATFORM_BUTTON_ORDER:
        bridge = nodes.get(legacy_node_key_platform(pk))
        present = bool(bridge and bridge["entry_id"] in catalog_tree)
        expected_roots_present.append(
            {"platform_key": pk, "present": present, "entry_id": (bridge or {}).get("entry_id")}
        )

    unresolved = []
    for sid in missing_in_catalog:
        lid = svc_to_legacy.get(sid)
        ready, issues = _service_is_catalog_ready(conn, sid)
        unresolved.append(
            {
                "soldium_service_id": sid,
                "legacy_catalog_id": lid,
                "ready": ready,
                "issues": issues,
            }
        )

    root_labels_ok = all(
        d.get("match") or not d.get("present_in_catalog_tree")
        for d in root_label_diffs
        if d.get("status") != "missing_node"
    )
    # For present roots, labels must match
    present_labels_ok = all(
        d.get("match") is True
        for d in root_label_diffs
        if d.get("present_in_catalog_tree")
    )

    legacy_root_keys_with_content = [
        k for k, v in legacy_tree.items() if _count_tree_services({k: v}) > 0
    ]
    roots_ok = all(
        any(r["platform_key"] == pk and r["present"] for r in expected_roots_present)
        for pk in legacy_root_keys_with_content
    )

    return {
        "legacy_roots": len(legacy_roots),
        "legacy_root_keys": legacy_roots,
        "catalog_roots": len(catalog_tree),
        "legacy_visible_services": len(legacy_ids),
        "catalog_visible_services": len(catalog_svc_ids),
        "bridged_legacy_visible": len(legacy_as_svc),
        "missing_in_catalog_count": len(missing_in_catalog),
        "missing_in_catalog": missing_in_catalog[:50],
        "extra_catalog_bridged_count": len(extra_in_catalog),
        "extra_catalog_bridged": extra_in_catalog[:20],
        "catalog_only_visible": catalog_only_visible,
        "root_label_diffs": root_label_diffs,
        "expected_roots_present": expected_roots_present,
        "roots_ok": roots_ok,
        "present_labels_ok": present_labels_ok,
        "root_labels_ok": root_labels_ok,
        "unresolved_missing": unresolved[:50],
        "unresolved_missing_total": len(unresolved),
        "customer_parity_achieved": (
            len(missing_in_catalog) == 0 and roots_ok and present_labels_ok
        ),
    }


def run_reconciliation(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = True,
    db_path: Path | None = None,
    legacy_tree_loader: Callable[[], dict[str, Any]] | None = None,
    create_backup: bool = True,
) -> ReconciliationReport:
    ts = datetime.now(timezone.utc).isoformat()
    report = ReconciliationReport(dry_run=dry_run, timestamp=ts, backup_path=None)
    report.before = capture_counts(conn)

    if create_backup and not dry_run and db_path is not None:
        backup = backup_database(Path(db_path))
        report.backup_path = str(backup)
        report.notes.append(f"backup_created:{backup}")

    report.label_updates.extend(
        reconcile_platform_labels(conn, dry_run=dry_run)
    )
    report.label_updates.extend(
        reconcile_section_labels(conn, dry_run=dry_run)
    )
    report.placement_updates = reconcile_direct_placement(conn, dry_run=dry_run)
    report.sort_updates = reconcile_ordering(
        conn, dry_run=dry_run, legacy_tree_loader=legacy_tree_loader
    )
    report.execution_repairs = repair_missing_execution_from_legacy(
        conn, dry_run=dry_run
    )
    pub_results, blocked = reconcile_publications(conn, dry_run=dry_run)
    report.publish_results = pub_results
    report.blocked = blocked

    if not dry_run:
        conn.commit()

    report.after = capture_counts(conn)

    legacy_tree: dict[str, Any] = {}
    if legacy_tree_loader is not None:
        legacy_tree = legacy_tree_loader()
    report.parity = build_parity_report(conn, legacy_tree=legacy_tree)

    report.safe_to_switch_telegram = bool(
        report.parity.get("customer_parity_achieved")
    )
    if report.blocked:
        report.notes.append(
            f"blocked_legacy_visible={len(report.blocked)} "
            "(not published — readiness/config blockers)"
        )
    if not report.safe_to_switch_telegram:
        report.notes.append(
            "SAFE_TO_SWITCH=false — leave STOREFRONT_BACKEND=legacy"
        )
    else:
        report.notes.append(
            "Catalog customer parity achieved for this DB — production switch still requires ops verification"
        )
    return report
