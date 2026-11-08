from __future__ import annotations

import unittest

from app.contracts import ContractError, SessionContext, migrate_legacy_v0


class LegacyMigrationTests(unittest.TestCase):
    def test_explicit_v0_payload_migrates_to_current_contract(self) -> None:
        migrated = migrate_legacy_v0(
            SessionContext,
            {
                "session_id": "session-1",
                "instance_id": "instance-1",
                "created_at": "2026-09-19T10:00:00Z",
            },
        )

        self.assertEqual(
            migrated,
            SessionContext(
                session_id="session-1",
                instance_id="instance-1",
                created_at="2026-09-19T10:00:00Z",
            ),
        )

    def test_v0_migration_rejects_unknown_fields(self) -> None:
        with self.assertRaisesRegex(ContractError, "未知字段"):
            migrate_legacy_v0(
                SessionContext,
                {
                    "session_id": "session-1",
                    "instance_id": "instance-1",
                    "created_at": "2026-09-19T10:00:00Z",
                    "legacy_extra": True,
                },
            )

    def test_v0_migration_rejects_already_versioned_payload(self) -> None:
        with self.assertRaisesRegex(ContractError, "已经包含 schema_version"):
            migrate_legacy_v0(
                SessionContext,
                {
                    "schema_version": 1,
                    "session_id": "session-1",
                    "instance_id": "instance-1",
                    "created_at": "2026-09-19T10:00:00Z",
                },
            )


if __name__ == "__main__":
    unittest.main()