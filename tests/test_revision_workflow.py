"""错条撤回—暂停—复算—签核恢复工作流的服务层验收。

运行：
  PYTHONPATH=backend backend/.venv/bin/python -m unittest tests.test_revision_workflow -v
本文件不依赖框架 HTTP，仅通过 SQLAlchemy 会话驱动 services 层。
"""

import os
import tempfile
import unittest
from types import SimpleNamespace

_DB_FD, _DB_PATH = tempfile.mkstemp(prefix='film-revision-', suffix='.db')
os.environ['DATABASE_URL'] = f'sqlite:///{_DB_PATH}'
os.environ.setdefault('SEED_DEMO', 'false')
os.environ.setdefault('JWT_SECRET', 'test-secret')

from sqlalchemy import select, text  # noqa: E402

from src.config.database import Session, engine  # noqa: E402
from src.constants.workflow import Decision, RevisionStatus, Role, RunStatus  # noqa: E402
from src.domain.fixtures import demo_rows  # noqa: E402
from src.domain.project import analyze  # noqa: E402
from src.domain.revisions import compare_batches  # noqa: E402
from src.models.account import Account  # noqa: E402
from src.models.dataset import Dataset  # noqa: E402
from src.models.revision import RunRevision  # noqa: E402
from src.models.revision_review import RevisionReview  # noqa: E402
from src.models.review import Review  # noqa: E402
from src.models.run import Run  # noqa: E402
from src.models.withdrawal import StripWithdrawal  # noqa: E402
from src.schemas.reviews import ReviewCreate  # noqa: E402
from src.schemas.revisions import RevisionReviewCreate, StripWithdrawCreate  # noqa: E402
from src.schemas.runs import RunCreate  # noqa: E402
from src.services import revisions, reviews, runs  # noqa: E402
from src.services.catalog import add_dataset  # noqa: E402
from src.utils.security import hash_password  # noqa: E402

from migrations.bootstrap import bootstrap  # noqa: E402


class RevisionWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bootstrap()
        cls.rows = demo_rows()
        cls.hist_fit = next(r['id'] for r in cls.rows if r['split'] == 'reference_fit')
        cls.hist_cal = next(r['id'] for r in cls.rows if r['split'] == 'reference_cal')
        cls.shift_strip = next(r['id'] for r in cls.rows if r['split'] == 'shift')
        cls.changed_candidate = cls._find_changed_candidate()

    @classmethod
    def _find_changed_candidate(cls):
        base_result = analyze(cls.rows, {})
        for row in cls.rows:
            if row['split'] not in ('reference_fit', 'reference_cal'):
                continue
            try:
                from src.domain.revisions import recalculate
                new_result = recalculate(cls.rows, {}, [row['id']])
            except ValueError:
                continue
            comparison = compare_batches(base_result, new_result)
            if comparison['changed_batches'] or comparison['added_batches'] or comparison['removed_batches']:
                return row['id']
        raise AssertionError('演示数据中应存在改变批次口径的剔除候选')

    def setUp(self):
        # 每个用例使用独立的内存数据库内容：清空所有业务表。
        with Session.begin() as session:
            for model in (RevisionReview, StripWithdrawal, RunRevision, Review, Run, Dataset, Account):
                session.query(model).delete()
            self.admin = self._account(session, Role.ADMIN)
            self.analyst = self._account(session, Role.ANALYST)
            self.viewer = self._account(session, Role.VIEWER)
            self.dataset = add_dataset(session, 'demo-revision.csv', self.rows, self.admin.id)
        self.run_id = self._create_run()
        self._approve()

    def _account(self, session, role):
        account = Account(email=f'{role}@test.local', role=role, password_hash=hash_password('pw-2611'))
        session.add(account)
        session.flush()
        return SimpleNamespace(id=account.id, email=account.email, role=account.role)

    def _create_run(self):
        with Session() as session:
            payload = RunCreate(label='回查流程用运行', dataset_id=self.dataset.id, settings={})
            info = runs.create_run(session, payload, self.analyst)
            return info['id']

    def _approve(self):
        with Session() as session:
            reviews.submit(session, self.run_id,
                           ReviewCreate(decision=Decision.APPROVED, note='首次签核，依据完整'), self.admin)

    def _withdraw(self, strip_id, reason='复核确认该历史控制条记错'):
        with Session() as session:
            return revisions.withdraw_strip(
                session, self.run_id, StripWithdrawCreate(strip_id=strip_id, reason=reason), self.admin)

    # 1. 撤条先暂停，原结果与原签核保留；批次无变化时自动恢复原签核。
    def test_withdraw_pauses_then_auto_restores_when_batches_unchanged(self):
        detail = self._withdraw(self.hist_fit)
        self.assertTrue(detail['effective'])
        self.assertTrue(detail['auto_restored'])
        self.assertEqual(detail['status'], RevisionStatus.ACTIVE)
        self.assertEqual(detail['excluded_strip_ids'], [self.hist_fit])
        self.assertEqual(len(detail['withdrawals']), 1)
        self.assertEqual(detail['original_review']['decision'], Decision.APPROVED)
        self.assertEqual(detail['revision_reviews'], [])

        with Session() as session:
            run = session.get(Run, self.run_id)
            revision = session.scalar(select(RunRevision).where(RunRevision.run_id == self.run_id))
            # 原结果与原签核原样保留，可随时翻查。
            self.assertIsNotNone(run.result)
            self.assertEqual(session.get(type(run), self.run_id).status, RunStatus.COMPLETED)
            self.assertEqual(revision.status, RevisionStatus.ACTIVE)
            self.assertFalse(revision.requires_review)
            self.assertEqual(session.query(StripWithdrawal).count(), 1)
            # 复算结果与原结果是两份独立快照。
            self.assertIsNot(revision.result, run.result)

    # 2. 批次发生变化（主条/档位/工位）时保持暂停，等待重新签核。
    def test_changed_batches_remain_suspended_until_re_review(self):
        changed_run = self._run_with_changed_batches()
        with Session() as session:
            revision = session.scalar(select(RunRevision).where(RunRevision.run_id == changed_run))
            self.assertEqual(revision.status, RevisionStatus.SUSPENDED)
            self.assertTrue(revision.requires_review)
            self.assertEqual(session.get(Run, changed_run).status, RunStatus.SUSPENDED)
            detail = revisions.revision_detail(session, changed_run, self.admin)
            self.assertTrue(detail['comparison']['changed_batches'])

            # 暂停期间下载正式回查单被拒绝。
            with self.assertRaises(Exception) as ctx:
                runs.export_table(session, changed_run, 'revisit_queue', self.admin)
            self.assertEqual(ctx.exception.status_code, 409)

            # viewer 在暂停期间看不到该运行。
            with self.assertRaises(LookupError):
                runs.get_run(session, changed_run, self.viewer)

            # 重新签核通过后恢复生效。
            result = revisions.submit_revision_review(
                session, changed_run,
                RevisionReviewCreate(decision=Decision.APPROVED, note='变化批次已重新核对'), self.admin)
            self.assertTrue(result['effective'])
            self.assertEqual(len(result['revision_reviews']), 1)
            self.assertEqual(session.get(Run, changed_run).status, RunStatus.COMPLETED)

        # 生效后 viewer 可见，且下载的是复算后的当前有效回查单。
        with Session() as session:
            run, _, revision = runs.get_run(session, changed_run, self.viewer)
            table = runs.export_table(session, changed_run, 'revisit_queue', self.viewer)
            self.assertEqual(len(table['rows']), len(revision.result['tables'][0]['rows']))

    # 3. 重新签核被退回时继续暂停，正式单仍不可下载。
    def test_rejected_re_review_keeps_suspended(self):
        changed_run = self._run_with_changed_batches()
        with Session() as session:
            revisions.submit_revision_review(
                session, changed_run,
                RevisionReviewCreate(decision=Decision.REJECTED, note='依据不足，退回重算'), self.admin)
            revision = session.scalar(select(RunRevision).where(RunRevision.run_id == changed_run))
            self.assertEqual(revision.status, RevisionStatus.SUSPENDED)
            self.assertEqual(session.get(Run, changed_run).status, RunStatus.SUSPENDED)
            with self.assertRaises(Exception) as ctx:
                runs.export_table(session, changed_run, 'revisit_queue', self.admin)
            self.assertEqual(ctx.exception.status_code, 409)

    # 4. 复算失败：继续暂停，重试不重复留下撤回记录；恢复后仍可重新签核。
    def test_failed_recalculation_retries_without_duplicate_withdrawal(self):
        # 一次撤掉 25 条校准记录，使校准组低于 24 条下限。
        cal_ids = [r['id'] for r in self.rows if r['split'] == 'reference_cal'][:25]
        with Session() as session:
            # 逐条撤到第 24 条仍可复算；第 25 条触发最小组数失败。
            for strip_id in cal_ids[:24]:
                revisions.withdraw_strip(
                    session, self.run_id,
                    StripWithdrawCreate(strip_id=strip_id, reason='连续剔除记错校准条'), self.admin)
            detail = revisions.withdraw_strip(
                session, self.run_id,
                StripWithdrawCreate(strip_id=cal_ids[24], reason='最后一条导致样本不足'), self.admin)
            self.assertEqual(detail['status'], RevisionStatus.SUSPENDED)
            self.assertIsNotNone(detail['error'])
            self.assertEqual(len(detail['withdrawals']), 25)

            # 重试仍失败：撤回记录数不变。
            retry = revisions.retry_recalculation(session, self.run_id, self.admin)
            self.assertEqual(retry['status'], RevisionStatus.SUSPENDED)
            self.assertIsNotNone(retry['error'])
            self.assertEqual(session.query(StripWithdrawal).count(), 25)

            # 无复算结果时不允许重新签核。
            with self.assertRaises(Exception) as ctx:
                revisions.submit_revision_review(
                    session, self.run_id,
                    RevisionReviewCreate(decision=Decision.APPROVED, note='不能签空单'), self.admin)
            self.assertEqual(ctx.exception.status_code, 409)

    # 5. 复算失败后重试：不重复留下撤回记录；复算恢复后按批次变化规则重新签核。
    def test_retry_recovers_after_transient_failure_without_duplicate_withdrawal(self):
        from unittest.mock import patch

        real_recalculate = revisions.recalculate
        with patch('src.services.revisions.recalculate',
                   side_effect=ValueError('模拟首次复算失败')):
            detail = self._withdraw(self.hist_cal, reason='首次复算遇到失败的撤回')
        self.assertEqual(detail['status'], RevisionStatus.SUSPENDED)
        self.assertIsNotNone(detail['error'])
        self.assertIsNone(detail['comparison'])
        self.assertEqual(len(detail['withdrawals']), 1)

        with Session() as session:
            # 重试使用真实复算，沿用同一份剔除清单，不新增撤回记录。
            retry = revisions.retry_recalculation(session, self.run_id, self.admin)
            self.assertIsNone(retry['error'])
            self.assertEqual(len(retry['withdrawals']), 1)
            self.assertEqual(session.query(StripWithdrawal).count(), 1)

            if retry['requires_review']:
                revisions.submit_revision_review(
                    session, self.run_id,
                    RevisionReviewCreate(decision=Decision.APPROVED, note='恢复后变化批次已重签'), self.admin)
            table = runs.export_table(session, self.run_id, 'revisit_queue', self.admin)
            self.assertTrue(table['rows'])
            revision = session.scalar(select(RunRevision).where(RunRevision.run_id == self.run_id))
            self.assertEqual(revision.status, RevisionStatus.ACTIVE)
            self.assertEqual(session.get(Run, self.run_id).status, RunStatus.COMPLETED)

    # 6. 不允许撤回当班样本，也不允许重复撤回同一条。
    def test_shift_and_duplicate_withdrawal_rejected(self):
        self._withdraw(self.hist_fit)
        with Session() as session:
            with self.assertRaises(ValueError):
                revisions.withdraw_strip(
                    session, self.run_id,
                    StripWithdrawCreate(strip_id=self.shift_strip, reason='试图剔除当班条'), self.admin)
            with self.assertRaises(Exception) as ctx:
                revisions.withdraw_strip(
                    session, self.run_id,
                    StripWithdrawCreate(strip_id=self.hist_fit, reason='重复撤回'), self.admin)
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(session.query(StripWithdrawal).count(), 1)

    # 7. 未签核的运行不允许撤条；非管理员无权限由路由层依赖保证，这里覆盖服务前置条件。
    def test_withdrawal_requires_formal_approved_sheet(self):
        with Session() as session:
            payload = RunCreate(label='未签核运行', dataset_id=self.dataset.id, settings={})
            fresh = runs.create_run(session, payload, self.analyst)
            with self.assertRaises(Exception) as ctx:
                revisions.withdraw_strip(
                    session, fresh['id'],
                    StripWithdrawCreate(strip_id=self.hist_fit, reason='无正式单先撤条'), self.admin)
            self.assertEqual(ctx.exception.status_code, 409)

    # 8. 并发撤条：两个事务并发提交，后提交者必须基于最新复算链，旧复算不得恢复正式单。
    def test_concurrent_withdrawals_serialize_and_never_restore_stale_sheet(self):
        second_fit = next(r['id'] for r in self.rows
                          if r['split'] == 'reference_fit' and r['id'] != self.hist_fit)
        engine.dispose()
        session_a = Session()
        session_b = Session()
        try:
            revisions.acquire_write_lock(session_a, self.run_id)
            run_a, review_a, revision_a = revisions.load_context(session_a, self.run_id)
            revisions.ensure_formal_sheet(run_a, review_a)

            # B 在 A 持锁期间尝试加写锁，应被阻塞或拒绝，而不是覆盖 A 的复算。
            timed_out = False
            try:
                session_b.execute(text('UPDATE analysis_runs SET status = status WHERE id = :i'),
                                  {'i': self.run_id})
                session_b.commit()
            except Exception as exc:
                timed_out = 'locked' in str(exc).lower() or 'busy' in str(exc).lower()
                session_b.rollback()
            self.assertTrue(timed_out, 'SQLite 写锁应阻止第二个并发写事务')

            # A 完成撤条与复算并提交。
            session_a.add(StripWithdrawal(
                run_id=self.run_id, strip_id=self.hist_fit, split='历史拟合',
                reason='并发事务 A', reviewer_id=self.admin.id, created_at='2026-11-27T00:00:00+00:00'))
            session_a.flush()
            revisions.apply_recalculation(session_a, run_a, revision_a, [self.hist_fit])
        finally:
            session_a.close()
            session_b.close()

        # B 之后重试时必须读取到 A 的复算链，在其剔除清单上继续，而不是恢复旧正式单。
        with Session() as session:
            detail = revisions.withdraw_strip(
                session, self.run_id,
                StripWithdrawCreate(strip_id=second_fit, reason='并发事务 B 串行重试'), self.admin)
            self.assertEqual(set(detail['excluded_strip_ids']), {self.hist_fit, second_fit})
            self.assertEqual(len(detail['withdrawals']), 2)
            revision = session.scalar(select(RunRevision).where(RunRevision.run_id == self.run_id))
            self.assertEqual(revision.lock_version, 2)

    def _run_with_changed_batches(self):
        """构造一个复算后批次口径发生变化的运行并撤条，返回该运行 id。"""
        with Session() as session:
            payload = RunCreate(label='批次变化用运行', dataset_id=self.dataset.id, settings={})
            info = runs.create_run(session, payload, self.analyst)
            run_id = info['id']
            reviews.submit(session, run_id,
                           ReviewCreate(decision=Decision.APPROVED, note='变化剧本首次签核'), self.admin)

        with Session() as session:
            revisions.withdraw_strip(
                session, run_id,
                StripWithdrawCreate(strip_id=self.changed_candidate,
                                    reason='该条记错导致批次口径变化'), self.admin)
        return run_id


if __name__ == '__main__':
    unittest.main(verbosity=2)
