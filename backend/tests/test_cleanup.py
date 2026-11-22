import pytest
from sqlalchemy import func, select

from src.config.settings import engine
from src.constants.workflow import Status
from src.models.tables import audit, cases, evidence, runs
from src.services import workflow
from src.services.workflow import CLEANED_PAYLOAD, STALE_RUN_ERROR

from conftest import auth, make_case, insert_evidence


def count(conn, table, *conditions):
    return conn.execute(select(func.count()).select_from(table).where(*conditions)).scalar_one()


def test_cleanup_removes_payload_and_unsigned_evidence_keeps_signed(client, tokens, sample_payload):
    case = make_case(client, tokens, sample_payload)
    signed_id, signed_run, signed_report, signed_digest = insert_evidence(case['id'], signed=True)
    unsigned_id, unsigned_run, _, _ = insert_evidence(case['id'], signed=False)

    response = client.post(f"/cases/{case['id']}/cleanup", headers=auth(tokens, 'operator'))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['cleaned_at']
    assert body['payload'] == CLEANED_PAYLOAD

    with engine.begin() as conn:
        stored_case = conn.execute(select(cases).where(cases.c.id == case['id'])).mappings().one()
        assert stored_case['payload'] == CLEANED_PAYLOAD
        assert conn.execute(select(evidence).where(evidence.c.id == unsigned_id)).first() is None
        kept = conn.execute(select(evidence).where(evidence.c.id == signed_id)).mappings().one()
        assert kept['report'] == signed_report
        assert kept['digest'] == signed_digest
        assert kept['reviewer'] == 'reviewer'
        assert kept['reviewed_at'] is not None
        action = conn.execute(select(audit).where(audit.c.action == 'cleanup')).mappings().all()
        assert len(action) == 1 and action[0]['reference'] == str(case['id'])


def test_signed_evidence_still_fetchable_by_original_id_and_download(client, tokens, sample_payload):
    case = make_case(client, tokens, sample_payload)
    signed_id, _, _, _ = insert_evidence(case['id'], signed=True)
    client.post(f"/cases/{case['id']}/cleanup", headers=auth(tokens, 'operator'))

    listed = client.get('/evidence', headers=auth(tokens, 'viewer')).json()
    assert [row['id'] for row in listed] == [signed_id]
    detail = client.get('/evidence', headers=auth(tokens, 'viewer')).json()[0]
    assert detail['reviewer'] == 'reviewer'

    download = client.get(f'/evidence/{signed_id}/download', headers=auth(tokens, 'viewer'))
    assert download.status_code == 200
    assert download.headers['content-disposition'] == f'attachment; filename="evidence-{signed_id}.json"'
    import json
    exported = json.loads(download.content)
    assert exported['id'] == signed_id
    assert exported['report']['summary'].startswith('结论-')
    assert exported['reviewer'] == 'reviewer'

    # Only the signed row remains; the unsigned one was purged by cleanup.
    remaining = client.get('/evidence', headers=auth(tokens, 'viewer')).json()
    assert [row['id'] for row in remaining] == [signed_id]


def test_cleanup_is_idempotent_no_duplicate_archive_or_audit(client, tokens, sample_payload):
    case = make_case(client, tokens, sample_payload)
    insert_evidence(case['id'], signed=False)

    first = client.post(f"/cases/{case['id']}/cleanup", headers=auth(tokens, 'operator'))
    first_at = first.json()['cleaned_at']
    second = client.post(f"/cases/{case['id']}/cleanup", headers=auth(tokens, 'operator'))
    assert second.status_code == 200
    assert second.json()['cleaned_at'] == first_at

    with engine.begin() as conn:
        assert count(conn, audit, audit.c.action == 'cleanup') == 1
        assert count(conn, evidence) == 0


def test_cleanup_failure_keeps_original_materials(client, tokens, sample_payload, monkeypatch):
    case = make_case(client, tokens, sample_payload)
    evidence_id, _, _, _ = insert_evidence(case['id'], signed=False)
    original_payload = dict(case['payload'])

    def boom(*args, **kwargs):
        raise RuntimeError('simulated database outage')

    monkeypatch.setattr(workflow, 'log', boom)

    with pytest.raises(RuntimeError):
        workflow.cleanup(case['id'], 'operator')

    with engine.begin() as conn:
        stored = conn.execute(select(cases).where(cases.c.id == case['id'])).mappings().one()
        assert stored['cleaned_at'] is None
        assert stored['payload'] == original_payload
        assert count(conn, evidence, evidence.c.id == evidence_id) == 1


def test_cannot_enqueue_run_after_cleanup(client, tokens, sample_payload):
    case = make_case(client, tokens, sample_payload)
    client.post(f"/cases/{case['id']}/cleanup", headers=auth(tokens, 'operator'))
    response = client.post(f"/cases/{case['id']}/runs", headers=auth(tokens, 'operator'))
    assert response.status_code == 409
    assert '清理' in response.json()['detail']


def test_queued_run_claimed_after_cleanup_fails_without_evidence(client, tokens, sample_payload):
    case = make_case(client, tokens, sample_payload)
    queued = client.post(f"/cases/{case['id']}/runs", headers=auth(tokens, 'operator')).json()
    client.post(f"/cases/{case['id']}/cleanup", headers=auth(tokens, 'operator'))

    assert workflow.claim() is None
    with engine.begin() as conn:
        run = conn.execute(select(runs).where(runs.c.id == queued['id'])).mappings().one()
        assert run['status'] == Status.FAILED
        assert run['error'] == STALE_RUN_ERROR
        assert count(conn, evidence) == 0


def test_late_finish_after_cleanup_does_not_store_conclusion(client, tokens, sample_payload):
    case = make_case(client, tokens, sample_payload)
    queued = client.post(f"/cases/{case['id']}/runs", headers=auth(tokens, 'operator')).json()
    # Worker claims before cleanup: it is mid-flight when the cleanup commits.
    run_id, token, _ = workflow.claim()
    assert run_id == queued['id']
    client.post(f"/cases/{case['id']}/cleanup", headers=auth(tokens, 'operator'))

    late_report = {'summary': '晚到的结论', 'metrics': {}, 'sections': []}
    workflow.finish(run_id, token, result=late_report)

    with engine.begin() as conn:
        run = conn.execute(select(runs).where(runs.c.id == run_id)).mappings().one()
        assert run['status'] == Status.FAILED
        assert run['error'] == STALE_RUN_ERROR
        assert count(conn, evidence) == 0


def test_finish_before_cleanup_stores_then_unsigned_report_is_purged(client, tokens, sample_payload):
    case = make_case(client, tokens, sample_payload)
    client.post(f"/cases/{case['id']}/runs", headers=auth(tokens, 'operator'))
    run_id, token, _ = workflow.claim()
    workflow.finish(run_id, token, result={'summary': '正常结论', 'metrics': {}, 'sections': []})
    with engine.begin() as conn:
        assert count(conn, evidence) == 1

    client.post(f"/cases/{case['id']}/cleanup", headers=auth(tokens, 'operator'))
    with engine.begin() as conn:
        assert count(conn, evidence) == 0


def test_cleanup_does_not_wait_for_running_check(client, tokens, sample_payload):
    case = make_case(client, tokens, sample_payload)
    client.post(f"/cases/{case['id']}/runs", headers=auth(tokens, 'operator'))
    run_id, token, _ = workflow.claim()  # status becomes running
    response = client.post(f"/cases/{case['id']}/cleanup", headers=auth(tokens, 'operator'))
    assert response.status_code == 200
    with engine.begin() as conn:
        still_running = conn.execute(select(runs).where(runs.c.id == run_id)).mappings().one()
        assert still_running['status'] == Status.RUNNING
        assert conn.execute(select(cases).where(cases.c.id == case['id'])).mappings().one()['cleaned_at']


def test_permissions_for_cleanup(client, tokens, sample_payload):
    case = make_case(client, tokens, sample_payload)
    for role in ('reviewer', 'viewer'):
        response = client.post(f"/cases/{case['id']}/cleanup", headers=auth(tokens, role))
        assert response.status_code == 403
    anon = client.post(f"/cases/{case['id']}/cleanup")
    assert anon.status_code == 401
    with engine.begin() as conn:
        assert conn.execute(select(cases).where(cases.c.id == case['id'])).mappings().one()['cleaned_at'] is None


def test_cleanup_unknown_case_returns_404(client, tokens):
    response = client.post('/cases/9999/cleanup', headers=auth(tokens, 'operator'))
    assert response.status_code == 404


def test_review_then_cleanup_signing_survives(client, tokens, sample_payload):
    case = make_case(client, tokens, sample_payload)
    evidence_id, _, _, _ = insert_evidence(case['id'], signed=False)
    review = client.post(f'/evidence/{evidence_id}/review',
                         json={'note': '准予发布'}, headers=auth(tokens, 'reviewer'))
    assert review.status_code == 200
    client.post(f"/cases/{case['id']}/cleanup", headers=auth(tokens, 'operator'))
    with engine.begin() as conn:
        kept = conn.execute(select(evidence).where(evidence.c.id == evidence_id)).mappings().one()
        assert kept['review_note'] == '准予发布'
        assert kept['reviewer'] == 'reviewer'


def test_cleanup_then_concurrent_review_request_does_not_resurrect_row(client, tokens, sample_payload):
    case = make_case(client, tokens, sample_payload)
    evidence_id, _, _, _ = insert_evidence(case['id'], signed=False)
    client.post(f"/cases/{case['id']}/cleanup", headers=auth(tokens, 'operator'))
    response = client.post(f'/evidence/{evidence_id}/review',
                           json={'note': '晚到签署'}, headers=auth(tokens, 'reviewer'))
    assert response.status_code == 404
    with engine.begin() as conn:
        assert count(conn, evidence, evidence.c.id == evidence_id) == 0


def test_double_review_still_conflicts(client, tokens, sample_payload):
    case = make_case(client, tokens, sample_payload)
    evidence_id, _, _, _ = insert_evidence(case['id'], signed=True, note='首签')
    response = client.post(f'/evidence/{evidence_id}/review',
                           json={'note': '重复签署'}, headers=auth(tokens, 'reviewer'))
    assert response.status_code == 409


def test_cases_and_evidence_lists_expose_cleaned_state(client, tokens, sample_payload):
    case = make_case(client, tokens, sample_payload)
    client.post(f"/cases/{case['id']}/cleanup", headers=auth(tokens, 'operator'))
    listed = client.get('/cases', headers=auth(tokens, 'viewer')).json()
    assert listed[0]['cleaned_at']
    assert listed[0]['payload'] == CLEANED_PAYLOAD


def test_concurrent_review_and_cleanup_never_loses_a_committed_signature(client, tokens, sample_payload):
    import threading
    from fastapi import HTTPException

    case = make_case(client, tokens, sample_payload)
    evidence_id, _, _, _ = insert_evidence(case['id'], signed=False)
    barrier = threading.Barrier(2)
    review_error = []

    def sign():
        try:
            barrier.wait()
            workflow.review(evidence_id, '并发签署意见', 'reviewer')
        except HTTPException as exc:
            review_error.append(exc.status_code)

    def clean():
        barrier.wait()
        workflow.cleanup(case['id'], 'operator')

    signer = threading.Thread(target=sign)
    cleaner = threading.Thread(target=clean)
    signer.start(); cleaner.start(); signer.join(); cleaner.join()

    with engine.begin() as conn:
        row = conn.execute(select(evidence).where(evidence.c.id == evidence_id)).mappings().first()
        stored_case = conn.execute(select(cases).where(cases.c.id == case['id'])).mappings().one()
    if row is not None:
        # Review committed first: the signed report must survive the cleanup.
        assert row['reviewer'] == 'reviewer'
        assert row['review_note'] == '并发签署意见'
        assert review_error == []
    else:
        # Cleanup committed first: the unsigned row is gone and the late review is a 404.
        assert review_error == [404]
    assert stored_case['cleaned_at'] is not None
    assert stored_case['payload'] == CLEANED_PAYLOAD
