from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.config.database import Base


class RunRevision(Base):
    """运行的错条剔除复算链，每个运行至多一条，随每次撤条更新。

    status:
      suspended  正式回查单已暂停，等待或正在进行复算/重新签核；
      active     复算结果与当前正式回查单一致（自动恢复原签核或已重新签核）。
    effective_result 为当前有效回查单对应的完整结果；原结果仍保留在
    analysis_runs.result 上，原签核仍保留在 reviews 表中。
    """

    __tablename__ = 'run_revisions'
    __table_args__ = (UniqueConstraint('run_id', name='uq_revision_run'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('analysis_runs.id'), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    excluded_strip_ids: Mapped[list] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text)
    comparison: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    requires_review: Mapped[bool] = mapped_column(default=False)
    lock_version: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32))
