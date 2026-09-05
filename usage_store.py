"""File-backed money balances and privacy-conscious generation usage history."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# 内部金额单位：1 厘 = 0.001 元。所有余额与账本金额均以「厘」为整数存储，避免浮点误差。
MONEY_SCALE = 1000
# 旧版「按次计费」数据迁移到「按金额计费」的汇率：1 次 = LEGACY_COUNT_TO_YUAN 元。
# 线上部署定为 0.04 元/次（2026-09 按实际数据确认）。
LEGACY_COUNT_TO_YUAN = 0.04


def yuan_to_amount(value: Any) -> int:
    """元（int/float/str）→ 厘（int），四舍五入到 0.001 元；无效输入返回 0。"""
    try:
        return int(round(float(value) * MONEY_SCALE))
    except (TypeError, ValueError):
        return 0


def amount_to_yuan(amount: Any) -> float:
    """厘（int）→ 元（float），保留 3 位小数。"""
    try:
        return round(int(amount) / MONEY_SCALE, 3)
    except (TypeError, ValueError):
        return 0.0


def format_amount(amount: Any) -> str:
    """厘（int）→ 金额文本（去尾零）：50 → "0.05"，1234 → "1.234"，0 → "0"。"""
    text = f"{amount_to_yuan(amount):.3f}".rstrip("0").rstrip(".")
    return text or "0"


class UsageStore:
    """Async facade around a plugin-local JSON ledger.

    Prompts, images, provider payloads, headers, and API credentials are never
    persisted. SQLite is only opened read-only once to import an older ledger.
    Balances and ledger money fields are integer 「厘」 (0.001 yuan).
    """

    FILE_VERSION = 2

    def __init__(self, history_path: Path, legacy_database_path: Path | None = None):
        self.history_path = Path(history_path)
        self.legacy_database_path = Path(legacy_database_path) if legacy_database_path else None
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] | None = None

    async def initialize(
        self,
        user_balances: dict[str, Any] | None = None,
        group_balances: dict[str, Any] | None = None,
        daily_stats: dict[str, Any] | None = None,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._initialize_sync,
                user_balances or {},
                group_balances or {},
                daily_stats or {},
            )

    async def close(self) -> None:
        # Every mutation is persisted atomically, so no long-lived resource needs closing.
        return None

    async def export_balances(self) -> dict[str, dict[str, int]]:
        async with self._lock:
            data = self._require_data()
            return {
                "user": copy.deepcopy(data["balances"]["user"]),
                "group": copy.deepcopy(data["balances"]["group"]),
            }

    async def merge_balance_sources(
        self,
        user_balances: dict[str, Any],
        group_balances: dict[str, Any],
    ) -> dict[str, dict[str, int]]:
        """Merge legacy balance sources (already in 厘) as the authoritative current balances."""
        async with self._lock:
            data = self._require_data()
            previous = copy.deepcopy(data)
            self._merge_balance_source(data, "user", user_balances, override=True)
            self._merge_balance_source(data, "group", group_balances, override=True)
            try:
                await asyncio.to_thread(self._persist_sync)
            except Exception:
                self._data = previous
                raise
            return {
                "user": copy.deepcopy(data["balances"]["user"]),
                "group": copy.deepcopy(data["balances"]["group"]),
            }

    def _initialize_sync(
        self,
        user_balances: dict[str, Any],
        group_balances: dict[str, Any],
        daily_stats: dict[str, Any],
    ) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        if self.history_path.exists():
            self._data = self._load_file_sync()
            if self._migrate_billing_v2_sync(self._data):
                self._ensure_shape(self._data)
                self._persist_sync()
                return
            self._ensure_shape(self._data)
            return

        imported_from_sqlite = False
        data = self._empty_data()
        if self.legacy_database_path and self.legacy_database_path.exists():
            data = self._import_sqlite_sync(self.legacy_database_path)
            imported_from_sqlite = True

        self._merge_balance_source(data, "user", user_balances, override=True)
        self._merge_balance_source(data, "group", group_balances, override=True)
        if imported_from_sqlite:
            data["migration"] = {
                "legacy_sqlite_imported": True,
                "source": self.legacy_database_path.name if self.legacy_database_path else "",
                "completed_at": self._now(),
            }
        else:
            self._import_legacy_json(data, user_balances, group_balances, daily_stats)
            data["migration"] = {"legacy_json_imported": True, "completed_at": self._now()}

        self._ensure_shape(data)
        self._data = data
        self._persist_sync()

    def _empty_data(self) -> dict[str, Any]:
        return {
            "version": self.FILE_VERSION,
            "migration": {},
            "balances": {"user": {}, "group": {}},
            "user_identities": {},
            "group_identities": {},
            "ledger_events": [],
            "next_event_id": 1,
        }

    def _migrate_billing_v2_sync(self, data: dict[str, Any]) -> bool:
        """v1（按次计费）→ v2（按金额计费）：余额与账本金额字段换算成「厘」。

        换算公式：厘 = 次 × LEGACY_COUNT_TO_YUAN × MONEY_SCALE。
        事件字段 charged_units 同时重命名为 charged_amount。返回是否发生迁移。
        """
        if not isinstance(data, dict):
            return False
        try:
            version = int(data.get("version") or 1)
        except (TypeError, ValueError):
            version = 1
        if version >= self.FILE_VERSION:
            return False
        factor = LEGACY_COUNT_TO_YUAN * MONEY_SCALE

        def scale(value: Any) -> int:
            return int(round(self._nonnegative_int(value) * factor))

        balances = data.get("balances")
        if isinstance(balances, dict):
            for scope in ("user", "group"):
                values = balances.get(scope)
                if isinstance(values, dict):
                    balances[scope] = {
                        subject_id: scale(balance)
                        for subject_id, balance in values.items()
                        if str(subject_id or "").strip()
                    }
        events = data.get("ledger_events")
        if isinstance(events, list):
            for event in events:
                if not isinstance(event, dict):
                    continue
                if "charged_units" in event:
                    event["charged_amount"] = scale(event.pop("charged_units"))
                elif "charged_amount" in event:
                    event["charged_amount"] = scale(event["charged_amount"])
                if "balance_delta" in event:
                    event["balance_delta"] = int(round(self._int(event["balance_delta"]) * factor))
                if event.get("resulting_balance") is not None:
                    event["resulting_balance"] = scale(event["resulting_balance"])
        migration = data.get("migration")
        if not isinstance(migration, dict):
            migration = {}
            data["migration"] = migration
        migration["billing_v2"] = {
            "legacy_count_to_yuan": LEGACY_COUNT_TO_YUAN,
            "completed_at": self._now(),
        }
        data["version"] = self.FILE_VERSION
        return True

    def _load_file_sync(self) -> dict[str, Any]:
        try:
            value = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取 JSON 用量账本: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("JSON 用量账本根节点必须是对象")
        return value

    def _ensure_shape(self, data: dict[str, Any]) -> None:
        data["version"] = self.FILE_VERSION
        if not isinstance(data.get("migration"), dict):
            data["migration"] = {}
        balances = data.get("balances")
        if not isinstance(balances, dict):
            balances = {}
        data["balances"] = {
            scope: self._normalize_balances(balances.get(scope, {}))
            for scope in ("user", "group")
        }
        data["user_identities"] = self._normalize_identity_map(data.get("user_identities"), "user")
        data["group_identities"] = self._normalize_identity_map(data.get("group_identities"), "group")
        raw_events = data.get("ledger_events")
        events = raw_events if isinstance(raw_events, list) else []
        data["ledger_events"] = [
            self._normalize_event(event, index + 1)
            for index, event in enumerate(events)
            if isinstance(event, dict)
        ]
        highest_id = max((event["id"] for event in data["ledger_events"]), default=0)
        try:
            next_event_id = int(data.get("next_event_id", highest_id + 1))
        except (TypeError, ValueError):
            next_event_id = highest_id + 1
        data["next_event_id"] = max(highest_id + 1, next_event_id, 1)

    @staticmethod
    def _normalize_balances(values: Any) -> dict[str, int]:
        if not isinstance(values, dict):
            return {}
        result: dict[str, int] = {}
        for raw_id, raw_balance in values.items():
            subject_id = str(raw_id or "").strip()
            if subject_id:
                result[subject_id] = UsageStore._nonnegative_int(raw_balance)
        return result

    def _normalize_identity_map(self, values: Any, subject_type: str) -> dict[str, dict[str, str]]:
        if not isinstance(values, dict):
            return {}
        result: dict[str, dict[str, str]] = {}
        for raw_id, raw_identity in values.items():
            subject_id = str(raw_id or "").strip()
            if not subject_id or not isinstance(raw_identity, dict):
                continue
            if subject_type == "user":
                result[subject_id] = {
                    "platform": str(raw_identity.get("platform") or ""),
                    "nickname": str(raw_identity.get("nickname") or ""),
                    "avatar_url": str(raw_identity.get("avatar_url") or ""),
                    "updated_at": str(raw_identity.get("updated_at") or ""),
                }
            else:
                result[subject_id] = {
                    "platform": str(raw_identity.get("platform") or ""),
                    "name": str(raw_identity.get("name") or ""),
                    "updated_at": str(raw_identity.get("updated_at") or ""),
                }
        return result

    def _normalize_event(self, raw_event: dict[str, Any], fallback_id: int) -> dict[str, Any]:
        try:
            event_id = max(1, int(raw_event.get("id", fallback_id)))
        except (TypeError, ValueError):
            event_id = fallback_id
        event = {
            "id": event_id,
            "occurred_at": str(raw_event.get("occurred_at") or ""),
            "source": str(raw_event.get("source") or ""),
            "event_kind": str(raw_event.get("event_kind") or "generation"),
            "user_id": str(raw_event.get("user_id") or ""),
            "group_id": str(raw_event.get("group_id") or ""),
            "actor": str(raw_event.get("actor") or ""),
            "identity_platform": str(raw_event.get("identity_platform") or ""),
            "user_nickname_snapshot": str(raw_event.get("user_nickname_snapshot") or ""),
            "user_avatar_url_snapshot": str(raw_event.get("user_avatar_url_snapshot") or ""),
            "group_name_snapshot": str(raw_event.get("group_name_snapshot") or ""),
            "logical_model": str(raw_event.get("logical_model") or ""),
            "actual_model": str(raw_event.get("actual_model") or ""),
            "api_route": str(raw_event.get("api_route") or ""),
            "endpoint_type": str(raw_event.get("endpoint_type") or ""),
            "generation_mode": str(raw_event.get("generation_mode") or ""),
            "outcome": str(raw_event.get("outcome") or ""),
            "http_status": self._int(raw_event.get("http_status")),
            "output_count": self._nonnegative_int(raw_event.get("output_count")),
            "charged_amount": self._nonnegative_int(
                raw_event.get("charged_amount", raw_event.get("charged_units"))
            ),
            "balance_subject_type": str(raw_event.get("balance_subject_type") or ""),
            "balance_subject_id": str(raw_event.get("balance_subject_id") or ""),
            "balance_delta": self._int(raw_event.get("balance_delta")),
            "resulting_balance": self._optional_int(raw_event.get("resulting_balance")),
            "note": self._safe_note(raw_event.get("note")),
            "is_legacy": 1 if self._truthy(raw_event.get("is_legacy")) else 0,
            "legacy_scope": str(raw_event.get("legacy_scope") or ""),
        }
        return event

    def _import_sqlite_sync(self, database_path: Path) -> dict[str, Any]:
        data = self._empty_data()
        # SQLite 是旧版「按次计费」账本，先按 v1 标记再统一迁移成金额（厘）。
        data["version"] = 1
        try:
            connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            raise RuntimeError(f"无法打开旧 SQLite 用量账本: {exc}") from exc
        try:
            tables = {
                row["name"]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if "balances" in tables:
                for row in connection.execute("SELECT subject_type, subject_id, balance FROM balances"):
                    scope = str(row["subject_type"] or "")
                    subject_id = str(row["subject_id"] or "").strip()
                    if scope in {"user", "group"} and subject_id:
                        data["balances"][scope][subject_id] = self._nonnegative_int(row["balance"])
            if "user_identities" in tables:
                for row in connection.execute("SELECT user_id, platform, nickname, avatar_url, updated_at FROM user_identities"):
                    subject_id = str(row["user_id"] or "").strip()
                    if subject_id:
                        data["user_identities"][subject_id] = {
                            "platform": str(row["platform"] or ""),
                            "nickname": str(row["nickname"] or ""),
                            "avatar_url": str(row["avatar_url"] or ""),
                            "updated_at": str(row["updated_at"] or ""),
                        }
            if "group_identities" in tables:
                for row in connection.execute("SELECT group_id, platform, name, updated_at FROM group_identities"):
                    subject_id = str(row["group_id"] or "").strip()
                    if subject_id:
                        data["group_identities"][subject_id] = {
                            "platform": str(row["platform"] or ""),
                            "name": str(row["name"] or ""),
                            "updated_at": str(row["updated_at"] or ""),
                        }
            if "ledger_events" in tables:
                for row in connection.execute("SELECT * FROM ledger_events ORDER BY id ASC"):
                    data["ledger_events"].append(self._normalize_event(dict(row), len(data["ledger_events"]) + 1))
        except sqlite3.Error as exc:
            raise RuntimeError(f"读取旧 SQLite 用量账本失败: {exc}") from exc
        finally:
            connection.close()
        self._migrate_billing_v2_sync(data)
        self._ensure_shape(data)
        return data

    def _merge_balance_source(self, data: dict[str, Any], subject_type: str, values: dict[str, Any], *, override: bool) -> None:
        target = data["balances"][subject_type]
        for subject_id, balance in self._normalize_balances(values).items():
            if override or subject_id not in target:
                target[subject_id] = balance

    def _import_legacy_json(
        self,
        data: dict[str, Any],
        user_balances: dict[str, Any],
        group_balances: dict[str, Any],
        daily_stats: dict[str, Any],
    ) -> None:
        timestamp = self._now()
        for subject_type, balances in (("user", user_balances), ("group", group_balances)):
            for subject_id, balance in self._normalize_balances(balances).items():
                self._append_event(data, {
                    "occurred_at": timestamp,
                    "source": "legacy_import",
                    "event_kind": "opening_balance",
                    "user_id": subject_id if subject_type == "user" else "",
                    "group_id": subject_id if subject_type == "group" else "",
                    "outcome": "imported",
                    "balance_subject_type": subject_type,
                    "balance_subject_id": subject_id,
                    "balance_delta": balance,
                    "resulting_balance": balance,
                    "note": "Imported from legacy JSON balance file.",
                    "is_legacy": 1,
                })
        date = str(daily_stats.get("date") or "").strip() if isinstance(daily_stats, dict) else ""
        event_time = f"{date}T00:00:00" if date else timestamp
        for scope, values in (("user", daily_stats.get("users", {})), ("group", daily_stats.get("groups", {}))):
            if not isinstance(values, dict):
                continue
            for raw_id, raw_count in values.items():
                subject_id = str(raw_id or "").strip()
                count = self._nonnegative_int(raw_count)
                if subject_id and count:
                    self._append_event(data, {
                        "occurred_at": event_time,
                        "source": "legacy_import",
                        "event_kind": "generation",
                        "user_id": subject_id if scope == "user" else "",
                        "group_id": subject_id if scope == "group" else "",
                        "outcome": "success",
                        "output_count": count,
                        "note": "Imported aggregate from legacy daily_stats.json; not request-level data.",
                        "is_legacy": 1,
                        "legacy_scope": scope,
                    })

    def _persist_sync(self) -> None:
        data = self._require_data()
        temporary_path = self.history_path.with_suffix(f"{self.history_path.suffix}.tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            with temporary_path.open("w", encoding="utf-8") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, self.history_path)
        except OSError as exc:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(f"写入 JSON 用量账本失败: {exc}") from exc

    async def snapshot_identity(
        self,
        *,
        user_id: str,
        platform: str = "",
        nickname: str = "",
        avatar_url: str = "",
        group_id: str = "",
        group_name: str = "",
    ) -> None:
        if not user_id and not group_id:
            return
        async with self._lock:
            data = self._require_data()
            previous = copy.deepcopy(data)
            timestamp = self._now()
            if user_id:
                existing = data["user_identities"].get(user_id, {})
                data["user_identities"][user_id] = {
                    "platform": str(platform or "") or existing.get("platform", ""),
                    "nickname": str(nickname or "") or existing.get("nickname", ""),
                    "avatar_url": str(avatar_url or "") or existing.get("avatar_url", ""),
                    "updated_at": timestamp,
                }
            if group_id:
                existing = data["group_identities"].get(group_id, {})
                data["group_identities"][group_id] = {
                    "platform": str(platform or "") or existing.get("platform", ""),
                    "name": str(group_name or "") or existing.get("name", ""),
                    "updated_at": timestamp,
                }
            try:
                await asyncio.to_thread(self._persist_sync)
            except Exception:
                self._data = previous
                raise

    async def settle_generation(
        self,
        *,
        timestamp: str,
        source: str,
        user_id: str,
        group_id: str | None,
        logical_model: str,
        actual_model: str,
        api_route: str,
        endpoint_type: str,
        mode: str,
        outcome: str,
        http_status: int = 0,
        output_count: int = 0,
        charged_amount: int = 0,
        deduction_source: str | None = None,
        note: str = "",
        identity_platform: str = "",
        user_nickname_snapshot: str = "",
        user_avatar_url_snapshot: str = "",
        group_name_snapshot: str = "",
    ) -> dict[str, Any]:
        async with self._lock:
            data = self._require_data()
            previous = copy.deepcopy(data)
            subject_type = deduction_source if deduction_source in {"user", "group"} else ""
            subject_id = user_id if subject_type == "user" else (group_id or "") if subject_type == "group" else ""
            requested_charge = self._nonnegative_int(charged_amount) if subject_id else 0
            actual_charge = 0
            balance_delta = 0
            resulting_balance: int | None = None
            if subject_type and subject_id and requested_charge:
                balance = self._get_balance(data, subject_type, subject_id)
                actual_charge = min(requested_charge, balance)
                resulting_balance = balance - actual_charge
                balance_delta = -actual_charge
                data["balances"][subject_type][subject_id] = resulting_balance
            self._append_event(data, {
                "occurred_at": timestamp or self._now(),
                "source": source,
                "event_kind": "generation",
                "user_id": user_id,
                "group_id": group_id or "",
                "identity_platform": identity_platform,
                "user_nickname_snapshot": user_nickname_snapshot,
                "user_avatar_url_snapshot": user_avatar_url_snapshot,
                "group_name_snapshot": group_name_snapshot,
                "logical_model": logical_model,
                "actual_model": actual_model,
                "api_route": api_route,
                "endpoint_type": endpoint_type,
                "generation_mode": mode,
                "outcome": outcome,
                "http_status": http_status,
                "output_count": output_count,
                "charged_amount": actual_charge,
                "balance_subject_type": subject_type,
                "balance_subject_id": subject_id,
                "balance_delta": balance_delta,
                "resulting_balance": resulting_balance,
                "note": note,
            })
            try:
                await asyncio.to_thread(self._persist_sync)
            except Exception:
                self._data = previous
                raise
            return {
                "balance_subject_type": subject_type,
                "balance_subject_id": subject_id,
                "balance_delta": balance_delta,
                "resulting_balance": resulting_balance,
            }

    async def record_generation_attempt(self, **kwargs: Any) -> None:
        await self.settle_generation(**kwargs)

    async def adjust_balance(
        self,
        *,
        subject_type: str,
        subject_id: str,
        amount: int,
        timestamp: str,
        source: str,
        actor: str = "",
        note: str = "",
        user_id: str = "",
        group_id: str = "",
        identity_platform: str = "",
        user_nickname_snapshot: str = "",
        user_avatar_url_snapshot: str = "",
        group_name_snapshot: str = "",
    ) -> dict[str, int]:
        if subject_type not in {"user", "group"}:
            raise ValueError("subject_type must be 'user' or 'group'")
        subject_id = str(subject_id or "").strip()
        if not subject_id:
            raise ValueError("subject_id cannot be empty")
        async with self._lock:
            data = self._require_data()
            previous = copy.deepcopy(data)
            before = self._get_balance(data, subject_type, subject_id)
            after = max(0, before + int(amount))
            applied_delta = after - before
            data["balances"][subject_type][subject_id] = after
            self._append_event(data, {
                "occurred_at": timestamp or self._now(),
                "source": source,
                "event_kind": "adjustment",
                "user_id": user_id or (subject_id if subject_type == "user" else ""),
                "group_id": group_id or (subject_id if subject_type == "group" else ""),
                "actor": actor,
                "identity_platform": identity_platform,
                "user_nickname_snapshot": user_nickname_snapshot,
                "user_avatar_url_snapshot": user_avatar_url_snapshot,
                "group_name_snapshot": group_name_snapshot,
                "outcome": "applied",
                "balance_subject_type": subject_type,
                "balance_subject_id": subject_id,
                "balance_delta": applied_delta,
                "resulting_balance": after,
                "note": note,
            })
            try:
                await asyncio.to_thread(self._persist_sync)
            except Exception:
                self._data = previous
                raise
            return {"before": before, "after": after, "applied_delta": applied_delta}

    async def get_overview(
        self, start: str | None = None, end: str | None = None, granularity: str = "day"
    ) -> dict[str, Any]:
        async with self._lock:
            events = self._filtered_events(self._require_data(), start, end)
            summary = {
                "successful_outputs": sum(event["output_count"] for event in events if event["event_kind"] == "generation" and event["outcome"] == "success" and not event["is_legacy"]),
                "failed_charged_amount": sum(event["charged_amount"] for event in events if event["event_kind"] == "generation" and event["outcome"] != "success" and event["charged_amount"] > 0 and not event["is_legacy"]),
                "charged_amount": sum(event["charged_amount"] for event in events if event["event_kind"] == "generation" and not event["is_legacy"]),
                "unbilled_llm_outputs": sum(event["output_count"] for event in events if event["event_kind"] == "generation" and event["source"] == "llm_tool" and event["outcome"] == "success" and event["charged_amount"] == 0 and not event["is_legacy"]),
            }
            trend_by_date: dict[str, dict[str, Any]] = {}
            model_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
            for event in events:
                if event["is_legacy"] or event["event_kind"] != "generation":
                    continue
                # granularity=hour 时按小时分桶（短范围趋势），默认按天
                date = event["occurred_at"][:13] if granularity == "hour" else event["occurred_at"][:10]
                trend = trend_by_date.setdefault(date, {"date": date, "outputs": 0, "charged_amount": 0})
                if event["outcome"] == "success":
                    trend["outputs"] += event["output_count"]
                trend["charged_amount"] += event["charged_amount"]
                key = (event["actual_model"], event["api_route"], event["endpoint_type"])
                model = model_rows.setdefault(key, {
                    "actual_model": key[0], "api_route": key[1], "endpoint_type": key[2],
                    "outputs": 0, "charged_amount": 0, "attempts": 0,
                })
                if event["outcome"] == "success":
                    model["outputs"] += event["output_count"]
                model["charged_amount"] += event["charged_amount"]
                model["attempts"] += 1
            models = sorted(model_rows.values(), key=lambda item: (-item["outputs"], -item["charged_amount"], -item["attempts"], item["actual_model"]))[:20]
            return {"summary": summary, "trend": [trend_by_date[key] for key in sorted(trend_by_date)], "models": models}

    async def list_users(self, *, start: str | None = None, end: str | None = None, search: str = "", page: int = 1, page_size: int = 30) -> dict[str, Any]:
        async with self._lock:
            return self._list_subjects(self._require_data(), "user", start, end, search, page, page_size)

    async def list_groups(self, *, start: str | None = None, end: str | None = None, search: str = "", page: int = 1, page_size: int = 30) -> dict[str, Any]:
        async with self._lock:
            return self._list_subjects(self._require_data(), "group", start, end, search, page, page_size)

    def _list_subjects(self, data: dict[str, Any], subject_type: str, start: str | None, end: str | None, search: str, page: int, page_size: int) -> dict[str, Any]:
        events = self._filtered_events(data, start, end)
        key = "user_id" if subject_type == "user" else "group_id"
        identity_key = "user_identities" if subject_type == "user" else "group_identities"
        subject_ids = set(data["balances"][subject_type])
        subject_ids.update(event[key] for event in events if event[key])
        search = str(search or "").strip().lower()
        rows = []
        for subject_id in subject_ids:
            identity = data[identity_key].get(subject_id, {})
            name = identity.get("nickname", "") if subject_type == "user" else identity.get("name", "")
            if search and search not in subject_id.lower() and search not in name.lower():
                continue
            relevant = [event for event in events if event[key] == subject_id]
            output_count = sum(event["output_count"] for event in relevant if event["event_kind"] == "generation" and event["outcome"] == "success")
            charged_amount = sum(event["charged_amount"] for event in relevant if event["event_kind"] == "generation")
            if subject_type == "user":
                row = {
                    "user_id": subject_id,
                    "balance": self._get_balance(data, "user", subject_id),
                    "platform": identity.get("platform", ""),
                    "nickname": name,
                    "avatar_url": identity.get("avatar_url", ""),
                    "outputs": output_count,
                    "charged_amount": charged_amount,
                }
            else:
                row = {
                    "group_id": subject_id,
                    "balance": self._get_balance(data, "group", subject_id),
                    "platform": identity.get("platform", ""),
                    "name": name,
                    "outputs": output_count,
                    "charged_amount": charged_amount,
                    "active_users": len({event["user_id"] for event in relevant if event["user_id"]}),
                }
            rows.append(row)
        id_key = "user_id" if subject_type == "user" else "group_id"
        rows.sort(key=lambda row: (-row["charged_amount"], -row["outputs"], row[id_key]))
        return self._page_result(rows, page, page_size)

    async def list_events(self, *, start: str | None = None, end: str | None = None, user_id: str = "", group_id: str = "", model: str = "", outcome: str = "", page: int = 1, page_size: int = 30) -> dict[str, Any]:
        async with self._lock:
            data = self._require_data()
            events = self._filtered_events(data, start, end)
            user_id, group_id, model = str(user_id or ""), str(group_id or ""), str(model or "")
            outcome = str(outcome or "").strip().lower()
            if outcome == "success":
                events = [event for event in events if event["outcome"] == "success"]
            elif outcome == "failed":
                events = [
                    event for event in events
                    if event["event_kind"] == "generation" and event["outcome"] != "success"
                ]
            events = [
                event for event in events
                if (not user_id or event["user_id"] == user_id)
                and (not group_id or event["group_id"] == group_id)
                and (not model or event["actual_model"] == model)
            ]
            rows = []
            for event in events:
                row = copy.deepcopy(event)
                user_identity = data["user_identities"].get(row["user_id"], {})
                group_identity = data["group_identities"].get(row["group_id"], {})
                row["user_nickname"] = row["user_nickname_snapshot"] or user_identity.get("nickname", "")
                row["user_avatar_url"] = row["user_avatar_url_snapshot"] or user_identity.get("avatar_url", "")
                row["group_name"] = row["group_name_snapshot"] or group_identity.get("name", "")
                rows.append(row)
            rows.sort(key=lambda event: (event["occurred_at"], event["id"]), reverse=True)
            return self._page_result(rows, page, page_size)

    def _filtered_events(self, data: dict[str, Any], start: str | None, end: str | None) -> list[dict[str, Any]]:
        return [
            event for event in data["ledger_events"]
            if (not start or event["occurred_at"] >= str(start))
            and (not end or event["occurred_at"] < str(end))
        ]

    def _append_event(self, data: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        event = self._normalize_event({"id": data["next_event_id"], **event}, data["next_event_id"])
        data["ledger_events"].append(event)
        data["next_event_id"] = event["id"] + 1
        return event

    @staticmethod
    def _page_result(rows: Iterable[dict[str, Any]], page: int, page_size: int) -> dict[str, Any]:
        page, page_size = UsageStore._page(page, page_size)
        values = list(rows)
        return {
            "items": values[(page - 1) * page_size:page * page_size],
            "total": len(values),
            "page": page,
            "page_size": page_size,
        }

    @staticmethod
    def _page(page: int, page_size: int) -> tuple[int, int]:
        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = min(100, max(1, int(page_size)))
        except (TypeError, ValueError):
            page_size = 30
        return page, page_size

    @staticmethod
    def _nonnegative_int(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _safe_note(note: Any) -> str:
        return str(note or "").strip()[:500]

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _get_balance(data: dict[str, Any], subject_type: str, subject_id: str) -> int:
        return int(data["balances"][subject_type].get(subject_id, 0))

    def _require_data(self) -> dict[str, Any]:
        if self._data is None:
            raise RuntimeError("UsageStore has not been initialized")
        return self._data
