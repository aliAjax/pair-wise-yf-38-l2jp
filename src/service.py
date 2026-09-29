from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
)
from .rules import OPEN_APPLICATION_STATUSES, RuleEngine, normalize_dataset_ids


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field=None, value=None):
        kind = self.rules.normalize_kind(kind)
        if field is None or value is None:
            return self.repository.list_entities(kind=kind)
        return self.repository.find_entities(kind, field, value)

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
                    return entity
        # Normalize first so duplicate detection compares canonical scopes.
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if kind == "application":
            duplicate = self._find_open_duplicate(validated)
            if duplicate:
                return duplicate
        entity_id = str(validated.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, validated, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _find_open_duplicate(self, payload):
        """Return the earliest still-open application with the same request.

        Keyed on applicant + purpose + exact dataset scope, so a repeat
        submission by the same applicant receives the first application rather
        than creating another one. Withdrawn/rejected requests are terminal
        and may be resubmitted.
        """
        applicant = payload.get("applicant_id")
        purpose = payload.get("purpose", "")
        scope = normalize_dataset_ids(payload)
        if not applicant or not scope:
            return None
        candidates = self.repository.find_entities(
            "application", "applicant_id", applicant
        )
        for candidate in candidates:
            if candidate["status"] not in OPEN_APPLICATION_STATUSES:
                continue
            if candidate["data"].get("purpose", "") != purpose:
                continue
            try:
                candidate_scope = normalize_dataset_ids(candidate["data"])
            except Exception:
                continue
            if candidate_scope == scope:
                return candidate
        return None

    def transition(self, actor, entity_id, action, data=None, expected_version=None,
                   _cascade=True):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
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
            {"patch": patch, "version_before": entity["version"]},
        )
        # Stop related grants after restrict/withdraw. A failing cascade for
        # one grant never rolls back the primary decision or hides the others.
        if _cascade:
            self._run_cascades(actor, updated, action)
        return updated

    def _run_cascades(self, actor, entity, action):
        directives = self.rules.cascade_directives(entity, action, self._lookup)
        system_actor = type(actor)(user_id="system:" + actor.user_id, role=actor.role)
        for directive in directives:
            try:
                # Re-read through optimistic lock: if the grant moved on
                # between directive generation and execution, skip it.
                self.transition(
                    system_actor,
                    directive["entity_id"],
                    directive["action"],
                    directive["data"],
                    expected_version=directive["expected_version"],
                    _cascade=False,
                )
            except (ConflictError, InvalidTransition, NotFoundError, PermissionDenied):
                continue

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
