from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.config.database import Base


class StripWithdrawal(Base):
    """复核员确认记错的历史控制条撤回记录。

    每条错条在同一运行上只记录一次；重试复算不重复写入。原运行结果与
    当时的复核依据保持不变，撤回历史始终可查。
    """

    __tablename__ = 'strip_withdrawals'
    __table_args__ = (UniqueConstraint('run_id', 'strip_id', name='uq_withdrawal_run_strip'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('analysis_runs.id'), index=True)
    strip_id: Mapped[str] = mapped_column(String(48), index=True)
    split: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    reviewer_id: Mapped[int] = mapped_column(ForeignKey('accounts.id'))
    created_at: Mapped[str] = mapped_column(String(32))
