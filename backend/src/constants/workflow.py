from enum import StrEnum


class Role(StrEnum):
    ADMIN = 'admin'
    ANALYST = 'analyst'
    VIEWER = 'viewer'


class RunStatus(StrEnum):
    COMPLETED = 'completed'
    FAILED = 'failed'
    SUSPENDED = 'suspended'


class RevisionStatus(StrEnum):
    SUSPENDED = 'suspended'
    ACTIVE = 'active'


class Decision(StrEnum):
    APPROVED = 'approved'
    REJECTED = 'rejected'
