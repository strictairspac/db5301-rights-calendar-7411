import os
import tempfile

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False).name
os.environ.setdefault('AUTH_KEY', 'integration-test-key')
os.environ['DB_DRIVER'] = 'sqlite'
os.environ['SQLITE_URL'] = f'sqlite:///{_DB_FILE}'
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6399/0')

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from src.config.settings import engine
from src.models.tables import audit, cases, evidence, runs, schema, users
from src.models.upgrade import ensure_schema
from src.services.security import hash_password
from src.constants.workflow import Role


if os.environ.get('DB_DRIVER', '').startswith('sqlite'):
    @event.listens_for(engine, 'connect')
    def _sqlite_pragmas(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute('PRAGMA journal_mode=WAL')
        cursor.execute('PRAGMA busy_timeout=30000')
        cursor.close()


@pytest.fixture()
def client():
    schema.drop_all(engine)
    schema.create_all(engine)
    ensure_schema(engine)
    with engine.begin() as conn:
        for role in Role:
            conn.execute(users.insert().values(username=role.value, role=role.value,
                             password_hash=hash_password('Demo-Walkthrough-2026')))
    from src.main import app
    with TestClient(app) as test_client:
        # The lifespan seeder adds a demo case; remove it so tests start clean.
        with engine.begin() as conn:
            conn.execute(audit.delete())
            conn.execute(cases.delete())
        yield test_client


@pytest.fixture()
def tokens(client):
    def login(username):
        response = client.post('/login', json={'username': username,
                                               'password': 'Demo-Walkthrough-2026'})
        assert response.status_code == 200
        return response.json()['token']
    return {role.value: login(role.value) for role in Role}


def auth(tokens, role):
    return {'Authorization': f"Bearer {tokens[role]}"}


@pytest.fixture()
def sample_payload():
    from src.services.domain import SPEC
    return SPEC['sample']


def make_case(client, tokens, payload, title='清理测试档案'):
    response = client.post('/cases', json={'title': title, 'payload': payload},
                           headers=auth(tokens, 'operator'))
    assert response.status_code == 201, response.text
    return response.json()


def insert_evidence(case_id, *, signed=False, note='同意发布'):
    """Simulate a worker conclusion, optionally already signed."""
    from src.utils.time import utc_time
    report = {'summary': f'结论-{case_id}', 'metrics': {'排片场次': 1}, 'sections': []}
    import hashlib
    from src.utils.encoding import canonical
    digest = hashlib.sha256(canonical(report)).hexdigest()
    with engine.begin() as conn:
        run_id = conn.execute(runs.insert().values(case_id=case_id, status='complete',
                              owner='operator', created_at=utc_time(),
                              finished_at=utc_time())).inserted_primary_key[0]
        evidence_id = conn.execute(evidence.insert().values(
            run_id=run_id, report=report, digest=digest,
            reviewer='reviewer' if signed else None,
            review_note=note if signed else None,
            reviewed_at=utc_time() if signed else None)).inserted_primary_key[0]
        return evidence_id, run_id, report, digest
