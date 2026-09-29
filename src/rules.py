from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


# Applications in these statuses still represent an active request from the
# applicant; duplicate submissions collapse onto the earliest matching one.
OPEN_APPLICATION_STATUSES = ("draft", "submitted", "under_review", "approved")


def normalize_dataset_ids(data):
    """Return the sorted, de-duplicated dataset list for an application/grant.

    Accepts the new ``dataset_ids`` list and the legacy single ``dataset_id``
    field so old records keep working after the upgrade.
    """
    if not isinstance(data, dict):
        raise ValidationError("dataset scope must be an object")
    raw = data.get("dataset_ids")
    if raw is None:
        single = data.get("dataset_id")
        raw = [single] if single else []
    if not isinstance(raw, (list, tuple)):
        raise ValidationError("dataset_ids must be a list")
    ids = []
    for item in raw:
        text = str(item).strip() if item is not None else ""
        if not text:
            raise ValidationError("dataset_ids must not contain empty values")
        if text not in ids:
            ids.append(text)
    return sorted(ids)


def _require_datasets_exist(scope, lookup):
    for dataset_id in scope:
        if not _find_one(lookup, "dataset", "id", dataset_id):
            raise ValidationError("dataset does not exist: " + dataset_id)


def _validate_dataset(actor, data, lookup):
    if len(data.get("access_policy", "")) < 3:
        raise ValidationError("access_policy is required")


def _validate_application(actor, data, lookup):
    if not data.get("purpose", "").strip():
        raise ValidationError("purpose is required")
    scope = normalize_dataset_ids(data)
    if not scope:
        raise ValidationError("dataset_id or dataset_ids is required")
    _require_datasets_exist(scope, lookup)
    return {"dataset_ids": scope}


def _validate_update_scope(actor, entity, data, lookup):
    scope = normalize_dataset_ids(data)
    if not scope:
        raise ValidationError("dataset_ids is required")
    _require_datasets_exist(scope, lookup)
    return {"dataset_ids": scope}


def _validate_approve(actor, entity, data, lookup):
    approvals = data.get("approvals") or []
    if len(set(approvals)) < 3:
        raise ValidationError("at least three distinct committee approvals are required")
    if data.get("conflict_of_interest"):
        raise PermissionDenied("conflicted reviewer cannot approve access")
    # Freeze the scope exactly as it is at approval time. Later edits to the
    # application must never widen permissions already derived from approval.
    scope = normalize_dataset_ids(entity["data"])
    if not scope:
        raise ValidationError("application has no dataset scope")
    return {
        "approved_scope": scope,
        "scope_application_version": entity["version"],
    }


def _validate_grant_create(actor, data, lookup):
    application = _find_one(lookup, "application", "id", data.get("application_id"))
    if not application:
        raise ValidationError("application does not exist")
    if application["status"] != "approved":
        raise ValidationError("grant can only be issued for an approved application")
    frozen_scope = application["data"].get("approved_scope")
    if not frozen_scope:
        # Defensive fallback for applications approved before the freeze field.
        frozen_scope = normalize_dataset_ids(application["data"])
    if not frozen_scope:
        raise ValidationError("approved application has no frozen scope")
    requested = normalize_dataset_ids(data)
    if requested:
        overflow = sorted(set(requested) - set(frozen_scope))
        if overflow:
            raise ValidationError(
                "grant scope exceeds approved application scope: " + ",".join(overflow)
            )
    # The credential always carries the full scope frozen at approval time.
    return {
        "dataset_ids": list(frozen_scope),
        "frozen_application_version": application["data"].get(
            "scope_application_version", application["version"]
        ),
    }


def valid_grant_window(expires_at, as_of):
    return str(expires_at) >= str(as_of)


def _validate_grant_activate(actor, entity, data, lookup):
    if data.get("expires_at") < data.get("starts_at"):
        raise ValidationError("grant expiry must be after start")
    # A grant cannot be activated while any scoped dataset is restricted or
    # its source application has been withdrawn.
    for dataset_id in normalize_dataset_ids(entity["data"]):
        dataset = _find_one(lookup, "dataset", "id", dataset_id)
        if not dataset:
            raise ValidationError("dataset no longer exists: " + dataset_id)
        if dataset["status"] == "restricted":
            raise ValidationError("dataset is restricted: " + dataset_id)
    application = _find_one(
        lookup, "application", "id", entity["data"].get("application_id")
    )
    if application and application["status"] == "withdrawn":
        raise ValidationError("source application was withdrawn")
    return {"activated_by": actor.user_id}


CUSTOM_CREATE = {
    'dataset': _validate_dataset,
    'application': _validate_application,
    'grant': _validate_grant_create,
}
CUSTOM_TRANSITIONS = {
    ('application', 'approve'): _validate_approve,
    ('application', 'update_scope'): _validate_update_scope,
    ('grant', 'activate'): _validate_grant_activate,
}


class RuleEngine:
    ALIASES = {'datasets': 'dataset', 'applications': 'application', 'grants': 'grant'}
    INITIAL_STATUS = {'dataset': 'registered', 'application': 'draft', 'grant': 'issued'}
    # A ``None`` target status means the action edits data but keeps the status.
    TRANSITIONS = {
        'dataset': {
            'restrict': (('registered',), 'restricted'),
            'publish': (('restricted',), 'published'),
        },
        'application': {
            'submit': (('draft',), 'submitted'),
            'review': (('submitted',), 'under_review'),
            'approve': (('under_review',), 'approved'),
            'reject': (('under_review',), 'rejected'),
            'withdraw': (('submitted', 'under_review', 'approved'), 'withdrawn'),
            'update_scope': (
                ('draft', 'submitted', 'under_review', 'approved'),
                None,
            ),
        },
        'grant': {
            'activate': (('issued',), 'active'),
            'suspend': (('active',), 'suspended'),
            'revoke': (('active', 'suspended'), 'revoked'),
            'expire': (('active',), 'expired'),
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
        ('grant', 'suspend'): (),
        ('grant', 'expire'): ('expired_at',),
    }
    CREATE_ROLES = {
        'dataset': ('admin', 'committee'),
        'application': ('admin', 'applicant'),
        'grant': ('admin', 'committee'),
    }
    ROLE_ACTIONS = {
        'restrict': ('admin', 'committee'),
        'publish': ('admin', 'committee'),
        'submit': ('admin', 'applicant'),
        'review': ('admin', 'committee'),
        'approve': ('admin', 'committee'),
        'reject': ('admin', 'committee'),
        'withdraw': ('admin', 'applicant'),
        ('application', 'update_scope'): ('admin', 'applicant'),
        'activate': ('admin', 'committee'),
        ('grant', 'suspend'): ('admin', 'committee', 'applicant'),
        'revoke': ('admin', 'committee'),
        'expire': ('admin', 'committee'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

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
        result = dict(data)
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            normalized = custom(actor, data, lookup)
            if isinstance(normalized, dict):
                result.update(normalized)
        return result

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
        patch = dict(data)
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        if custom:
            extra = custom(actor, entity, data, lookup)
            if isinstance(extra, dict):
                patch.update(extra)
        if next_status is None:
            next_status = entity["status"]
        return next_status, patch

    def cascade_directives(self, entity, action, lookup):
        """Decide which related grants must stop after an action.

        Only policy lives here; the service layer executes the directives and
        the repository layer persists them. Directives are generated from the
        grants' frozen scopes, never from the application's current scope.
        """
        kind = self.normalize_kind(entity["kind"])
        directives = []
        if kind == "dataset" and action == "restrict":
            for grant in lookup("grant", None, None) or []:
                scope = normalize_dataset_ids(grant["data"])
                if grant["status"] == "active" and entity["id"] in scope:
                    directives.append(
                        self._suspend_directive(
                            grant,
                            reason="dataset restricted: " + entity["id"],
                            cause={"kind": "dataset", "id": entity["id"], "action": action},
                        )
                    )
        elif kind == "application" and action == "withdraw":
            for grant in lookup("grant", "application_id", entity["id"]) or []:
                if grant["status"] == "active":
                    directives.append(
                        self._suspend_directive(
                            grant,
                            reason="application withdrawn: " + entity["id"],
                            cause={"kind": "application", "id": entity["id"], "action": action},
                        )
                    )
        return directives

    @staticmethod
    def _suspend_directive(grant, reason, cause):
        return {
            "entity_id": grant["id"],
            "expected_version": grant["version"],
            "action": "suspend",
            "data": {
                "reason": reason,
                # Echo the frozen scope so the audit row itself records the
                # original scope, even if other records change later.
                "scope": normalize_dataset_ids(grant["data"]),
                "suspended_cause": cause,
            },
        }


def _find_one(lookup, kind, field, value):
    if lookup is None or value in (None, ""):
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
