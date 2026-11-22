import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from src.config.settings import cache, engine
from src.constants.workflow import Status
from src.models.tables import audit, cases, evidence, runs
from src.services import domain
from src.utils.encoding import canonical
from src.utils.time import utc_time

# Marker payload kept after expiry cleanup: raw screenings/rights are gone,
# while the case row survives so signed evidence stays traceable by its id.
CLEANED_PAYLOAD = {'cleaned': True}
# Runs claimed after cleanup are failed with this message instead of being solved.
STALE_RUN_ERROR = '档案已到期清理，输入材料已清除，本次校核不再产出证据。'


def log(conn, actor, action, reference):
    conn.execute(audit.insert().values(actor=actor, action=action, reference=str(reference), created_at=utc_time()))


def recent(table):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(table).order_by(table.c.id.desc()).limit(100)).mappings()]


def found(conn, table, identity):
    row = conn.execute(select(table).where(table.c.id == identity)).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该记录')
    return dict(row)


def make_case(title, payload, actor):
    title = title.strip()
    if not title:
        raise HTTPException(422, '档案名称不能为空')
    try:
        if len(canonical(payload)) > 512 * 1024:
            raise ValueError('业务输入超过 512 KiB')
        if callable(getattr(domain, 'validate', None)):
            domain.validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None
    with engine.begin() as conn:
        case_id = conn.execute(cases.insert().values(slug=uuid.uuid4().hex, title=title, payload=payload,
                               author=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'create_case', case_id)
        return found(conn, cases, case_id)


def enqueue(case_id, actor):
    with engine.begin() as conn:
        case = found(conn, cases, case_id)
        if case.get('cleaned_at'):
            raise HTTPException(409, '档案已到期清理，不能再发起校核')
        run_id = conn.execute(runs.insert().values(case_id=case_id, status=Status.QUEUED,
                              owner=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'enqueue', run_id)
        result = found(conn, runs, run_id)
    try:
        cache.lpush('worker:wake', 'queued')
        cache.ltrim('worker:wake', 0, 99)
    except Exception:
        # The durable database queue is polled even if Redis is temporarily unavailable.
        pass
    return result


def claim():
    ready = or_(runs.c.status == Status.QUEUED,
                and_(runs.c.status == Status.RUNNING, runs.c.started_at < utc_time(-600)))
    with engine.begin() as conn:
        run = conn.execute(select(runs.c.id, runs.c.case_id).where(ready).order_by(runs.c.id)
                           .limit(1).with_for_update(skip_locked=True)).first()
        if not run:
            return None
        case = found(conn, cases, run.case_id)
        token = str(uuid.uuid4())
        conn.execute(runs.update().where(runs.c.id == run.id).values(
            started_at=utc_time(), status=Status.RUNNING, attempt_token=token))
        if case.get('cleaned_at'):
            # The case was cleaned while this run was queued (or its worker died).
            # Fail it inside this transaction so no late conclusion is ever stored.
            conn.execute(runs.update().where(runs.c.id == run.id).values(
                status=Status.FAILED, finished_at=utc_time(), error=STALE_RUN_ERROR))
            log(conn, 'worker', Status.FAILED.value, run.id)
            return None
        return run.id, token, case['payload']


def finish(run_id, token, result=None, error=None):
    digest = hashlib.sha256(canonical(result)).hexdigest() if result is not None else None
    status = Status.FAILED if error else Status.COMPLETE
    with engine.begin() as conn:
        count = conn.execute(runs.update().where(runs.c.id == run_id,
                        runs.c.attempt_token == token, runs.c.status == Status.RUNNING)
                        .values(status=status, finished_at=utc_time(), error=error)).rowcount
        if not count:
            return
        case = conn.execute(select(cases).select_from(cases.join(
            runs, runs.c.case_id == cases.c.id)).where(runs.c.id == run_id)).mappings().first()
        if result is not None and not (case and case.get('cleaned_at')):
            conn.execute(evidence.insert().values(run_id=run_id, report=result, digest=digest))
        elif result is not None:
            # A late conclusion after expiry cleanup: mark the run failed and keep
            # the materials out of the database instead of re-archiving them.
            conn.execute(runs.update().where(runs.c.id == run_id).values(
                status=Status.FAILED, error=STALE_RUN_ERROR))
            status = Status.FAILED
        log(conn, 'worker', status.value, run_id)


def review(identity, note, actor):
    note = note.strip()
    if not note:
        raise HTTPException(422, '复核意见不能为空')
    with engine.begin() as conn:
        row = conn.execute(select(evidence).where(evidence.c.id == identity)
                           .with_for_update()).mappings().first()
        if row is None:
            raise HTTPException(404, '找不到该记录')
        if row['reviewed_at'] is not None:
            raise HTTPException(409, '该证据已经复核')
        conn.execute(evidence.update().where(evidence.c.id == identity).values(
            review_note=note, reviewer=actor, reviewed_at=utc_time()))
        log(conn, actor, 'review', identity)
        return found(conn, evidence, identity)


def cleanup(case_id, actor):
    """Expire a case: purge raw inputs and unsigned evidence in one transaction.

    Signed evidence, its original id/report/digest and review metadata are kept.
    Idempotent: a repeated call returns the already-cleaned case without writing
    another audit row or touching any material again.
    """
    with engine.begin() as conn:
        case = conn.execute(select(cases).where(cases.c.id == case_id)
                            .with_for_update()).mappings().first()
        if case is None:
            raise HTTPException(404, '找不到该记录')
        if case['cleaned_at'] is not None:
            return dict(case)
        run_ids = select(runs.c.id).where(runs.c.case_id == case_id)
        # Signed rows are locked by the same row locks that review takes, so a
        # signing that commits first survives; an unsigned row is deleted.
        conn.execute(evidence.delete().where(evidence.c.run_id.in_(run_ids),
                                             evidence.c.reviewed_at.is_(None)))
        cleaned_at = utc_time()
        conn.execute(cases.update().where(cases.c.id == case_id).values(
            payload=CLEANED_PAYLOAD, cleaned_at=cleaned_at))
        log(conn, actor, 'cleanup', case_id)
        return found(conn, cases, case_id)
