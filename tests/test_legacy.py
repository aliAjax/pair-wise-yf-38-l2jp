import json
import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


ADMIN = Actor("admin", "admin")
COMMITTEE = Actor("committee-1", "committee")
APPLICANT = Actor("app-1", "applicant")


class LegacyCompatibilityTest(unittest.TestCase):
    """Applications and grants created before the multi-dataset upgrade.

    Old rows only carry a single ``dataset_id`` and have no ``scope`` snapshot.
    They must remain viewable and revocable after the upgrade.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.repo = SQLiteRepository(self.db_path)
        self.service = DomainService(self.repo, RuleEngine())
        self.dataset = self.service.create(
            ADMIN,
            "dataset",
            {"name": "Legacy Cohort", "access_policy": "controlled"},
        )["id"]
        self._insert_legacy_application()
        self._insert_legacy_grant()

    def tearDown(self):
        self.tmp.cleanup()

    def _insert_legacy_application(self):
        with self.repo._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, 'application', 'approved', 4, ?, 'app-1', '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00')",
                (
                    "legacy-app",
                    json.dumps(
                        {
                            "dataset_id": self.dataset,
                            "applicant_id": "app-1",
                            "purpose": "legacy study",
                            "terms": "noncommercial",
                            "expires_at": "2099-01-01",
                        },
                        sort_keys=True,
                    ),
                ),
            )

    def _insert_legacy_grant(self):
        with self.repo._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, 'grant', 'active', 2, ?, 'committee-1', '2026-01-03T00:00:00+00:00', '2026-01-03T00:00:00+00:00')",
                (
                    "legacy-grant",
                    json.dumps(
                        {
                            "application_id": "legacy-app",
                            "dataset_id": self.dataset,
                            "recipient": "researcher-1",
                        },
                        sort_keys=True,
                    ),
                ),
            )

    def test_legacy_application_is_viewable_with_canonical_scope(self):
        app = self.service.get("legacy-app")
        self.assertEqual(app["data"]["dataset_ids"], [self.dataset])
        self.assertEqual(app["data"]["dataset_id"], self.dataset)

    def test_legacy_grant_is_viewable_with_derived_scope(self):
        grant = self.service.get("legacy-grant")
        self.assertEqual(grant["status"], "active")
        self.assertEqual(grant["data"]["dataset_ids"], [self.dataset])
        self.assertEqual(grant["data"]["scope"]["dataset_ids"], [self.dataset])

    def test_legacy_grant_can_be_revoked(self):
        revoked = self.service.transition(
            COMMITTEE,
            "legacy-grant",
            "revoke",
            {"reason": "legacy cleanup"},
        )
        self.assertEqual(revoked["status"], "revoked")
        # The stored scope fields are untouched by presentation; revocation
        # persists the original legacy payload.
        self.assertEqual(revoked["data"]["dataset_ids"], [self.dataset])

    def test_legacy_grant_is_suspended_when_dataset_restricted(self):
        self.service.transition(
            ADMIN, self.dataset, "restrict", {"reason": "old policy"}
        )
        grant = self.service.get("legacy-grant")
        self.assertEqual(grant["status"], "suspended")
        self.assertEqual(grant["data"]["scope"]["dataset_ids"], [self.dataset])

    def test_new_multi_dataset_application_sits_alongside_legacy(self):
        app = self.service.create(
            APPLICANT,
            "application",
            {
                "applicant_id": "app-2",
                "dataset_ids": [self.dataset],
                "purpose": "new study",
            },
        )
        self.assertEqual(app["data"]["dataset_ids"], [self.dataset])
        self.assertEqual(len(self.service.list(kind="application")), 2)


if __name__ == "__main__":
    unittest.main()
