from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine, grant_dataset_ids


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _present(self, entity):
        return self.rules.present(entity)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return self._present(entity)
        # The same applicant resubmitting an open application with an identical
        # scope gets back the application generated the first time.
        duplicate = None
        if kind == "application":
            duplicate = self.rules.find_duplicate_application(payload, self._lookup)
        if duplicate is None:
            payload = self.rules.validate_create(actor, kind, payload, self._lookup)
            entity_id = str(payload.pop("id", "") or uuid4())
            if self.repository.get_entity(entity_id):
                raise ConflictError("entity already exists: " + entity_id)
            status = self.rules.initial_status(kind)
            duplicate = self.repository.create_entity(
                entity_id, kind, status, payload, actor.user_id
            )
            self.audit.record(
                duplicate["id"], actor, "create", None, status, {"kind": kind}
            )
            if idempotency_key:
                self.repository.save_idempotency(
                    actor.user_id, idempotency_key, duplicate["id"]
                )
        return self._present(duplicate)

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if expected_version is None and self.rules.requires_expected_version(
            kind, action
        ):
            # Cooperative scope edits cannot blindly overwrite a colleague's edit.
            raise ValidationError("expected_version is required for action " + action)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        suspended = self._cascade_stop(actor, updated, action)
        if suspended:
            updated = self.repository.get_entity(entity_id)
            updated = dict(updated, suspended_grant_ids=suspended)
        return self._present(updated)

    def _cascade_stop(self, actor, entity, action):
        """Stop grants that must no longer be usable after a governance action.

        The grant rows keep their frozen scope; only their status moves to
        ``suspended``, and every change is written to the audit timeline.
        """
        kind = self.rules.normalize_kind(entity["kind"])
        grants = []
        reason = None
        if kind == "dataset" and action == "restrict":
            reason = "dataset restricted: " + entity["id"]
            for grant in self.repository.list_entities(kind="grant"):
                if entity["id"] in grant_dataset_ids(grant["data"]):
                    grants.append(grant)
        elif kind == "application" and action == "withdraw":
            reason = "application withdrawn: " + entity["id"]
            grants = self._lookup("grant", "application_id", entity["id"])
        result = []
        for grant in grants:
            if grant["status"] not in ("issued", "active"):
                continue
            suspended = self.repository.update_entity(
                grant["id"], grant["version"], "suspended", dict(grant["data"])
            )
            self.audit.record(
                grant["id"],
                actor,
                "suspend",
                grant["status"],
                "suspended",
                {
                    "reason": reason,
                    "scope": suspended["data"].get("scope"),
                    "dataset_ids": grant_dataset_ids(suspended["data"]),
                    "source_kind": kind,
                    "source_id": entity["id"],
                },
            )
            result.append(grant["id"])
        return result

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return self._present(entity)

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return [self._present(entity) for entity in self.repository.list_entities(kind=kind, status=status)]

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
