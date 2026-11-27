import math

from fastapi import HTTPException
from sqlalchemy import select

from src.constants.workflow import Decision, RevisionStatus, Role, RunStatus
from src.domain.project import SPEC
from src.models.review import Review
from src.models.revision import RunRevision
from src.models.run import Run
from src.services.catalog import get_dataset
from src.services.execution import calculate
from src.services.revisions import effective_result, revision_summary
from src.utils.time import utc_stamp


def run_info(run, review, revision=None):
    fields = ('id', 'label', 'dataset_id', 'created_at', 'status', 'settings')
    return {
        **{key: getattr(run, key) for key in fields},
        'decision': review.decision if review else None,
        'revision': revision_summary(revision),
    }


def list_runs(session, actor):
    statement = (
        select(Run, Review, RunRevision)
        .outerjoin(Review, Run.id == Review.run_id)
        .outerjoin(RunRevision, Run.id == RunRevision.run_id)
        .order_by(Run.id.desc())
        .limit(100)
    )
    rows = session.execute(statement)
    if actor.role == Role.VIEWER:
        # 班长与工位只能看到当前有效（原签核生效或复算已生效）的正式回查单。
        rows = [item for item in rows
                if item[1] is not None and item[1].decision == Decision.APPROVED
                and (item[2] is None or item[2].status == RevisionStatus.ACTIVE)]
    return [run_info(run, review, revision) for run, review, revision in rows]


def get_run(session, run_id, actor):
    run = session.get(Run, run_id)
    review = session.get(Review, run_id)
    revision = session.scalar(select(RunRevision).where(RunRevision.run_id == run_id))
    effective = revision is None or revision.status == RevisionStatus.ACTIVE
    if run is None or (actor.role == Role.VIEWER
                       and (not review or review.decision != Decision.APPROVED or not effective)):
        raise LookupError('找不到可查看的运行')
    return run, review, revision


def validate_settings(settings):
    parameters = {item['key']: item for item in SPEC['settings']}
    if settings.keys() - parameters.keys():
        raise ValueError('存在本项目未定义的运行参数')
    checked = {}
    for key, parameter in parameters.items():
        value = settings.get(key, parameter['default'])
        if not math.isfinite(value) or not parameter['min'] <= value <= parameter['max']:
            raise ValueError(f"{parameter['label']}超出范围")
        if parameter['step'] == 1 and value != int(value):
            raise ValueError(f"{parameter['label']}必须是整数")
        checked[key] = int(value) if parameter['step'] == 1 else value
    return checked


def create_run(session, payload, actor):
    rows = get_dataset(session, payload.dataset_id).rows
    actor_id = actor.id
    settings = validate_settings(payload.settings)
    created_at = utc_stamp()
    session.rollback()
    result, error = calculate(rows, settings)
    run = Run(label=payload.label, dataset_id=payload.dataset_id, created_by=actor_id,
              created_at=created_at, settings=settings, result=result, error=error,
              status=RunStatus.FAILED if error is not None else RunStatus.COMPLETED)
    session.add(run)
    session.commit()
    return run_info(run, None)


def export_table(session, run_id, key, actor):
    run, review, revision = get_run(session, run_id, actor)
    if review is None or review.decision != Decision.APPROVED:
        raise HTTPException(409, '复核通过后可下载正式 CSV')
    if revision is not None and revision.status != RevisionStatus.ACTIVE:
        # 暂停中的旧回查单不得下载，下载只给当前有效的回查单。
        raise HTTPException(409, '正式回查单因错条撤回处于暂停中，复算生效后可下载')
    for table in effective_result(run, revision)['tables']:
        if table['key'] == key:
            return table
    raise LookupError('找不到结果表')
