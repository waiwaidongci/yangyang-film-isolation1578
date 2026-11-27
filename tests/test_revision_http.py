"""错条撤回工作流的 HTTP 端到端冒烟测试（FastAPI TestClient）。"""

import os
import tempfile

_DB_FD, _DB_PATH = tempfile.mkstemp(prefix='film-http-', suffix='.db')
os.environ['DATABASE_URL'] = f'sqlite:///{_DB_PATH}'
os.environ['SEED_DEMO'] = 'true'
os.environ['JWT_SECRET'] = 'http-test-secret'

import unittest  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from migrations.bootstrap import bootstrap  # noqa: E402
from scripts.seed_demo import seed  # noqa: E402
from src.main import app  # noqa: E402


class RevisionHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bootstrap()
        seed()
        cls.client = TestClient(app)
        cls.tokens = {}
        for role in ('analyst', 'admin', 'viewer'):
            resp = cls.client.post('/api/auth/login',
                                   json={'email': f'{role}@demo.local', 'password': 'Demo-2611!'})
            assert resp.status_code == 200, resp.text
            cls.tokens[role] = resp.json()['access_token']

    def _headers(self, role):
        return {'Authorization': f'Bearer {self.tokens[role]}'}

    def test_full_withdrawal_lifecycle_over_http(self):
        # 演示台账已由 seed 装载；分析员新建运行。
        datasets = self.client.get('/api/datasets', headers=self._headers('analyst')).json()
        dataset_id = datasets[0]['id']
        run = self.client.post('/api/runs', headers=self._headers('analyst'),
                               json={'label': 'HTTP 撤条流程运行', 'dataset_id': dataset_id,
                                     'settings': {}})
        self.assertEqual(run.status_code, 201, run.text)
        run_id = run.json()['id']

        # 管理员首次签核。
        review = self.client.post(f'/api/runs/{run_id}/review', headers=self._headers('admin'),
                                  json={'decision': 'approved', 'note': 'HTTP 首次签核依据完整'})
        self.assertEqual(review.status_code, 201, review.text)

        # viewer 此刻可以下载。
        export = self.client.get(f'/api/runs/{run_id}/export/revisit_queue',
                                 headers=self._headers('viewer'))
        self.assertEqual(export.status_code, 200)
        before_lines = len(export.text.strip().splitlines())

        # 找一条历史拟合控制条编号。
        detail = self.client.get(f'/api/runs/{run_id}', headers=self._headers('admin')).json()
        audit = next(t for t in detail['result']['tables'] if t['key'] == 'reference_audit')
        self.assertTrue(audit['rows'])

        # 通过台账明细找历史条编号。
        ds_detail = self.client.get(f'/api/datasets/{dataset_id}?limit=200',
                                    headers=self._headers('admin')).json()
        strip_id = next(r['id'] for r in ds_detail['rows'] if r['split'] == 'reference_fit')

        # 非管理员撤条被拒绝。
        forbidden = self.client.post(f'/api/runs/{run_id}/withdrawals',
                                     headers=self._headers('analyst'),
                                     json={'strip_id': strip_id, 'reason': '越权撤条不应通过'})
        self.assertEqual(forbidden.status_code, 403)

        # 管理员撤条。
        withdrawn = self.client.post(f'/api/runs/{run_id}/withdrawals',
                                     headers=self._headers('admin'),
                                     json={'strip_id': strip_id, 'reason': '复核确认该历史条记错'})
        self.assertEqual(withdrawn.status_code, 201, withdrawn.text)
        payload = withdrawn.json()
        self.assertEqual(payload['excluded_strip_ids'], [strip_id])
        self.assertEqual(len(payload['withdrawals']), 1)

        # 重复撤条返回 409。
        duplicate = self.client.post(f'/api/runs/{run_id}/withdrawals',
                                     headers=self._headers('admin'),
                                     json={'strip_id': strip_id, 'reason': '重复撤回'})
        self.assertEqual(duplicate.status_code, 409)

        # 运行详情暴露复算链，且非 viewer 可看到 original_result。
        detail = self.client.get(f'/api/runs/{run_id}', headers=self._headers('admin')).json()
        self.assertIn('revision', detail)
        self.assertIn('original_result', detail)
        self.assertEqual(detail['revision']['excluded_strip_ids'], [strip_id])

        # 复算链独立端点。
        chain = self.client.get(f'/api/runs/{run_id}/revision', headers=self._headers('admin'))
        self.assertEqual(chain.status_code, 200)
        self.assertEqual(chain.json()['original_review']['decision'], 'approved')

        if detail['revision']['status'] == 'suspended':
            # 批次变化：viewer 暂停期间不可见、下载 409，重签后恢复。
            hidden = self.client.get(f'/api/runs/{run_id}', headers=self._headers('viewer'))
            self.assertEqual(hidden.status_code, 404)
            blocked = self.client.get(f'/api/runs/{run_id}/export/revisit_queue',
                                      headers=self._headers('viewer'))
            self.assertEqual(blocked.status_code, 409)

            re_review = self.client.post(f'/api/runs/{run_id}/revision/review',
                                         headers=self._headers('admin'),
                                         json={'decision': 'approved', 'note': '变化批次已重新签核'})
            self.assertEqual(re_review.status_code, 201, re_review.text)

        # 生效后 viewer 可见，下载的是当前有效回查单（原签核与撤回历史仍在复算链里可查）。
        visible = self.client.get(f'/api/runs/{run_id}', headers=self._headers('viewer'))
        self.assertEqual(visible.status_code, 200)
        self.assertNotIn('original_result', visible.json())
        export_after = self.client.get(f'/api/runs/{run_id}/export/revisit_queue',
                                       headers=self._headers('viewer'))
        self.assertEqual(export_after.status_code, 200)
        self.assertIn('change_id', export_after.text)
        self.assertEqual(len(export_after.text.strip().splitlines()), before_lines)

        # OpenAPI 已登记新端点。
        spec = self.client.get('/api/openapi.json').json()
        self.assertIn('/api/runs/{run_id}/withdrawals', spec['paths'])
        self.assertIn('/api/runs/{run_id}/revision/retry', spec['paths'])
        self.assertIn('/api/runs/{run_id}/revision/review', spec['paths'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
