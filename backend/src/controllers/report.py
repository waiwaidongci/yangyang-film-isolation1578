from fastapi.responses import Response

from src.constants.workflow import Role
from src.services.reviews import review_info
from src.services.revisions import (
    effective_result,
    list_revision_reviews,
    list_withdrawals,
)
from src.services.runs import export_table, get_run, run_info
from src.utils.csv_files import write_rows


def report_detail(session, run_id, actor):
    run, review, revision = get_run(session, run_id, actor)
    payload = {
        **run_info(run, review, revision),
        'result': effective_result(run, revision),
        'error': run.error,
        'review': review_info(review) if review else None,
    }
    if revision is not None:
        # 原签核和当时的依据仍可查；班长与工位只看当前有效结果。
        payload['revision'] = {
            'status': revision.status,
            'requires_review': revision.requires_review,
            'excluded_strip_ids': list(revision.excluded_strip_ids),
            'error': revision.error,
            'comparison': revision.comparison,
            'withdrawals': list_withdrawals(session, run_id),
            'revision_reviews': list_revision_reviews(session, run_id),
        }
        if actor.role != Role.VIEWER:
            payload['original_result'] = run.result
    return payload


def report_csv(session, run_id, key, actor):
    table = export_table(session, run_id, key, actor)
    return Response(write_rows(table['columns'], table['rows']), media_type='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="run-{run_id}-{table["key"]}.csv"'})
