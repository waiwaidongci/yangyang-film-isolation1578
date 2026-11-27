from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.config.database import Base


class RevisionReview(Base):
    """复算后对发生变化批次的重新签核。

    原 reviews 表的首次签核与其依据不可变、始终可查；仅当复算产生新增、
    撤单或变化批次时在此重新签核，通过后复算结果才成为当前正式回查单。
    """

    __tablename__ = 'revision_reviews'

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('analysis_runs.id'), index=True)
    reviewer_id: Mapped[int] = mapped_column(ForeignKey('accounts.id'))
    decision: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32))
