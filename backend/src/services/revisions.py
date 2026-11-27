import logging

from sqlalchemy import select, update
from fastapi import HTTPException

from src.constants.workflow import Decision, RevisionStatus, RunStatus
from src.domain.revisions import compare_batches, recalculate, split_label, withdrawn_split
from src.models.review import Review
from src.models.revision import RunRevision
from src.models.revision_review import RevisionReview
from src.models.run import Run
from src.models.withdrawal import StripWithdrawal
from src.services.catalog import get_dataset
from src.utils.time import utc_stamp


logger = logging.getLogger(__name__)
REFERENCE_SPLITS = ('reference_fit', 'reference_cal')


def acquire_write_lock(session, run_id):
    """先取得 SQLite 写锁，使并发撤条/复算在同一运行上串行执行。"""
    session.rollback()
    session.execute(update(Run).where(Run.id == run_id).values(status=Run.status))


def load_context(session, run_id):
    run = session.get(Run, run_id)
    if run is None:
        raise LookupError('找不到运行')
    review = session.get(Review, run_id)
    revision = session.scalar(select(RunRevision).where(RunRevision.run_id == run_id))
    return run, review, revision


def ensure_formal_sheet(run, review):
    if run.status == RunStatus.FAILED or run.result is None:
        raise HTTPException(409, '计算失败的运行没有正式回查单')
    if review is None or review.decision != Decision.APPROVED:
        raise HTTPException(409, '仅已签核生效的正式回查单可撤回错条')


def withdrawal_info(item):
    return {
        'strip_id': item.strip_id,
        'split': item.split,
        'reason': item.reason,
        'reviewer_id': item.reviewer_id,
        'created_at': item.created_at,
    }


def revision_review_info(item):
    return {
        'decision': item.decision,
        'note': item.note,
        'reviewer_id': item.reviewer_id,
        'created_at': item.created_at,
    }


def effective_result(run, revision):
    """当前有效回查单：复算生效后取复算结果，否则取原签核结果。"""
    if revision is not None and revision.status == RevisionStatus.ACTIVE:
        return revision.result
    return run.result


def revision_summary(revision):
    if revision is None:
        return None
    return {
        'status': revision.status,
        'excluded_count': len(revision.excluded_strip_ids),
        'requires_review': revision.requires_review,
        'effective': revision.status == RevisionStatus.ACTIVE,
    }


def list_withdrawals(session, run_id):
    statement = select(StripWithdrawal).where(StripWithdrawal.run_id == run_id).order_by(StripWithdrawal.id)
    return [withdrawal_info(item) for item in session.scalars(statement)]


def list_revision_reviews(session, run_id):
    statement = select(RevisionReview).where(RevisionReview.run_id == run_id).order_by(RevisionReview.id)
    return [revision_review_info(item) for item in session.scalars(statement)]


def apply_recalculation(session, run, revision, excluded_ids):
    """执行复算并把结果落到复算链；任何分支都不写入新的撤回记录。"""
    now = utc_stamp()
    try:
        new_result = recalculate(get_dataset(session, run.dataset_id).rows, run.settings, excluded_ids)
    except ValueError as error:
        failure = str(error)
    except Exception:
        # 复算不成就继续暂停，等待重试，避免把旧正式单重新暴露给下载。
        logger.exception('错条剔除后复算失败，运行 %s 维持暂停', run.id)
        failure = '复算遇到内部错误，请检查台账后重试。'
    else:
        failure = None
    if failure is not None:
        _persist_revision(
            session, revision, run.id,
            status=RevisionStatus.SUSPENDED, excluded=excluded_ids,
            result=None, error=failure, comparison=None, requires_review=False, now=now,
        )
        _set_run_status(session, run.id, RunStatus.SUSPENDED)
        session.commit()
        return False

    comparison = compare_batches(run.result, new_result)
    requires_review = bool(
        comparison['changed_batches']
        or comparison['added_batches']
        or comparison['removed_batches']
    )
    if requires_review:
        status, run_status = RevisionStatus.SUSPENDED, RunStatus.SUSPENDED
    else:
        # 各换槽批主条、档位、工位均未变：恢复原签核，正式回查单即时生效。
        status, run_status = RevisionStatus.ACTIVE, RunStatus.COMPLETED
    _persist_revision(
        session, revision, run.id,
        status=status, excluded=excluded_ids,
        result=new_result, error=None, comparison=comparison,
        requires_review=requires_review, now=now,
    )
    _set_run_status(session, run.id, run_status)
    session.commit()
    return not requires_review


def _persist_revision(session, revision, run_id, *, status, excluded, result, error,
                      comparison, requires_review, now):
    if revision is None:
        session.add(RunRevision(
            run_id=run_id, status=status, excluded_strip_ids=excluded, result=result,
            error=error, comparison=comparison, requires_review=requires_review,
            lock_version=1, created_at=now, updated_at=now,
        ))
        session.flush()
        return
    locked = session.execute(
        update(RunRevision)
        .where(RunRevision.run_id == run_id, RunRevision.lock_version == revision.lock_version)
        .values(
            status=status, excluded_strip_ids=excluded, result=result, error=error,
            comparison=comparison, requires_review=requires_review,
            lock_version=RunRevision.lock_version + 1, updated_at=now,
        )
    )
    if locked.rowcount != 1:
        raise HTTPException(409, '回查单刚被其他撤条操作更新，请刷新后重试')


def _set_run_status(session, run_id, status):
    session.execute(update(Run).where(Run.id == run_id).values(status=status))


def withdraw_strip(session, run_id, payload, actor):
    acquire_write_lock(session, run_id)
    run, review, revision = load_context(session, run_id)
    ensure_formal_sheet(run, review)

    strip_id = payload.strip_id
    split = withdrawn_split(get_dataset(session, run.dataset_id).rows, strip_id)
    if split is None:
        raise LookupError('台账中找不到该控制条编号')
    if split not in REFERENCE_SPLITS:
        raise ValueError('仅可剔除历史拟合或历史校准控制条，当班样本须保持不变')

    existing = {item.strip_id for item in session.scalars(
        select(StripWithdrawal).where(StripWithdrawal.run_id == run_id))}
    if strip_id in existing:
        raise HTTPException(409, '该控制条已撤回，请勿重复操作')

    excluded = list(revision.excluded_strip_ids) if revision else []
    excluded.append(strip_id)
    session.add(StripWithdrawal(
        run_id=run_id, strip_id=strip_id, split=split_label(split), reason=payload.reason,
        reviewer_id=actor.id, created_at=utc_stamp(),
    ))
    session.flush()  # 唯一约束作为并发重复撤回的最后防线

    # 先暂停引用该错条的正式回查单，再剔除复算。
    _set_run_status(session, run_id, RunStatus.SUSPENDED)
    restored = apply_recalculation(session, run, revision, excluded)
    return revision_detail(session, run_id, actor, restored=restored)


def retry_recalculation(session, run_id, actor):
    acquire_write_lock(session, run_id)
    run, _, revision = load_context(session, run_id)
    if revision is None:
        raise HTTPException(409, '该运行没有暂停中的复算，无需重试')
    if revision.status == RevisionStatus.ACTIVE:
        raise HTTPException(409, '当前回查单已生效，无需重试')
    if revision.result is not None:
        raise HTTPException(409, '复算已完成，请对新增、撤单或变化的批次重新签核')
    # 重试沿用既有剔除清单，不重复留下撤回记录。
    restored = apply_recalculation(session, run, revision, list(revision.excluded_strip_ids))
    return revision_detail(session, run_id, actor, restored=restored)


def submit_revision_review(session, run_id, payload, actor):
    acquire_write_lock(session, run_id)
    run, _, revision = load_context(session, run_id)
    if revision is None or revision.status != RevisionStatus.SUSPENDED:
        raise HTTPException(409, '仅暂停中的复算结果可重新签核')
    if revision.result is None or not revision.requires_review:
        raise HTTPException(409, '复算尚未成功或批次没有变化，不能重新签核')

    session.add(RevisionReview(
        run_id=run_id, reviewer_id=actor.id, decision=payload.decision,
        note=payload.note, created_at=utc_stamp(),
    ))
    if payload.decision == Decision.APPROVED:
        now = utc_stamp()
        locked = session.execute(
            update(RunRevision)
            .where(RunRevision.run_id == run_id, RunRevision.lock_version == revision.lock_version)
            .values(status=RevisionStatus.ACTIVE, lock_version=RunRevision.lock_version + 1,
                    updated_at=now)
        )
        if locked.rowcount != 1:
            raise HTTPException(409, '回查单刚被其他撤条操作更新，请刷新后重试')
        _set_run_status(session, run_id, RunStatus.COMPLETED)
    session.commit()
    return revision_detail(session, run_id, actor)


def revision_detail(session, run_id, actor, restored=None):
    run, review, revision = load_context(session, run_id)
    if revision is None:
        raise LookupError('该运行没有错条撤回与复算记录')
    if actor.role == 'viewer' and not (review and review.decision == Decision.APPROVED
                                       and revision.status == RevisionStatus.ACTIVE):
        raise LookupError('找不到可查看的运行')
    return {
        'run_id': run_id,
        'status': revision.status,
        'requires_review': revision.requires_review,
        'excluded_strip_ids': list(revision.excluded_strip_ids),
        'error': revision.error,
        'comparison': revision.comparison,
        'withdrawals': list_withdrawals(session, run_id),
        'revision_reviews': list_revision_reviews(session, run_id),
        'original_review': {
            'decision': review.decision,
            'note': review.note,
            'created_at': review.created_at,
        } if review else None,
        'effective': revision.status == RevisionStatus.ACTIVE,
        'auto_restored': restored if restored is not None else (
            revision.status == RevisionStatus.ACTIVE and not revision.requires_review
            and not list_revision_reviews(session, run_id)
        ),
    }
