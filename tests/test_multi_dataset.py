import json
import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository, utcnow
from src.rules import RuleEngine
from src.service import DomainService

ADMIN = Actor("admin-1", "admin")
APPLICANT = Actor("app-1", "applicant")
COMMITTEE = Actor("cm-1", "committee")


def approve(service, application_id, expires="2099-01-01", submitter=APPLICANT):
    service.transition(submitter, application_id, "submit", {})
    service.transition(
        COMMITTEE, application_id, "review", {"committee_id": "committee-a"}
    )
    return service.transition(
        COMMITTEE,
        application_id,
        "approve",
        {
            "approvals": ["r1", "r2", "r3"],
            "terms": "noncommercial",
            "expires_at": expires,
        },
    )


class MultiDatasetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    def _dataset(self, name):
        return self.service.create(
            ADMIN, "dataset", {"name": name, "access_policy": "controlled"}
        )["id"]

    def _application(self, scope, purpose="variant analysis", applicant="APP-1"):
        return self.service.create(
            APPLICANT,
            "application",
            {
                "dataset_ids": scope,
                "applicant_id": applicant,
                "purpose": purpose,
            },
        )

    def test_application_supports_multiple_datasets(self):
        d1, d2, d3 = self._dataset("D1"), self._dataset("D2"), self._dataset("D3")
        app = self._application([d3, d1, d1, d2])
        self.assertEqual(app["data"]["dataset_ids"], sorted([d1, d2, d3]))

    def test_unknown_dataset_rejected(self):
        with self.assertRaises(ValidationError):
            self._application(["missing-dataset"])

    def test_scope_frozen_at_approval_and_never_widens_grant(self):
        d1, d2, d3 = [self._dataset(n) for n in ("D1", "D2", "D3")]
        app = self._application([d1, d2])
        approved = approve(self.service, app["id"])
        # The approval freezes the scope exactly as it stood at that moment.
        self.assertEqual(approved["data"]["approved_scope"], sorted([d1, d2]))
        self.assertEqual(approved["data"]["scope_application_version"], approved["version"] - 1)

        # The application is later edited (even after approval) to add a dataset.
        self.service.transition(
            APPLICANT,
            app["id"],
            "update_scope",
            {"dataset_ids": [d1, d2, d3]},
            expected_version=approved["version"],
        )

        # Credentials still derive from the frozen scope, not the new scope.
        grant = self.service.create(
            COMMITTEE,
            "grant",
            {"application_id": app["id"], "recipient": "researcher-1"},
        )
        self.assertEqual(grant["data"]["dataset_ids"], sorted([d1, d2]))
        self.assertNotIn(d3, grant["data"]["dataset_ids"])

    def test_grant_cannot_exceed_approved_scope(self):
        d1, d2 = self._dataset("D1"), self._dataset("D2")
        app = self._application([d1])
        approve(self.service, app["id"])
        with self.assertRaises(ValidationError):
            self.service.create(
                COMMITTEE,
                "grant",
                {
                    "application_id": app["id"],
                    "dataset_id": d2,
                    "recipient": "researcher-1",
                },
            )

    def _active_grant(self, app_id, recipient="researcher-1"):
        grant = self.service.create(
            COMMITTEE,
            "grant",
            {"application_id": app_id, "recipient": recipient},
        )
        return self.service.transition(
            COMMITTEE,
            grant["id"],
            "activate",
            {"starts_at": "2026-09-24", "expires_at": "2099-01-01"},
        )

    def test_restricted_dataset_suspends_related_grants_audit_keeps_scope(self):
        d1, d2 = self._dataset("D1"), self._dataset("D2")
        app_a = self._application([d1, d2])
        approve(self.service, app_a["id"])
        grant_a = self._active_grant(app_a["id"])

        # A second grant scoped only to another dataset must not be affected.
        d3 = self._dataset("D3")
        app_b = self._application([d3], purpose="other study", applicant="APP-2")
        approve(self.service, app_b["id"])
        grant_b = self._active_grant(app_b["id"], recipient="researcher-2")

        self.service.transition(
            ADMIN, d1, "restrict", {"reason": "consent withdrawn"}
        )

        self.assertEqual(self.service.get(grant_a["id"])["status"], "suspended")
        suspended = self.service.get(grant_a["id"])
        self.assertIn("suspended_cause", suspended["data"])
        # Original frozen scope remains readable on the credential itself.
        self.assertEqual(suspended["data"]["dataset_ids"], sorted([d1, d2]))
        self.assertEqual(self.service.get(grant_b["id"])["status"], "active")

        # Audit timeline still shows the original scope in the suspend entry.
        suspend_entries = [
            row
            for row in self.service.audit_log(grant_a["id"])
            if row["action"] == "suspend"
        ]
        self.assertEqual(len(suspend_entries), 1)
        self.assertEqual(
            suspend_entries[0]["detail"]["patch"]["scope"], sorted([d1, d2])
        )
        # Suspended credentials can still be revoked.
        revoked = self.service.transition(
            ADMIN, grant_a["id"], "revoke", {"reason": "cleanup"}
        )
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(revoked["data"]["dataset_ids"], sorted([d1, d2]))

    def test_cannot_activate_grant_for_restricted_dataset(self):
        d1 = self._dataset("D1")
        app = self._application([d1])
        approve(self.service, app["id"])
        grant = self.service.create(
            COMMITTEE,
            "grant",
            {"application_id": app["id"], "recipient": "researcher-1"},
        )
        self.service.transition(ADMIN, d1, "restrict", {"reason": "hold"})
        with self.assertRaises(ValidationError):
            self.service.transition(
                COMMITTEE,
                grant["id"],
                "activate",
                {"starts_at": "2026-09-24", "expires_at": "2099-01-01"},
            )

    def test_applicant_withdrawal_suspends_related_grant(self):
        d1 = self._dataset("D1")
        app = self._application([d1])
        approve(self.service, app["id"])
        grant = self._active_grant(app["id"])
        self.service.transition(
            APPLICANT, app["id"], "withdraw", {"reason": "study cancelled"}
        )
        self.assertEqual(self.service.get(grant["id"])["status"], "suspended")
        self.assertEqual(self.service.get(app["id"])["status"], "withdrawn")
        cause = self.service.get(grant["id"])["data"]["suspended_cause"]
        self.assertEqual(cause["kind"], "application")

    def test_concurrent_scope_edits_first_wins_later_conflicts(self):
        d1, d2, d3 = [self._dataset(n) for n in ("D1", "D2", "D3")]
        app = self._application([d1])
        approve(self.service, app["id"])
        base_version = self.service.get(app["id"])["version"]

        # Both editors loaded version base_version. First commit wins.
        first = self.service.transition(
            APPLICANT,
            app["id"],
            "update_scope",
            {"dataset_ids": [d1, d2]},
            expected_version=base_version,
        )
        # Second commit against the stale version is rejected.
        with self.assertRaises(ConflictError):
            self.service.transition(
                APPLICANT,
                app["id"],
                "update_scope",
                {"dataset_ids": [d1, d3]},
                expected_version=base_version,
            )
        # The loser re-reads, re-confirms against the new version, then succeeds.
        fresh = self.service.get(app["id"])
        self.assertEqual(fresh["data"]["dataset_ids"], sorted([d1, d2]))
        retried = self.service.transition(
            APPLICANT,
            app["id"],
            "update_scope",
            {"dataset_ids": [d1, d2, d3]},
            expected_version=first["version"],
        )
        self.assertEqual(retried["data"]["dataset_ids"], sorted([d1, d2, d3]))

    def test_duplicate_submission_returns_first_application(self):
        d1, d2 = self._dataset("D1"), self._dataset("D2")
        first = self._application([d2, d1])
        # Same applicant, same scope (any order), same purpose -> first one back.
        again = self.service.create(
            APPLICANT,
            "application",
            {"dataset_ids": [d1, d2], "applicant_id": "APP-1", "purpose": "variant analysis"},
        )
        self.assertEqual(again["id"], first["id"])

        # A different scope or purpose is a distinct application.
        other = self._application([d1], purpose="different purpose")
        self.assertNotEqual(other["id"], first["id"])

        # After the first request reaches a terminal state, resubmission works.
        self.service.transition(APPLICANT, first["id"], "submit", {})
        self.service.transition(
            APPLICANT, first["id"], "withdraw", {"reason": "changed mind"}
        )
        recreated = self._application([d1, d2])
        self.assertNotEqual(recreated["id"], first["id"])

    def test_legacy_single_dataset_records_upgrade_and_stay_actionable(self):
        d1 = self._dataset("legacy-D")
        # Simulate rows written by the old single-dataset version of the app:
        # application approved with only dataset_id, grant already active.
        with self.repo._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy-app",
                    "application",
                    "approved",
                    4,
                    json.dumps(
                        {
                            "dataset_id": d1,
                            "applicant_id": "LEGACY",
                            "purpose": "old study",
                        },
                        sort_keys=True,
                    ),
                    "legacy",
                    utcnow(),
                    utcnow(),
                ),
            )
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy-grant",
                    "grant",
                    "active",
                    2,
                    json.dumps(
                        {
                            "application_id": "legacy-app",
                            "dataset_id": d1,
                            "recipient": "legacy-researcher",
                        },
                        sort_keys=True),
                    "legacy",
                    utcnow(),
                    utcnow(),
                ),
            )

        # Reopen the repository the way a restarted service would.
        repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        service = DomainService(repo, RuleEngine())

        legacy_app = service.get("legacy-app")
        self.assertEqual(legacy_app["data"]["dataset_ids"], [d1])
        self.assertEqual(legacy_app["data"]["approved_scope"], [d1])
        legacy_grant = service.get("legacy-grant")
        self.assertEqual(legacy_grant["data"]["dataset_ids"], [d1])

        # Old credential stays viewable and revocable after the upgrade.
        revoked = service.transition(
            ADMIN, "legacy-grant", "revoke", {"reason": "legacy cleanup"}
        )
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(revoked["data"]["dataset_ids"], [d1])
        self.assertEqual(revoked["data"]["dataset_id"], d1)


if __name__ == "__main__":
    unittest.main()
