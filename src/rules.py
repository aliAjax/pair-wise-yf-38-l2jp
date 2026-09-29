from datetime import datetime, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


# Applications in these states are still considered open for the same applicant,
# so a repeated submission with the same scope resolves to the first application.
OPEN_APPLICATION_STATUSES = ("draft", "submitted", "under_review")
# Grants in these states can still be used (or activated) and must be stopped by
# cascading governance actions such as dataset restriction or withdrawal.
GRANT_LIVE_STATUSES = ("issued", "active")


def _unique_ids(values):
    result = []
    for value in values or []:
        if value and value not in result:
            result.append(value)
    return result


def _sorted_ids(values):
    return sorted(set(_unique_ids(values)))


def application_dataset_ids(data):
    """Canonical dataset scope of an application.

    New applications carry ``dataset_ids`` (possibly many). Legacy records only
    carry a single ``dataset_id`` and are normalized on read.
    """
    raw = data.get("dataset_ids")
    if raw is None and data.get("dataset_id"):
        raw = [data.get("dataset_id")]
    return _unique_ids(raw)


def grant_dataset_ids(data):
    """Dataset scope covered by a grant.

    The frozen ``scope`` snapshot wins; legacy grants fall back to the single
    ``dataset_id`` they were issued with.
    """
    scope = data.get("scope") or {}
    raw = scope.get("dataset_ids") or data.get("dataset_ids")
    if not raw and data.get("dataset_id"):
        raw = [data.get("dataset_id")]
    return _unique_ids(raw)


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _require_datasets_exist(ids, lookup):
    for dataset_id in ids:
        if not _find_one(lookup, "dataset", "id", dataset_id):
            raise ValidationError("dataset does not exist: " + str(dataset_id))


def _validate_dataset(actor, data, lookup):
    if len(data.get("access_policy", "")) < 3:
        raise ValidationError("access_policy is required")


def _validate_application(actor, data, lookup):
    raw = data.get("dataset_ids")
    if raw is None and data.get("dataset_id"):
        raw = [data.get("dataset_id")]
    if not isinstance(raw, list) or not raw:
        raise ValidationError("dataset_ids is required")
    if any(not item for item in raw):
        raise ValidationError("dataset_ids must not contain empty values")
    if len(raw) != len(set(raw)):
        raise ValidationError("dataset_ids must not contain duplicates")
    ids = _unique_ids(raw)
    _require_datasets_exist(ids, lookup)
    if not data.get("purpose", "").strip():
        raise ValidationError("purpose is required")
    # Canonicalize to a sorted scope so duplicate detection is deterministic.
    data["dataset_ids"] = _sorted_ids(ids)


def _validate_grant(actor, data, lookup):
    application = _find_one(lookup, "application", "id", data.get("application_id"))
    if not application:
        raise ValidationError("application does not exist")
    if application["status"] != "approved":
        raise ValidationError("grant can only be issued for an approved application")

    approved_scope = application["data"].get("approved_scope") or {}
    approved_ids = approved_scope.get("dataset_ids") or application_dataset_ids(
        application["data"]
    )
    requested = application_dataset_ids(data) or list(approved_ids)
    exceeded = [dataset_id for dataset_id in requested if dataset_id not in approved_ids]
    if exceeded:
        raise ValidationError(
            "grant scope exceeds approved application scope: " + ",".join(exceeded)
        )

    # The credential freezes the scope as approved. Whatever the client sends,
    # the grant scope is derived from the application and can never be widened
    # by later edits to the application.
    snapshot = dict(approved_scope) if approved_scope else {}
    snapshot.setdefault("dataset_ids", _sorted_ids(approved_ids))
    snapshot.setdefault("terms", application["data"].get("terms"))
    snapshot.setdefault("expires_at", application["data"].get("expires_at"))
    return {
        "dataset_ids": _sorted_ids(requested),
        "scope": snapshot,
        "applicant_id": application["data"].get("applicant_id"),
        "terms": snapshot.get("terms"),
        "expires_at": snapshot.get("expires_at"),
    }


def _validate_approve(actor, entity, data, lookup):
    approvals = data.get("approvals") or []
    if len(set(approvals)) < 3:
        raise ValidationError("at least three distinct committee approvals are required")
    if data.get("conflict_of_interest"):
        raise PermissionDenied("conflicted reviewer cannot approve access")
    ids = application_dataset_ids(entity["data"])
    if not ids:
        raise ValidationError("application has no dataset scope to approve")
    for dataset_id in ids:
        dataset = _find_one(lookup, "dataset", "id", dataset_id)
        if not dataset:
            raise ValidationError("dataset does not exist: " + str(dataset_id))
        if dataset["status"] == "restricted":
            raise ValidationError(
                "dataset is restricted and cannot be approved: " + str(dataset_id)
            )
    # Freeze the scope at approval time. Issued grants copy this snapshot and
    # later application edits never mutate it.
    return {
        "approved_scope": {
            "dataset_ids": _sorted_ids(ids),
            "terms": data.get("terms"),
            "expires_at": data.get("expires_at"),
            "application_version": entity["version"],
            "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    }


def _validate_update_scope(actor, entity, data, lookup):
    raw = data.get("dataset_ids")
    if not isinstance(raw, list) or not raw:
        raise ValidationError("dataset_ids must be a non-empty list")
    if any(not item for item in raw):
        raise ValidationError("dataset_ids must not contain empty values")
    if len(raw) != len(set(raw)):
        raise ValidationError("dataset_ids must not contain duplicates")
    ids = _unique_ids(raw)
    _require_datasets_exist(ids, lookup)
    return {"dataset_ids": _sorted_ids(ids)}


def valid_grant_window(expires_at, as_of):
    return str(expires_at) >= str(as_of)


def _validate_grant_activate(actor, entity, data, lookup):
    if data.get("expires_at") < data.get("starts_at"):
        raise ValidationError("grant expiry must be after start")
    return {"activated_by": actor.user_id}


CUSTOM_CREATE = {'dataset': _validate_dataset, 'application': _validate_application, 'grant': _validate_grant}
CUSTOM_TRANSITIONS = {
    ('application', 'approve'): _validate_approve,
    ('application', 'update_scope'): _validate_update_scope,
    ('grant', 'activate'): _validate_grant_activate,
}


class RuleEngine:
    ALIASES = {'datasets': 'dataset', 'applications': 'application', 'grants': 'grant'}
    INITIAL_STATUS = {'dataset': 'registered', 'application': 'draft', 'grant': 'issued'}
    # A next_status of None means "keep the current status" (e.g. scope edits).
    TRANSITIONS = {
        'dataset': {
            'restrict': (('registered', 'published'), 'restricted'),
            'publish': (('restricted',), 'published'),
        },
        'application': {
            'submit': (('draft',), 'submitted'),
            'review': (('submitted',), 'under_review'),
            'approve': (('under_review',), 'approved'),
            'reject': (('under_review',), 'rejected'),
            'withdraw': (('submitted', 'under_review', 'approved'), 'withdrawn'),
            'update_scope': (('draft', 'submitted', 'under_review'), None),
        },
        'grant': {
            'activate': (('issued',), 'active'),
            'revoke': (('active', 'suspended'), 'revoked'),
            'expire': (('active',), 'expired'),
            'suspend': (('issued', 'active'), 'suspended'),
        },
    }
    CREATE_REQUIRED = {
        'dataset': ('name', 'access_policy'),
        'application': ('applicant_id', 'purpose'),
        'grant': ('application_id', 'recipient'),
    }
    ACTION_REQUIRED = {
        ('dataset', 'restrict'): ('reason',),
        ('application', 'review'): ('committee_id',),
        ('application', 'approve'): ('approvals', 'terms', 'expires_at'),
        ('application', 'reject'): ('reason',),
        ('application', 'withdraw'): ('reason',),
        ('application', 'update_scope'): ('dataset_ids',),
        ('grant', 'activate'): ('starts_at', 'expires_at'),
        ('grant', 'revoke'): ('reason',),
        ('grant', 'suspend'): ('reason',),
        ('grant', 'expire'): ('expired_at',),
    }
    CREATE_ROLES = {'dataset': ('admin', 'committee'), 'application': ('admin', 'applicant'), 'grant': ('admin', 'committee')}
    ROLE_ACTIONS = {
        'restrict': ('admin', 'committee'),
        'publish': ('admin', 'committee'),
        'submit': ('admin', 'applicant'),
        'review': ('admin', 'committee'),
        'approve': ('admin', 'committee'),
        'reject': ('admin', 'committee'),
        'withdraw': ('admin', 'applicant'),
        'update_scope': ('admin', 'applicant'),
        'activate': ('admin', 'committee'),
        'revoke': ('admin', 'committee'),
        'suspend': ('admin', 'committee'),
        'expire': ('admin', 'committee'),
    }
    # These edits are cooperative: callers must re-confirm the version they saw.
    REQUIRES_EXPECTED_VERSION = {('application', 'update_scope')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    def requires_expected_version(self, kind, action):
        return (self.normalize_kind(kind), action) in self.REQUIRES_EXPECTED_VERSION

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        extra = custom(actor, data, lookup) if custom else {}
        payload = dict(data)
        if extra:
            payload.update(extra)
        return payload

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status or entity["status"], patch

    def find_duplicate_application(self, data, lookup):
        """Return the applicant's first open application with the same scope/purpose."""
        applicant_id = data.get("applicant_id")
        if not applicant_id:
            return None
        scope_key = tuple(_sorted_ids(application_dataset_ids(data)))
        purpose = (data.get("purpose") or "").strip()
        if not scope_key or not purpose:
            return None
        for application in lookup("application", "applicant_id", applicant_id):
            if application["status"] not in OPEN_APPLICATION_STATUSES:
                continue
            candidate_scope = tuple(
                _sorted_ids(application_dataset_ids(application["data"]))
            )
            candidate_purpose = (application["data"].get("purpose") or "").strip()
            if candidate_scope == scope_key and candidate_purpose == purpose:
                return application
        return None

    def present(self, entity):
        """Add canonical scope fields without mutating stored or audit data."""
        if entity is None:
            return None
        kind = self.normalize_kind(entity["kind"])
        data = dict(entity["data"])
        if kind == "application":
            ids = application_dataset_ids(data)
            data["dataset_ids"] = ids
            if ids and not data.get("dataset_id"):
                data["dataset_id"] = ids[0]
        elif kind == "grant":
            ids = grant_dataset_ids(data)
            data["dataset_ids"] = ids
            scope = dict(data.get("scope") or {})
            scope.setdefault("dataset_ids", ids)
            data["scope"] = scope
            if ids and not data.get("dataset_id"):
                data["dataset_id"] = ids[0]
        return dict(entity, data=data)


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
