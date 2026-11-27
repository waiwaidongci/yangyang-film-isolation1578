from fastapi import APIRouter

from src.controllers.dependencies import Actor, Admin, Db, Writer
from src.controllers.report import report_csv, report_detail
from src.schemas.revisions import RevisionReviewCreate, StripWithdrawCreate
from src.schemas.reviews import ReviewCreate, ReviewInfo
from src.schemas.runs import RunCreate, RunInfo
from src.services import reviews, revisions, runs

router = APIRouter(prefix='/runs')


@router.get('', response_model=list[RunInfo])
def list_all(session: Db, actor: Actor):
    return runs.list_runs(session, actor)


@router.post('', response_model=RunInfo, status_code=201)
def create(payload: RunCreate, session: Db, actor: Writer):
    return runs.create_run(session, payload, actor)


@router.get('/{run_id}')
def detail(run_id: int, session: Db, actor: Actor):
    return report_detail(session, run_id, actor)


@router.post('/{run_id}/review', response_model=ReviewInfo, status_code=201)
def review(run_id: int, payload: ReviewCreate, session: Db, actor: Admin):
    return reviews.submit(session, run_id, payload, actor)


@router.post('/{run_id}/withdrawals', status_code=201)
def withdraw(run_id: int, payload: StripWithdrawCreate, session: Db, actor: Admin):
    return revisions.withdraw_strip(session, run_id, payload, actor)


@router.post('/{run_id}/revision/retry')
def retry_revision(run_id: int, session: Db, actor: Admin):
    return revisions.retry_recalculation(session, run_id, actor)


@router.post('/{run_id}/revision/review', status_code=201)
def review_revision(run_id: int, payload: RevisionReviewCreate, session: Db, actor: Admin):
    return revisions.submit_revision_review(session, run_id, payload, actor)


@router.get('/{run_id}/revision')
def revision_detail(run_id: int, session: Db, actor: Actor):
    return revisions.revision_detail(session, run_id, actor)


@router.get('/{run_id}/export/{key}')
def export(run_id: int, key: str, session: Db, actor: Actor):
    return report_csv(session, run_id, key, actor)
