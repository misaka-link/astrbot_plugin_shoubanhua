import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from usage_store import UsageStore, format_amount, yuan_to_amount


class MoneyHelperTests(unittest.TestCase):
    def test_yuan_to_amount_converts_to_milli(self):
        self.assertEqual(yuan_to_amount(1), 1000)
        self.assertEqual(yuan_to_amount(0.05), 50)
        self.assertEqual(yuan_to_amount("0.001"), 1)
        self.assertEqual(yuan_to_amount(3.0), 3000)
        self.assertEqual(yuan_to_amount(None), 0)
        self.assertEqual(yuan_to_amount("abc"), 0)

    def test_format_amount_strips_trailing_zeros(self):
        self.assertEqual(format_amount(0), "0")
        self.assertEqual(format_amount(1), "0.001")
        self.assertEqual(format_amount(50), "0.05")
        self.assertEqual(format_amount(1234), "1.234")
        self.assertEqual(format_amount(1000), "1")
        self.assertEqual(format_amount(-50), "-0.05")


class UsageStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.history_path = Path(self.temp_dir.name) / "usage_history.json"
        self.store = UsageStore(self.history_path)
        await self.store.initialize(
            {"10001": 8000, "10002": 3000},
            {"20001": 12000},
            {"date": "2026-08-14", "users": {"10001": 2}, "groups": {"20001": 2}},
        )

    async def asyncTearDown(self):
        await self.store.close()
        self.temp_dir.cleanup()

    async def test_legacy_json_import_creates_durable_file_once(self):
        users = await self.store.list_users()
        groups = await self.store.list_groups()
        overview = await self.store.get_overview()

        self.assertEqual(users["total"], 2)
        self.assertEqual(next(item for item in users["items"] if item["user_id"] == "10001")["balance"], 8000)
        self.assertEqual(groups["items"][0]["balance"], 12000)
        self.assertEqual(overview["summary"]["legacy_output_count"], 2)
        self.assertTrue(self.history_path.exists())

        await self.store.close()
        self.store = UsageStore(self.history_path)
        await self.store.initialize({"10001": 99000}, {"20001": 99000}, {})
        users_after_reopen = await self.store.list_users()
        self.assertEqual(next(item for item in users_after_reopen["items"] if item["user_id"] == "10001")["balance"], 8000)

    async def test_settlement_deducts_balance_and_records_event(self):
        result = await self.store.settle_generation(
            timestamp="2026-08-15T10:00:00",
            source="chat",
            user_id="10001",
            group_id="20001",
            logical_model="entry-model",
            actual_model="actual-model",
            api_route="generic",
            endpoint_type="images_generations",
            mode="文生图",
            outcome="success",
            http_status=200,
            output_count=1,
            charged_amount=5000,
            deduction_source="group",
        )
        self.assertEqual(result["balance_delta"], -5000)
        self.assertEqual(result["resulting_balance"], 7000)

        groups = await self.store.list_groups(start="2026-08-15T00:00:00")
        row = next(item for item in groups["items"] if item["group_id"] == "20001")
        self.assertEqual(row["balance"], 7000)
        self.assertEqual(row["outputs"], 1)
        self.assertEqual(row["charged_amount"], 5000)

        events = await self.store.list_events(model="actual-model")
        self.assertEqual(events["total"], 1)
        self.assertEqual(events["items"][0]["balance_subject_type"], "group")

    async def test_settlement_records_actual_charge_when_balance_is_lower(self):
        await self.store.adjust_balance(
            subject_type="user",
            subject_id="10002",
            amount=-2000,
            timestamp="2026-08-15T10:30:00",
            source="web_admin",
        )
        result = await self.store.settle_generation(
            timestamp="2026-08-15T10:31:00",
            source="chat",
            user_id="10002",
            group_id=None,
            logical_model="m",
            actual_model="m",
            api_route="generic",
            endpoint_type="images_generations",
            mode="文生图",
            outcome="success",
            output_count=1,
            charged_amount=9000,
            deduction_source="user",
        )
        self.assertEqual(result["balance_delta"], -1000)
        event = (await self.store.list_events(model="m"))["items"][0]
        self.assertEqual(event["charged_amount"], 1000)

    async def test_adjustment_never_creates_negative_balance(self):
        changed = await self.store.adjust_balance(
            subject_type="user",
            subject_id="10002",
            amount=-99000,
            timestamp="2026-08-15T11:00:00",
            source="web_admin",
            actor="admin",
            note="remove test quota",
        )
        self.assertEqual(changed, {"before": 3000, "after": 0, "applied_delta": -3000})

        events = await self.store.list_events(user_id="10002")
        adjustment = next(item for item in events["items"] if item["event_kind"] == "adjustment")
        self.assertEqual(adjustment["actor"], "admin")
        self.assertEqual(adjustment["resulting_balance"], 0)

    async def test_identity_snapshot_is_preserved_per_event(self):
        await self.store.snapshot_identity(
            user_id="10001",
            platform="aiocqhttp",
            nickname="旧昵称",
            avatar_url="https://q1.qlogo.cn/g?b=qq&nk=10001&s=100",
            group_id="20001",
            group_name="旧群名",
        )
        await self.store.settle_generation(
            timestamp="2026-08-15T12:30:00",
            source="chat",
            user_id="10001",
            group_id="20001",
            logical_model="m",
            actual_model="snapshot-model",
            api_route="generic",
            endpoint_type="images_generations",
            mode="文生图",
            outcome="success",
            output_count=1,
            identity_platform="aiocqhttp",
            user_nickname_snapshot="旧昵称",
            user_avatar_url_snapshot="https://q1.qlogo.cn/g?b=qq&nk=10001&s=100",
            group_name_snapshot="旧群名",
        )
        await self.store.snapshot_identity(
            user_id="10001",
            platform="aiocqhttp",
            nickname="新昵称",
            avatar_url="https://q1.qlogo.cn/g?b=qq&nk=10001&s=100",
            group_id="20001",
            group_name="新群名",
        )

        event = (await self.store.list_events(model="snapshot-model"))["items"][0]
        self.assertEqual(event["user_nickname"], "旧昵称")
        self.assertEqual(event["group_name"], "旧群名")

    async def test_free_generation_subjects_are_visible_without_balances(self):
        await self.store.settle_generation(
            timestamp="2026-08-15T13:00:00",
            source="llm_tool",
            user_id="90001",
            group_id="90002",
            logical_model="m",
            actual_model="m",
            api_route="gemini",
            endpoint_type="gemini_generate_content",
            mode="文生图",
            outcome="success",
            output_count=1,
        )

        users = await self.store.list_users(search="90001")
        groups = await self.store.list_groups(search="90002")
        self.assertEqual(users["total"], 1)
        self.assertEqual(users["items"][0]["balance"], 0)
        self.assertEqual(users["items"][0]["outputs"], 1)
        self.assertEqual(groups["total"], 1)
        self.assertEqual(groups["items"][0]["balance"], 0)
        self.assertEqual(groups["items"][0]["active_users"], 1)

    async def test_events_keep_id_tiebreaker_for_equal_timestamps(self):
        timestamp = "2026-12-31T14:00:00"
        for index in range(20):
            await self.store.adjust_balance(
                subject_type="user",
                subject_id="10001",
                amount=1,
                timestamp=timestamp,
                source="web_admin",
                note=f"event-{index}",
            )

        events = await self.store.list_events(
            user_id="10001", start="2026-12-31T00:00:00", page=1, page_size=15,
        )

        self.assertEqual(events["total"], 20)
        self.assertEqual([item["note"] for item in events["items"]], [f"event-{index}" for index in range(19, 4, -1)])

    async def test_concurrent_settlement_is_serialized_and_persisted(self):
        await self.store.adjust_balance(
            subject_type="user",
            subject_id="10003",
            amount=10000,
            timestamp="2026-08-15T12:00:00",
            source="chat_admin",
        )

        async def settle():
            return await self.store.settle_generation(
                timestamp="2026-08-15T12:01:00",
                source="chat",
                user_id="10003",
                group_id=None,
                logical_model="m",
                actual_model="m",
                api_route="gemini",
                endpoint_type="gemini_generate_content",
                mode="文生图",
                outcome="success",
                output_count=1,
                charged_amount=1000,
                deduction_source="user",
            )

        await asyncio.gather(*[settle() for _ in range(10)])
        users = await self.store.list_users(search="10003")
        self.assertEqual(users["items"][0]["balance"], 0)
        events = await self.store.list_events(user_id="10003", page_size=100)
        self.assertEqual(sum(item["output_count"] for item in events["items"]), 10)
        saved = json.loads(self.history_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["balances"]["user"]["10003"], 0)

    async def test_list_events_filters_by_outcome(self):
        for index, (outcome, status) in enumerate(
            (("success", 200), ("failed", 400), ("success", 200), ("failed", 400))
        ):
            await self.store.settle_generation(
                timestamp=f"2026-08-16T10:0{index}:00",
                source="chat",
                user_id="10001",
                group_id=None,
                logical_model="m",
                actual_model="filter-model",
                api_route="generic",
                endpoint_type="images_generations",
                mode="文生图",
                outcome=outcome,
                http_status=status,
                output_count=1 if outcome == "success" else 0,
                deduction_source=None,
            )
        await self.store.adjust_balance(
            subject_type="user",
            subject_id="10001",
            amount=1000,
            timestamp="2026-08-16T11:00:00",
            source="web_admin",
        )

        success = await self.store.list_events(model="filter-model", outcome="success")
        self.assertEqual(success["total"], 2)
        self.assertTrue(all(item["outcome"] == "success" for item in success["items"]))

        failed = await self.store.list_events(model="filter-model", outcome="failed")
        self.assertEqual(failed["total"], 2)
        self.assertTrue(all(
            item["event_kind"] == "generation" and item["outcome"] != "success"
            for item in failed["items"]
        ))

        # 全部：4 条生成事件 + 1 条调整事件（排除 setUp 建账当天的 opening_balance）
        everything = await self.store.list_events(
            user_id="10001", start="2026-08-16T00:00:00", end="2026-08-17T00:00:00",
        )
        self.assertEqual(everything["total"], 5)

    async def test_v1_ledger_is_migrated_to_amount_units(self):
        legacy = {
            "version": 1,
            "migration": {},
            "balances": {"user": {"10001": 8}, "group": {"20001": 12}},
            "user_identities": {},
            "group_identities": {},
            "ledger_events": [
                {
                    "id": 1, "occurred_at": "2026-08-14T10:00:00", "source": "chat",
                    "event_kind": "generation", "user_id": "10001", "group_id": "",
                    "actor": "", "identity_platform": "", "user_nickname_snapshot": "",
                    "user_avatar_url_snapshot": "", "group_name_snapshot": "",
                    "logical_model": "m", "actual_model": "m", "api_route": "generic",
                    "endpoint_type": "images_generations", "generation_mode": "文生图",
                    "outcome": "success", "http_status": 200, "output_count": 1,
                    "charged_units": 3, "balance_subject_type": "user",
                    "balance_subject_id": "10001", "balance_delta": -3,
                    "resulting_balance": 5, "note": "", "is_legacy": 0, "legacy_scope": "",
                }
            ],
            "next_event_id": 2,
        }
        self.history_path.write_text(json.dumps(legacy), encoding="utf-8")

        store = UsageStore(self.history_path)
        await store.initialize({}, {}, {})
        # 汇率 1 次 = 0.04 元：8 次 → 320 厘，12 次 → 480 厘
        balances = await store.export_balances()
        self.assertEqual(balances["user"]["10001"], 320)
        self.assertEqual(balances["group"]["20001"], 480)

        event = (await store.list_events())["items"][0]
        self.assertEqual(event["charged_amount"], 120)
        self.assertEqual(event["balance_delta"], -120)
        self.assertEqual(event["resulting_balance"], 200)

        saved = json.loads(self.history_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 2)
        self.assertIn("billing_v2", saved["migration"])
        self.assertNotIn("charged_units", saved["ledger_events"][0])
        await store.close()

    async def test_sqlite_import_preserves_events_and_json_balances_win(self):
        legacy_path = Path(self.temp_dir.name) / "usage_history.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.executescript("""
            CREATE TABLE balances (subject_type TEXT, subject_id TEXT, balance INTEGER);
            CREATE TABLE user_identities (user_id TEXT, platform TEXT, nickname TEXT, avatar_url TEXT, updated_at TEXT);
            CREATE TABLE group_identities (group_id TEXT, platform TEXT, name TEXT, updated_at TEXT);
            CREATE TABLE ledger_events (
                id INTEGER, occurred_at TEXT, source TEXT, event_kind TEXT,
                user_id TEXT, group_id TEXT, actor TEXT, identity_platform TEXT,
                user_nickname_snapshot TEXT, user_avatar_url_snapshot TEXT,
                group_name_snapshot TEXT, logical_model TEXT, actual_model TEXT,
                api_route TEXT, endpoint_type TEXT, generation_mode TEXT, outcome TEXT,
                http_status INTEGER, output_count INTEGER, charged_units INTEGER,
                balance_subject_type TEXT, balance_subject_id TEXT, balance_delta INTEGER,
                resulting_balance INTEGER, note TEXT, is_legacy INTEGER, legacy_scope TEXT
            );
        """)
        connection.execute("INSERT INTO balances VALUES ('user', 'same', 2)")
        connection.execute("INSERT INTO balances VALUES ('group', 'legacy-group', 6)")
        connection.execute("INSERT INTO user_identities VALUES ('same', 'qq', '旧用户', '', '2026-08-01T00:00:00')")
        connection.execute("INSERT INTO ledger_events VALUES (42, '2026-08-01T00:00:00', 'chat', 'generation', 'same', 'legacy-group', '', 'qq', '旧用户', '', '旧群', 'source', 'legacy-model', 'generic', 'images_generations', '文生图', 'success', 200, 1, 1, 'user', 'same', -1, 2, 'legacy event', 0, '')")
        connection.commit()
        connection.close()

        imported_path = Path(self.temp_dir.name) / "imported.json"
        imported = UsageStore(imported_path, legacy_path)
        await imported.initialize({"same": 9000, "json-only": 4000}, {}, {})

        balances = await imported.export_balances()
        self.assertEqual(balances["user"], {"same": 9000, "json-only": 4000})
        self.assertEqual(balances["group"]["legacy-group"], 240)
        events = await imported.list_events(model="legacy-model")
        self.assertEqual(events["items"][0]["id"], 42)
        self.assertEqual(events["items"][0]["user_nickname"], "旧用户")
        self.assertEqual(events["items"][0]["charged_amount"], 40)
        self.assertTrue(legacy_path.exists())
        saved = json.loads(imported_path.read_text(encoding="utf-8"))
        self.assertTrue(saved["migration"]["legacy_sqlite_imported"])


if __name__ == "__main__":
    unittest.main()
