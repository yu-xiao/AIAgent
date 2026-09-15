"""SQLAlchemy models for the P1 modular monolith."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class RunJobStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


class AgentStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class AgentReleaseAction(StrEnum):
    DEPLOY = "deploy"
    ROLLBACK = "rollback"


class AgentReleaseStatus(StrEnum):
    DEPLOYED = "deployed"


class EvaluationDatasetStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class EvaluationRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class McpTransport(StrEnum):
    STREAMABLE_HTTP = "streamable_http"


class McpAuthMode(StrEnum):
    OAUTH_AUTHORIZATION_CODE = "oauth_authorization_code"
    CLIENT_CREDENTIALS = "client_credentials"
    API_KEY = "api_key"


class ConnectionOwnership(StrEnum):
    PERSONAL = "personal"
    ORGANIZATION = "organization"


class ConnectionStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"
    DISCONNECTED = "disconnected"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_users_issuer_subject"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    issuer: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class LocalCredential(Base):
    __tablename__ = "local_credentials"

    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class OrganizationMember(TimestampMixin, Base):
    __tablename__ = "organization_members"
    __table_args__ = (UniqueConstraint("organization_id", "user_id", name="uq_members_org_user"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )


class Permission(Base):
    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(String(100), primary_key=True)
    description: Mapped[str] = mapped_column(String(300), nullable=False)


class Role(TimestampMixin, Base):
    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("organization_id", "code", name="uq_roles_org_code"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_code: Mapped[str] = mapped_column(
        ForeignKey("permissions.code", ondelete="CASCADE"), primary_key=True
    )


class MemberRole(Base):
    __tablename__ = "member_roles"

    member_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization_members.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_org_user_updated", "organization_id", "user_id", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_created", "conversation_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[MessageRole] = mapped_column(
        Enum(
            MessageRole,
            native_enum=False,
            length=20,
            values_callable=lambda values: [item.value for item in values],
        ),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AgentDefinition(TimestampMixin, Base):
    __tablename__ = "agent_definitions"
    __table_args__ = (
        UniqueConstraint("organization_id", "code", name="uq_agent_definitions_org_code"),
        Index("ix_agent_definitions_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(1_000), nullable=False, default="")
    status: Mapped[AgentStatus] = mapped_column(
        Enum(
            AgentStatus,
            native_enum=False,
            length=20,
            values_callable=lambda values: [item.value for item in values],
        ),
        nullable=False,
        default=AgentStatus.ACTIVE,
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


Index(
    "uq_agent_definitions_one_default_org",
    AgentDefinition.organization_id,
    unique=True,
    postgresql_where=AgentDefinition.is_default.is_(True),
    sqlite_where=AgentDefinition.is_default.is_(True),
)


class AgentDraft(TimestampMixin, Base):
    __tablename__ = "agent_drafts"

    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_definitions.id", ondelete="CASCADE"), primary_key=True
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class AgentVersion(Base):
    __tablename__ = "agent_versions"
    __table_args__ = (
        UniqueConstraint("agent_id", "version_number", name="uq_agent_versions_number"),
        Index("ix_agent_versions_org_agent", "organization_id", "agent_id", "version_number"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    config_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AgentDeployment(TimestampMixin, Base):
    __tablename__ = "agent_deployments"
    __table_args__ = (
        UniqueConstraint("agent_id", "environment", name="uq_agent_deployments_environment"),
        Index("ix_agent_deployments_org_environment", "organization_id", "environment"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    environment: Mapped[str] = mapped_column(String(30), nullable=False)
    version_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    deployed_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class AgentRelease(Base):
    __tablename__ = "agent_releases"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_agent_releases_org_idempotency"
        ),
        Index("ix_agent_releases_org_agent", "organization_id", "agent_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=False
    )
    previous_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_versions.id", ondelete="RESTRICT")
    )
    environment: Mapped[str] = mapped_column(String(30), nullable=False)
    action: Mapped[AgentReleaseAction] = mapped_column(
        Enum(
            AgentReleaseAction,
            native_enum=False,
            length=20,
            values_callable=lambda values: [item.value for item in values],
        ),
        nullable=False,
    )
    status: Mapped[AgentReleaseStatus] = mapped_column(
        Enum(
            AgentReleaseStatus,
            native_enum=False,
            length=20,
            values_callable=lambda values: [item.value for item in values],
        ),
        nullable=False,
        default=AgentReleaseStatus.DEPLOYED,
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    bypassed_gate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    evaluation_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("evaluation_runs.id", ondelete="RESTRICT")
    )
    gate_decision: Mapped[str | None] = mapped_column(String(30))
    gate_policy_digest: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    requested_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class EvaluationDataset(TimestampMixin, Base):
    __tablename__ = "evaluation_datasets"
    __table_args__ = (
        UniqueConstraint("organization_id", "code", name="uq_eval_datasets_org_code"),
        Index("ix_eval_datasets_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(1_000), nullable=False, default="")
    status: Mapped[EvaluationDatasetStatus] = mapped_column(
        Enum(
            EvaluationDatasetStatus,
            native_enum=False,
            length=20,
            values_callable=lambda values: [item.value for item in values],
        ),
        nullable=False,
        default=EvaluationDatasetStatus.ACTIVE,
    )
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class EvaluationDatasetDraft(TimestampMixin, Base):
    __tablename__ = "evaluation_dataset_drafts"

    dataset_id: Mapped[UUID] = mapped_column(
        ForeignKey("evaluation_datasets.id", ondelete="CASCADE"), primary_key=True
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cases: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class EvaluationDatasetVersion(Base):
    __tablename__ = "evaluation_dataset_versions"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id", "version_number", name="uq_eval_dataset_versions_number"
        ),
        Index(
            "ix_eval_dataset_versions_org_dataset",
            "organization_id",
            "dataset_id",
            "version_number",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    dataset_id: Mapped[UUID] = mapped_column(
        ForeignKey("evaluation_datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    cases_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    cases_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AgentEvaluationPolicy(TimestampMixin, Base):
    __tablename__ = "agent_evaluation_policies"
    __table_args__ = (
        UniqueConstraint(
            "agent_id", "environment", name="uq_agent_eval_policies_environment"
        ),
        Index("ix_agent_eval_policies_org_environment", "organization_id", "environment"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    environment: Mapped[str] = mapped_column(String(30), nullable=False)
    dataset_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("evaluation_dataset_versions.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    min_pass_rate: Mapped[float] = mapped_column(Float, nullable=False)
    max_critical_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class EvaluationRun(TimestampMixin, Base):
    __tablename__ = "evaluation_runs"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_eval_runs_org_idempotency"
        ),
        Index("ix_eval_runs_org_agent_created", "organization_id", "agent_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    agent_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    agent_config_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("evaluation_dataset_versions.id", ondelete="RESTRICT"), nullable=False
    )
    policy_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_evaluation_policies.id", ondelete="RESTRICT"), nullable=False
    )
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    min_pass_rate: Mapped[float] = mapped_column(Float, nullable=False)
    max_critical_failures: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    trace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, unique=True, default=uuid4)
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[EvaluationRunStatus] = mapped_column(
        Enum(
            EvaluationRunStatus,
            native_enum=False,
            length=20,
            values_callable=lambda values: [item.value for item in values],
        ),
        nullable=False,
        default=EvaluationRunStatus.QUEUED,
    )
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    critical_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pass_rate: Mapped[float | None] = mapped_column(Float)
    gate_passed: Mapped[bool | None] = mapped_column(Boolean)
    error_code: Mapped[str | None] = mapped_column(String(100))
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvaluationCaseResult(Base):
    __tablename__ = "evaluation_case_results"
    __table_args__ = (
        UniqueConstraint("evaluation_run_id", "case_key", name="uq_eval_results_run_case"),
        Index("ix_eval_results_org_run", "organization_id", "evaluation_run_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    evaluation_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("evaluation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    case_key: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(String(100), nullable=False)
    used_tools: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    citations_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    answer_digest: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "user_id", "idempotency_key", name="uq_runs_org_user_idempotency"
        ),
        Index("ix_runs_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="RESTRICT"), nullable=False
    )
    assistant_message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    agent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"), index=True
    )
    agent_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_versions.id", ondelete="RESTRICT"), index=True
    )
    agent_config_digest: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[RunStatus] = mapped_column(
        Enum(
            RunStatus,
            native_enum=False,
            length=20,
            values_callable=lambda values: [item.value for item in values],
        ),
        nullable=False,
        default=RunStatus.QUEUED,
    )
    trace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, unique=True, default=uuid4)
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    limits_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(500))
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class RunJob(TimestampMixin, Base):
    __tablename__ = "run_jobs"
    __table_args__ = (
        Index("ix_run_jobs_available", "status", "available_at", "created_at"),
        Index("ix_run_jobs_lease_expiry", "lease_expires_at"),
        Index("ix_run_jobs_org_status", "organization_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[RunJobStatus] = mapped_column(
        Enum(
            RunJobStatus,
            native_enum=False,
            length=30,
            values_callable=lambda values: [item.value for item in values],
        ),
        nullable=False,
        default=RunJobStatus.QUEUED,
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))


class RunJobAttempt(Base):
    __tablename__ = "run_job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_run_job_attempt"),
        Index("ix_run_job_attempts_job_started", "job_id", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("run_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(200), nullable=False)
    lease_token: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(30))
    error_code: Mapped[str | None] = mapped_column(String(100))


Index(
    "uq_runs_one_active_conversation",
    Run.conversation_id,
    unique=True,
    postgresql_where=Run.status.in_((RunStatus.QUEUED, RunStatus.RUNNING)),
    sqlite_where=Run.status.in_((RunStatus.QUEUED, RunStatus.RUNNING)),
)


class RunStep(Base):
    __tablename__ = "run_steps"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_run_steps_sequence"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ToolInvocation(Base):
    """Non-sensitive summary of an MCP invocation attached to a Run."""

    __tablename__ = "tool_invocations"
    __table_args__ = (
        Index("ix_tool_invocations_run_created", "run_id", "created_at"),
        Index("ix_tool_invocations_trace", "trace_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    server_code: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    arguments_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(String(500))
    trace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Citation(Base):
    """Traceable source reference without persisting business payloads."""

    __tablename__ = "citations"
    __table_args__ = (
        Index("ix_citations_run_created", "run_id", "created_at"),
        Index("ix_citations_trace", "trace_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_system: Mapped[str] = mapped_column(String(100), nullable=False)
    server_code: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(300))
    queried_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    partial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ModelUsage(Base):
    __tablename__ = "model_usage"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class McpServerDefinition(TimestampMixin, Base):
    __tablename__ = "mcp_server_definitions"
    __table_args__ = (
        UniqueConstraint("organization_id", "code", name="uq_mcp_servers_org_code"),
        Index("ix_mcp_servers_org_enabled", "organization_id", "enabled"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    system_code: Mapped[str] = mapped_column(String(100), nullable=False)
    mcp_url: Mapped[str] = mapped_column(String(2_000), nullable=False)
    transport: Mapped[McpTransport] = mapped_column(
        Enum(
            McpTransport,
            native_enum=False,
            length=40,
            values_callable=lambda values: [item.value for item in values],
        ),
        nullable=False,
        default=McpTransport.STREAMABLE_HTTP,
    )
    auth_mode: Mapped[McpAuthMode] = mapped_column(
        Enum(
            McpAuthMode,
            native_enum=False,
            length=50,
            values_callable=lambda values: [item.value for item in values],
        ),
        nullable=False,
    )
    credential_header: Mapped[str] = mapped_column(
        String(100), nullable=False, default="Authorization"
    )
    authorization_server: Mapped[str | None] = mapped_column(String(2_000))
    authorization_endpoint: Mapped[str | None] = mapped_column(String(2_000))
    token_endpoint: Mapped[str | None] = mapped_column(String(2_000))
    oauth_client_id: Mapped[str | None] = mapped_column(String(300))
    oauth_client_secret_reference: Mapped[str | None] = mapped_column(String(300))
    token_endpoint_auth_method: Mapped[str] = mapped_column(
        String(40), nullable=False, default="none"
    )
    redirect_uri: Mapped[str | None] = mapped_column(String(2_000))
    required_scope: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    allowed_tools: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    tool_sets: Mapped[dict[str, list[str]]] = mapped_column(JSON, nullable=False, default=dict)
    risk_level: Mapped[str] = mapped_column(String(30), nullable=False, default="low")
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    circuit_breaker_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    circuit_breaker_recovery_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=30.0
    )
    response_size_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=1_000_000)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class CredentialReference(TimestampMixin, Base):
    __tablename__ = "credential_references"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reference: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    vault_kind: Mapped[str] = mapped_column(String(50), nullable=False, default="memory")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExternalConnection(TimestampMixin, Base):
    __tablename__ = "external_connections"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "server_id",
            "owner_user_id",
            "ownership",
            name="uq_external_connections_owner",
        ),
        Index("ix_external_connections_org_user", "organization_id", "owner_user_id"),
        Index("ix_external_connections_server", "server_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    server_id: Mapped[UUID] = mapped_column(
        ForeignKey("mcp_server_definitions.id", ondelete="CASCADE"), nullable=False
    )
    owner_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    ownership: Mapped[ConnectionOwnership] = mapped_column(
        Enum(
            ConnectionOwnership,
            native_enum=False,
            length=20,
            values_callable=lambda values: [item.value for item in values],
        ),
        nullable=False,
    )
    status: Mapped[ConnectionStatus] = mapped_column(
        Enum(
            ConnectionStatus,
            native_enum=False,
            length=30,
            values_callable=lambda values: [item.value for item in values],
        ),
        nullable=False,
        default=ConnectionStatus.PENDING,
    )
    external_issuer: Mapped[str | None] = mapped_column(String(500))
    external_subject: Mapped[str | None] = mapped_column(String(500))
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    credential_reference: Mapped[str] = mapped_column(String(300), nullable=False)
    refresh_credential_reference: Mapped[str | None] = mapped_column(String(300))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    allowed_tools: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    allowed_tool_sets: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    connection_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )


class ExternalAuthorizationGrant(Base):
    __tablename__ = "external_authorization_grants"
    __table_args__ = (UniqueConstraint("state_hash", name="uq_external_auth_grants_state"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    server_id: Mapped[UUID] = mapped_column(
        ForeignKey("mcp_server_definitions.id", ondelete="CASCADE"), nullable=False
    )
    state_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    nonce_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ConnectionToolGrant(Base):
    __tablename__ = "connection_tool_grants"
    __table_args__ = (
        UniqueConstraint("connection_id", "tool_name", name="uq_connection_tool_grant"),
    )

    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("external_connections.id", ondelete="CASCADE"), primary_key=True
    )
    tool_name: Mapped[str] = mapped_column(String(200), primary_key=True)
    tool_set: Mapped[str | None] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_org_created", "organization_id", "created_at"),
        Index("ix_audit_logs_trace", "trace_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), index=True
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(100))
    trace_id: Mapped[UUID | None] = mapped_column(Uuid)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    integrity_hash: Mapped[str | None] = mapped_column(String(64))
    integrity_key_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
