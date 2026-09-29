import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


ADMIN = Actor("admin", "admin")
APPLICANT = Actor("app-1", "applicant")
COMMITTEE = Actor("committee-1", "committee")


class MultiDatasetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.ds1 = self.service.create(
            ADMIN, "dataset", {"name": "D1", "access_policy": "controlled"}
        )["id"]
        self.ds2 = self.service.create(
            ADMIN, "dataset", {"name": "D2", "access_policy": "controlled"}
        )["id"]
        self.ds3 = self.service.create(
            ADMIN, "dataset", {"name": "D3", "access_policy": "controlled"}
        )["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _application(self, ids, purpose="variant analysis", actor=APPLICANT):
        return self.service.create(
            actor,
            "application",
            {
                "applicant_id": "app-1",
                "dataset_ids": ids,
                "purpose": purpose,
            },
        )

    def _approve(self, application_id, expected_version=3):
        # draft -> submitted -> under_review -> approved
        self.service.transition(APPLICANT, application_id, "submit", {})
        self.service.transition(
            COMMITTEE,
            application_id,
            "review",
            {"committee_id": "committee-a"},
        )
        return self.service.transition(
            COMMITTEE,
            application_id,
            "approve",
            {
                "approvals": ["r1", "r2", "r3"],
                "terms": "noncommercial",
                "expires_at": "2099-01-01",
            },
            expected_version=expected_version,
        )

    def _grant(self, application_id):
        return self.service.create(
            COMMITTEE,
            "grant",
            {"application_id": application_id, "recipient": "researcher-1"},
        )

    def test_application_binds_multiple_datasets(self):
        app = self._application([self.ds1, self.ds2])
        self.assertEqual(app["data"]["dataset_ids"], sorted([self.ds1, self.ds2]))
        # Legacy single-dataset field is kept populated for old consumers.
        self.assertIn(app["data"]["dataset_id"], (self.ds1, self.ds2))

    def test_unknown_dataset_rejected(self):
        with self.assertRaises(ValidationError):
            self._application([self.ds1, "does-not-exist"])

    def test_duplicate_datasets_rejected(self):
        with self.assertRaises(ValidationError):
            self._application([self.ds1, self.ds1])

    def test_repeated_submission_returns_first_application(self):
        first = self._application([self.ds1, self.ds2], purpose="p")
        second = self._application(
            [self.ds2, self.ds1], purpose="p"
        )  # same set, different order
        self.assertEqual(first["id"], second["id"])

    def test_different_purpose_creates_new_application(self):
        first = self._application([self.ds1], purpose="p1")
        second = self._application([self.ds1], purpose="p2")
        self.assertNotEqual(first["id"], second["id"])

    def test_finalized_application_not_deduplicated(self):
        first = self._application([self.ds1], purpose="p")
        self._approve(first["id"], expected_version=3)
        second = self._application([self.ds1], purpose="p")
        self.assertNotEqual(first["id"], second["id"])

    def test_approved_scope_is_frozen_into_grant(self):
        app = self._application([self.ds1, self.ds2])
        approved = self._approve(app["id"])
        scope = approved["data"]["approved_scope"]
        self.assertEqual(scope["dataset_ids"], sorted([self.ds1, self.ds2]))
        self.assertEqual(scope["terms"], "noncommercial")

        grant = self._grant(approved["id"])
        self.assertEqual(grant["data"]["scope"]["dataset_ids"], sorted([self.ds1, self.ds2]))

    def test_grant_scope_cannot_be_widened_by_application_edits(self):
        app = self._application([self.ds1])
        approved = self._approve(app["id"])
        grant = self._grant(approved["id"])

        # Post-approval edits are forbidden, and even if the application moved
        # through a new lifecycle the issued grant keeps its frozen scope.
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                APPLICANT,
                approved["id"],
                "update_scope",
                {"dataset_ids": [self.ds1, self.ds2, self.ds3]},
                expected_version=approved["version"],
            )
        grant = self.service.get(grant["id"])
        self.assertEqual(grant["data"]["scope"]["dataset_ids"], [self.ds1])

    def test_grant_requesting_datasets_outside_approved_scope_rejected(self):
        app = self._application([self.ds1])
        approved = self._approve(app["id"])
        with self.assertRaises(ValidationError):
            self.service.create(
                COMMITTEE,
                "grant",
                {
                    "application_id": approved["id"],
                    "recipient": "researcher-1",
                    "dataset_ids": [self.ds1, self.ds2],
                },
            )

    def test_concurrent_scope_edits_first_wins_second_conflicts(self):
        app = self._application([self.ds1])
        # version 1 on create
        first = self.service.transition(
            APPLICANT,
            app["id"],
            "update_scope",
            {"dataset_ids": [self.ds1, self.ds2]},
            expected_version=1,
        )
        self.assertEqual(first["version"], 2)
        self.assertEqual(first["data"]["dataset_ids"], sorted([self.ds1, self.ds2]))
        # The second user still holds version 1 and must re-confirm.
        with self.assertRaises(ConflictError):
            self.service.transition(
                APPLICANT,
                app["id"],
                "update_scope",
                {"dataset_ids": [self.ds1, self.ds3]},
                expected_version=1,
            )
        # Re-reading and submitting the new version succeeds.
        current = self.service.get(app["id"])
        retried = self.service.transition(
            APPLICANT,
            app["id"],
            "update_scope",
            {"dataset_ids": [self.ds1, self.ds3]},
            expected_version=current["version"],
        )
        self.assertEqual(retried["data"]["dataset_ids"], sorted([self.ds1, self.ds3]))

    def test_update_scope_requires_expected_version(self):
        app = self._application([self.ds1])
        with self.assertRaises(ValidationError):
            self.service.transition(
                APPLICANT,
                app["id"],
                "update_scope",
                {"dataset_ids": [self.ds1, self.ds2]},
            )

    def test_viewer_cannot_update_scope(self):
        app = self._application([self.ds1])
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"),
                app["id"],
                "update_scope",
                {"dataset_ids": [self.ds2]},
                expected_version=1,
            )

    def test_restricted_dataset_suspends_related_grants_and_keeps_scope(self):
        app = self._application([self.ds1, self.ds2])
        approved = self._approve(app["id"])
        grant = self._grant(approved["id"])
        self.service.transition(
            COMMITTEE,
            grant["id"],
            "activate",
            {"starts_at": "2026-09-29", "expires_at": "2099-01-01"},
        )

        restricted = self.service.transition(
            ADMIN, self.ds2, "restrict", {"reason": "policy review"}
        )
        self.assertEqual(restricted["suspended_grant_ids"], [grant["id"]])

        suspended = self.service.get(grant["id"])
        self.assertEqual(suspended["status"], "suspended")
        # The frozen scope survives suspension intact.
        self.assertEqual(
            suspended["data"]["scope"]["dataset_ids"], sorted([self.ds1, self.ds2])
        )
        # Suspended grants can still be revoked.
        revoked = self.service.transition(
            COMMITTEE, suspended["id"], "revoke", {"reason": "cleanup"}
        )
        self.assertEqual(revoked["status"], "revoked")

        records = [
            item
            for item in self.service.audit_log(grant["id"])
            if item["action"] == "suspend"
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["detail"]["scope"]["dataset_ids"], sorted([self.ds1, self.ds2])
        )
        self.assertEqual(records[0]["detail"]["source_id"], self.ds2)

    def test_unrelated_grant_not_suspended(self):
        other = self._application([self.ds3])
        approved = self._approve(other["id"])
        other_grant = self._grant(approved["id"])

        self.service.transition(
            ADMIN, self.ds1, "restrict", {"reason": "policy review"}
        )
        self.assertEqual(self.service.get(other_grant["id"])["status"], "issued")

    def test_withdrawn_application_suspends_grant(self):
        app = self._application([self.ds1])
        approved = self._approve(app["id"])
        grant = self._grant(approved["id"])
        withdrawn = self.service.transition(
            APPLICANT, approved["id"], "withdraw", {"reason": "no longer needed"}
        )
        self.assertEqual(withdrawn["status"], "withdrawn")
        self.assertEqual(withdrawn["suspended_grant_ids"], [grant["id"]])
        self.assertEqual(self.service.get(grant["id"])["status"], "suspended")

    def test_cannot_approve_scope_containing_restricted_dataset(self):
        app = self._application([self.ds1, self.ds2])
        self.service.transition(APPLICANT, app["id"], "submit", {})
        self.service.transition(
            COMMITTEE, app["id"], "review", {"committee_id": "c"}
        )
        self.service.transition(
            ADMIN, self.ds2, "restrict", {"reason": "policy review"}
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                COMMITTEE,
                app["id"],
                "approve",
                {
                    "approvals": ["r1", "r2", "r3"],
                    "terms": "t",
                    "expires_at": "2099-01-01",
                },
            )


if __name__ == "__main__":
    unittest.main()
