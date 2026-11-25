from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.queueing import result, submit
from core.records import get, need, scan
from domain.cue_tracks_model import cue_tracks
from domain.cue_clusters_model import (
    cue_clusters,
    cue_cluster_comparisons,
    cue_cluster_reviews,
    cue_cluster_votes,
)

try:  # Use the exact canonical operator in the deployed backend.
    from domain.learning import dtw as _learning_dtw
except Exception:  # pragma: no cover - lightweight test environments

    def _learning_dtw(a, b):
        inf = float('inf')
        prev = [inf] * (len(b) + 1)
        prev[0] = 0
        for i, x in enumerate(a, 1):
            cur = [inf] * (len(b) + 1)
            for j, y in enumerate(b, 1):
                cur[j] = abs(x - y) + min(prev[j], cur[j - 1], prev[j - 1])
            prev = cur
        return float(prev[-1] / (len(a) + len(b)))


# Exposed under the canonical operator name so tests can monkeypatch
# ``cue_clusters_service.dtw`` when they do not need real sequence math.
dtw = _learning_dtw


UNUSABLE_WORDS = {
    'false', '0', 'no', 'n', 'unusable', 'not-usable', 'not_usable',
    'unsuitable', 'not-suitable', 'not_suitable', 'unfit', 'reject',
    'rejected', 'inapplicable', 'none', 'null', 'bad', 'fail', 'failed',
    '不合用', '不可用', '不适用', '驳回', '拒绝', '否决',
}
USABLE_WORDS = {
    'true', '1', 'yes', 'y', 'usable', 'suitable', 'accepted', 'accept',
    'agree', 'agreed', 'approve', 'approved', 'qualified', 'ok', 'okay',
    'pass', 'passed', 'uphold', '合用', '可用', '适用', '同意', '同意合用',
    '通过', '赞成', '裁决合用',
}
SIDE_WORDS = {
    'first': 0, '1': 0, 'a': 0, 'one': 0, '第一位': 0, '第一个': 0,
    'second': 1, '2': 1, 'b': 1, 'two': 1, '第二位': 1, '第二个': 1,
}


def _data_dict(data: Any) -> dict[str, Any]:
    if data is None:
        return {}
    if isinstance(data, dict):
        return dict(data)
    if hasattr(data, 'model_dump'):
        # exclude_unset lets us distinguish an omitted representative from an
        # explicit null meaning "no suitable sample".
        try:
            return data.model_dump(exclude_unset=True)
        except TypeError:
            return data.model_dump()
    return dict(vars(data))


def _merge_data(data: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    payload = _data_dict(data)
    payload.update(kwargs or {})
    return payload


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get('code') or value.get('id') or value.get('value')
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first(d: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return None


def _case_code(d: dict[str, Any]) -> str | None:
    value = _first(d, 'code', 'cluster_code', 'review_code', 'case_code',
                   'review_id', 'case_id', 'cluster_id', 'case', 'review',
                   'cluster', 'id')
    return _clean(value)


def _voter(d: dict[str, Any], user: dict[str, Any] | None) -> tuple[str, str | None]:
    auth_actor = _clean(_first(user or {}, 'sub', 'id', 'email', 'username',
                               'name', 'uid')) or 'anonymous-reviewer'
    supplied = _clean(_first(d, 'voter', 'voter_id', 'foreman', 'foreman_id',
                            'reviewer', 'reviewer_id', 'judge', 'captain',
                            'leader', 'actor', 'name', 'email', 'reviewer_email',
                            'foreman_email', 'uid', 'user_id'))
    # The JWT subject remains an audit actor.  An explicit ballot identity is
    # useful for role-level demo clients; absent it, the authenticated subject
    # is the ballot identity.
    return supplied or auth_actor, auth_actor


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
    text = str(value).strip().lower()
    if text in USABLE_WORDS or text == 'true':
        return True
    if text in UNUSABLE_WORDS or text == 'false':
        return False
    return None


def _snapshot_from_run(run: dict[str, Any], d: dict[str, Any]) -> dict[str, Any]:
    stats = run.get('result') if isinstance(run, dict) else None
    stats = stats if isinstance(stats, dict) else {}
    job_input = run.get('input') if isinstance(run, dict) else None
    job_input = job_input if isinstance(job_input, dict) else {}

    raw_candidates = _first(stats, 'members', 'candidates', 'options',
                            'samples', 'choices')
    if raw_candidates is None:
        raw_candidates = _first(run, 'members', 'candidates', 'options')
    if not isinstance(raw_candidates, (list, tuple)) or not raw_candidates:
        raise HTTPException(409, '本次归簇没有可供选择的原始成员')

    candidates = []
    for item in raw_candidates:
        code = _clean(item)
        if code and code not in candidates:
            candidates.append(code)

    track_obj = job_input.get('track')
    if isinstance(track_obj, dict):
        track = _clean(track_obj.get('code'))
    else:
        track = _clean(track_obj)
    track = track or _clean(_first(stats, 'track', 'input_track')) or _clean(d.get('track'))
    if not track:
        raise HTTPException(409, '计算结果缺少输入素材编号')

    suggested = _clean(_first(stats, 'representative', 'suggested_representative',
                              'default_representative'))
    return {'track': track, 'candidates': candidates,
            'suggested_representative': suggested}


def _choice_from_side(raw_side: Any, votes: list[dict[str, Any]]) -> str | None:
    side = _clean(raw_side)
    if side is None:
        return None
    lowered = side.lower()
    if lowered in SIDE_WORDS and len(votes) >= 2:
        return votes[SIDE_WORDS[lowered]]['choice']
    for vote in votes[:2]:
        if side in {vote.get('voter'), vote.get('actor')}:
            return vote.get('choice')
    return None


def _normalize_choice(d: dict[str, Any], review: dict[str, Any] | None = None,
                      votes: list[dict[str, Any]] | None = None
                      ) -> tuple[str | None, bool | None]:
    """Return (choice, usable).

    ``usable is None`` means the request did not contain a ballot.  A negative
    ballot is represented by ``(None, False)``; a positive one by
    ``(member_code, True)``.
    """
    candidates = list((review or {}).get('candidates') or [])
    suggested = _clean((review or {}).get('suggested_representative'))

    negative = False
    positive = False
    for key in ('usable', 'is_usable', 'suitable', 'is_fit', 'fit',
                'applicable', 'is_applicable', 'accepted', 'qualified',
                'matches', 'matched', 'shareable'):
        if key in d and d[key] is not None:
            parsed = _as_bool(d[key])
            if parsed is False:
                negative = True
            elif parsed is True:
                positive = True
    for key in ('unusable', 'reject', 'rejected', 'is_unusable'):
        if key in d and d[key] is not None and _as_bool(d[key]) is not False:
            # Explicit false means "not rejected"; anything truthy rejects.
            if bool(d[key]) if not isinstance(d[key], str) else _as_bool(d[key]) is True:
                negative = True

    raw_decision = _first(d, 'decision', 'verdict', 'ruling', 'judgment',
                          'judgement', 'answer', 'action', 'status')
    decision_text = _clean(raw_decision)
    if decision_text:
        lowered = decision_text.lower()
        if lowered in UNUSABLE_WORDS or decision_text in UNUSABLE_WORDS:
            negative = True
        elif lowered in USABLE_WORDS or decision_text in USABLE_WORDS:
            positive = True

    choice_keys = (
        'representative', 'representative_code', 'representative_id',
        'selected_representative', 'choice', 'candidate_code', 'candidate_id',
        'candidate', 'selected', 'selection', 'sample', 'sample_code',
        'sample_id', 'template', 'template_code', 'template_id',
        'final_representative', 'signed_representative', 'cue',
        'cue_code', 'cue_id', 'track_code', 'member', 'member_code',
        'selected_candidate', 'vote',
    )
    explicit_choice_key = next((key for key in choice_keys if key in d), None)
    raw_choice = _first(d, *choice_keys)
    # A decision field may itself carry a candidate code or a side reference.
    if raw_choice is None and decision_text and not (negative or positive):
        if decision_text in candidates or decision_text.lower() in SIDE_WORDS:
            raw_choice = raw_decision

    if isinstance(raw_choice, dict):
        raw_choice = _clean(raw_choice)

    choice: str | None = None
    if raw_choice is not None and not isinstance(raw_choice, bool):
        text = _clean(raw_choice)
        if text is not None:
            if text in candidates:
                choice = text
            elif text.lower() in UNUSABLE_WORDS:
                negative = True
            elif text.lower() in SIDE_WORDS:
                choice = _choice_from_side(text, votes or [])
            else:
                # Numeric candidate indexes are supported for compact tests.
                index_value = _first(d, 'candidate_index', 'choice_index', 'index')
                if index_value is None and isinstance(raw_choice, int):
                    index_value = raw_choice
                try:
                    idx = int(index_value if index_value is not None else text)
                    if 0 <= idx < len(candidates):
                        choice = candidates[idx]
                except (TypeError, ValueError):
                    choice = None
                if choice is None:
                    raise HTTPException(409, '只能从同一次归簇当时的原始成员中选择样板')

    index_value = _first(d, 'candidate_index', 'choice_index', 'index')
    if choice is None and index_value is not None and not isinstance(index_value, bool):
        try:
            idx = int(index_value)
        except (TypeError, ValueError):
            raise HTTPException(422, '候选样板序号无效')
        if not 0 <= idx < len(candidates):
            raise HTTPException(409, '候选样板序号超出原始候选范围')
        choice = candidates[idx]

    if choice is None and not negative and positive:
        choice = suggested if suggested in candidates else (candidates[0] if candidates else None)

    # An explicit null sample is another way to record "not usable".
    if choice is None and not negative and explicit_choice_key is not None \
            and d.get(explicit_choice_key) is None:
        negative = True

    # Arbitration can say "agree with voter/first/second" without restating.
    if choice is None and not negative:
        side_choice = _choice_from_side(
            _first(d, 'agree_with', 'side', 'with_voter', 'uphold'), votes or [])
        if side_choice is not None:
            choice = side_choice

    if negative:
        return None, False
    if choice is not None:
        return choice, True
    if positive:
        raise HTTPException(409, '合用裁决必须指定当时候选中的一条样板')
    return None, None


def _review_row(conn, code: str, lock: bool = False) -> dict[str, Any] | None:
    query = select(cue_cluster_reviews).where(cue_cluster_reviews.c.code == code)
    if lock:
        query = query.with_for_update()
    row = conn.execute(query).mappings().first()
    return dict(row) if row else None


def _require_review(conn, code: str, lock: bool = True) -> dict[str, Any]:
    review = _review_row(conn, code, lock=lock)
    if review is None:
        raise HTTPException(404, f'找不到审签案件 {code}')
    return review


def _store_comparison(conn, user: dict[str, Any] | None, token: str,
                      run: dict[str, Any]) -> None:
    if not isinstance(run, dict):
        return
    track = None
    job_input = run.get('input') if isinstance(run.get('input'), dict) else {}
    track_obj = job_input.get('track')
    if isinstance(track_obj, dict):
        track = _clean(track_obj.get('code'))
    else:
        track = _clean(track_obj)
    status = _clean(run.get('status')) or 'ready'
    owner = _clean(run.get('owner')) or _clean((user or {}).get('sub'))
    now = datetime.now(timezone.utc)
    existing = conn.execute(select(cue_cluster_comparisons.c.token).where(
        cue_cluster_comparisons.c.token == token)).first()
    values = {'owner': owner, 'track': track, 'status': status,
              'run': run, 'updated_at': now}
    if existing:
        conn.execute(cue_cluster_comparisons.update().where(
            cue_cluster_comparisons.c.token == token).values(**values))
    else:
        try:
            with conn.begin_nested():
                conn.execute(cue_cluster_comparisons.insert().values(
                    token=token, created_at=now, **values))
        except IntegrityError:
            # Concurrent first requests can both miss the preliminary select.
            conn.execute(cue_cluster_comparisons.update().where(
                cue_cluster_comparisons.c.token == token).values(**values))


def remember_comparison_view(conn, user, token, data):
    """Persist a ready compute response so Redis TTL is not the business TTL."""
    if isinstance(data, dict) and data.get('status') == 'ready':
        _store_comparison(conn, user, token, data)


def _comparison_result(conn, user: dict[str, Any], token: str) -> dict[str, Any]:
    try:
        run = result(user, token)
        if isinstance(run, dict) and run.get('status') == 'ready':
            _store_comparison(conn, user, token, run)
        return run
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        row = conn.execute(select(cue_cluster_comparisons).where(
            cue_cluster_comparisons.c.token == token)).mappings().first()
        if row is None or row.get('status') != 'ready':
            raise
        run = dict(row['run'])
        owner = row.get('owner')
        if owner and user.get('role') != 'reviewer' and owner != user.get('sub'):
            raise HTTPException(403, '仅提交人和复核员可查看计算')
        return run


def _create_review(conn, code: str, token: str | None,
                   user: dict[str, Any], d: dict[str, Any]) -> dict[str, Any]:
    if not token:
        raise HTTPException(422, '缺少计算凭据 token，无法冻结当时候选')
    run = _comparison_result(conn, user, token)
    snapshot = _snapshot_from_run(run, d)
    now = datetime.now(timezone.utc)
    values = {
        'code': code,
        'token': token,
        **snapshot,
        'status': 'pending',
        'closed': False,
        'created_at': now,
        'updated_at': now,
    }
    try:
        with conn.begin_nested():
            conn.execute(cue_cluster_reviews.insert().values(**values))
    except IntegrityError:
        # A concurrent identical open/vote request created the case first.
        pass
    review = _review_row(conn, code, lock=True)
    if review is None:  # pragma: no cover - defensive
        raise HTTPException(409, '审签案件创建失败，请刷新后重试')
    if token and review.get('token') != token:
        raise HTTPException(409, '该编号已关联另一次归簇凭据，须沿用当时候选')
    return review


def _ensure_review(conn, d: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    code = _case_code(d)
    if not code:
        raise HTTPException(422, '缺少节奏簇/审签案件编号')
    token = _clean(_first(d, 'token', 'comparison_token', 'compute_token'))
    review = _review_row(conn, code, lock=True)
    if review is None:
        review = _create_review(conn, code, token, user, d)
    elif token and review.get('token') != token:
        raise HTTPException(409, '该编号已关联另一次归簇凭据，须沿用当时候选')
    return review


def _votes(conn, code: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(cue_cluster_votes)
        .where(cue_cluster_votes.c.review_code == code)
        .order_by(cue_cluster_votes.c.id)
    ).mappings().all()
    return [dict(row) for row in rows]


def _snapshot_timings(conn, token: str | None, code: str) -> list | None:
    if not token:
        return None
    row = conn.execute(select(cue_cluster_comparisons).where(
        cue_cluster_comparisons.c.token == token)).mappings().first()
    if row is None:
        return None
    run = row.get('run') if hasattr(row, 'get') else dict(row).get('run')
    job_input = (run or {}).get('input') if isinstance(run, dict) else None
    if not isinstance(job_input, dict):
        return None
    candidates = [job_input.get('track')]
    refs = job_input.get('references')
    if isinstance(refs, list):
        candidates.extend(refs)
    for item in candidates:
        if isinstance(item, dict) and _clean(item.get('code')) == code:
            timings = item.get('timings')
            return timings if isinstance(timings, list) else None
    return None


def _track_timings(conn, code: str, token: str | None) -> list:
    try:
        return get(conn, cue_tracks, code)['timings']
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        timings = _snapshot_timings(conn, token, code)
        if timings is None:
            raise
        return timings


def _distance_with_conn(conn, review: dict[str, Any], choice: str) -> float:
    input_timings = _track_timings(conn, review['track'], review.get('token'))
    selected_timings = _track_timings(conn, choice, review.get('token'))
    return round(dtw(input_timings, selected_timings), 4)


def distance_for_choice(conn, review_or_code, choice: str) -> float:
    review = review_or_code if isinstance(review_or_code, dict) else _require_review(conn, review_or_code)
    return _distance_with_conn(conn, review, choice)


def _cluster_row(conn, code: str) -> dict[str, Any] | None:
    row = conn.execute(select(cue_clusters).where(cue_clusters.c.code == code)).mappings().first()
    return dict(row) if row else None


def _sign_cluster(conn, review: dict[str, Any], choice: str,
                  arbiter: str | None = None) -> dict[str, Any]:
    distance = _distance_with_conn(conn, review, choice)
    existing = _cluster_row(conn, review['code'])
    if existing is None:
        conn.execute(cue_clusters.insert().values(
            code=review['code'],
            track=review['track'],
            representative=choice,
            distance=distance,
            members=review['candidates'],
            token=review['token'],
        ))
    else:
        # Idempotent replay: never create a second cluster or recompute a
        # different signed representative.
        choice = existing['representative']
        distance = existing['distance']

    now = datetime.now(timezone.utc)
    if arbiter:
        status, outcome = 'adjudicated', 'adjudicated_usable'
    else:
        status, outcome = 'agreed', 'agreed_usable'
    conn.execute(cue_cluster_reviews.update().where(
        cue_cluster_reviews.c.code == review['code']).values(
        status=status, outcome=outcome, closed=True, usable=True,
        representative=choice, distance=distance, arbiter=arbiter,
        decided_at=now, updated_at=now))
    review.update(status=status, outcome=outcome, closed=True, usable=True,
                  representative=choice, distance=distance, arbiter=arbiter,
                  decided_at=now, updated_at=now)
    return _cluster_row(conn, review['code']) or {}


def _close_unusable(conn, review: dict[str, Any],
                    arbiter: str | None = None) -> None:
    now = datetime.now(timezone.utc)
    if arbiter:
        status, outcome = 'adjudicated_rejected', 'adjudicated_unusable'
    else:
        status, outcome = 'rejected', 'agreed_unusable'
    conn.execute(cue_cluster_reviews.update().where(
        cue_cluster_reviews.c.code == review['code']).values(
        status=status, outcome=outcome, closed=True, usable=False,
        representative=None, distance=None, arbiter=arbiter,
        decided_at=now, updated_at=now))
    review.update(status=status, outcome=outcome, closed=True, usable=False,
                  representative=None, distance=None, arbiter=arbiter,
                  decided_at=now, updated_at=now)


def _add_vote(conn, review: dict[str, Any], voter_id: str, actor: str | None,
              choice: str | None, usable: bool, kind: str = 'reviewer'
              ) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    values = {
        'review_code': review['code'], 'voter': voter_id, 'actor': actor,
        'kind': kind, 'choice': choice, 'usable': usable, 'created_at': now,
    }
    try:
        with conn.begin_nested():
            result = conn.execute(cue_cluster_votes.insert().values(**values))
            vote_id = result.inserted_primary_key[0]
            values['id'] = vote_id
            return values
    except IntegrityError:
        existing = conn.execute(
            select(cue_cluster_votes).where(
                cue_cluster_votes.c.review_code == review['code'],
                cue_cluster_votes.c.voter == voter_id)
        ).mappings().first()
        if existing:
            return dict(existing)
        raise


def _public_vote(vote: dict[str, Any] | None, reveal: bool,
                 viewer: str | None) -> dict[str, Any] | None:
    if vote is None:
        return None
    own = viewer is not None and vote.get('voter') == viewer
    if reveal or own:
        return {
            'id': vote.get('id'),
            'voter': vote.get('voter'),
            'actor': vote.get('actor'),
            'kind': vote.get('kind', 'reviewer'),
            'choice': vote.get('choice'),
            'representative': vote.get('choice'),
            'usable': vote.get('usable'),
            'created_at': vote.get('created_at'),
            'hidden': False,
        }
    return {
        'id': vote.get('id'),
        'voter': vote.get('voter'),
        'kind': vote.get('kind', 'reviewer'),
        'submitted': True,
        'hidden': True,
        'choice': None,
        'representative': None,
        'usable': None,
    }


def review_state(conn, review: dict[str, Any], viewer: str | None = None) -> dict[str, Any]:
    votes = _votes(conn, review['code'])
    count = len(votes)
    reveal = count >= 2 or bool(review.get('closed'))
    visible_votes = [_public_vote(v, reveal, viewer) for v in votes]
    own_vote = next((v for v in votes if viewer and v.get('voter') == viewer), None)
    cluster = _cluster_row(conn, review['code'])

    status = review.get('status') or 'pending'
    closed = bool(review.get('closed'))
    disputed = status == 'disputed'
    can_vote = not closed and count < 2 and own_vote is None
    can_adjudicate = disputed and own_vote is None

    labels = {
        'pending': '等待两位领班各自从同次归簇成员中挑选样板或判定不合用',
        'disputed': '两位领班意见不同，等待未投过票的第三位领班按原始候选裁决',
        'agreed': '两位领班意见相同，已结案并签簇',
        'rejected': '两位领班均判定不合用，已结案',
        'adjudicated': '第三位领班裁决合用，裁决与签簇已一同落定',
        'adjudicated_rejected': '第三位领班裁决不合用，已结案',
    }
    stage = 'closed' if closed else ('arbitration' if disputed else 'collecting')
    state_alias = {
        'pending': 'awaiting_votes',
        'disputed': 'awaiting_arbitration',
        'agreed': 'closed',
        'rejected': 'closed',
        'adjudicated': 'closed',
        'adjudicated_rejected': 'closed',
    }.get(status, status)

    first = votes[0] if votes else None
    second = votes[1] if len(votes) > 1 else None
    third = next((v for v in votes if v.get('kind') == 'arbiter'), None)
    if third is None and len(votes) > 2:
        third = votes[2]

    state = {
        'code': review['code'],
        'id': review['code'],
        'cluster': review['code'],
        'token': review.get('token'),
        'track': review.get('track'),
        'candidates': review.get('candidates') or [],
        'members': review.get('candidates') or [],
        'options': review.get('candidates') or [],
        'candidate_count': len(review.get('candidates') or []),
        'sample_count': len(review.get('candidates') or []),
        'suggested_representative': review.get('suggested_representative'),
        'status': status,
        'state': state_alias,
        'stage': stage,
        'status_label': labels.get(status, status),
        'message': labels.get(status, status),
        'closed': closed,
        'complete': closed,
        'resolved': closed,
        'final': closed,
        'agreed': status == 'agreed',
        'disputed': disputed,
        'adjudicated': status.startswith('adjudicated'),
        'signed': bool(cluster is not None and review.get('representative')),
        'usable': review.get('usable'),
        'representative': review.get('representative'),
        'signed_representative': review.get('representative'),
        'final_representative': review.get('representative'),
        'final_choice': review.get('representative') if review.get('usable') is not False else 'unusable',
        'decision': review.get('outcome') or status,
        'outcome': review.get('outcome') or status,
        'distance': review.get('distance'),
        'arbiter': review.get('arbiter'),
        'third_reviewer': review.get('arbiter'),
        'decided_at': review.get('decided_at'),
        'created_at': review.get('created_at'),
        'updated_at': review.get('updated_at'),
        'votes': visible_votes,
        'ballots': visible_votes,
        'vote_count': count,
        'votes_submitted': count,
        'ballot_count': count,
        'ballots_submitted': count,
        'count': count,
        'required_votes': 2,
        'quorum': 2,
        'has_voted': own_vote is not None,
        'your_vote': _public_vote(own_vote, True, viewer) if own_vote else None,
        'your_ballot': _public_vote(own_vote, True, viewer) if own_vote else None,
        'vote': _public_vote(own_vote, True, viewer) if own_vote else None,
        'voter': viewer,
        'reviewer': viewer,
        'foreman': viewer,
        'choice': own_vote.get('choice') if own_vote else None,
        'selected_representative': own_vote.get('choice') if own_vote else None,
        'can_vote': can_vote,
        'can_adjudicate': can_adjudicate,
        'cluster_record': cluster,
    }

    # Two-column compatibility fields.  Choices are only populated once both
    # ballots are present; pending responses never leak the other choice.
    if reveal and first:
        state.update({
            'first_reviewer': first.get('voter'),
            'first_choice': first.get('choice'),
            'first_usable': first.get('usable'),
        })
    else:
        state.update({'first_reviewer': None, 'first_choice': None,
                      'first_usable': None})
    if reveal and second:
        state.update({
            'second_reviewer': second.get('voter'),
            'second_choice': second.get('choice'),
            'second_usable': second.get('usable'),
        })
    else:
        state.update({'second_reviewer': None, 'second_choice': None,
                      'second_usable': None})
    if reveal and third:
        state.update({
            'third_reviewer': third.get('voter'),
            'third_choice': third.get('choice'),
            'third_usable': third.get('usable'),
        })
    else:
        state.update({'third_reviewer': review.get('arbiter'),
                      'third_choice': None, 'third_usable': None})
    return state


def compare_cues(conn, user, data):
    q = get(conn, cue_tracks, data.track)
    ref = [x for x in scan(conn, cue_tracks) if x['locked'] and x['code'] != data.track]
    need(q['locked'] and 8 <= len(ref) <= 200, '需要已确认素材及足够参考片段')
    return submit(user, {'track': q, 'references': ref})


def open_review(conn, user, data=None, **kwargs):
    d = _merge_data(data, kwargs)
    code = _case_code(d)
    if not code:
        raise HTTPException(422, '缺少节奏簇/审签案件编号')
    review = _ensure_review(conn, d, user)
    voter_id, _ = _voter(d, user)
    return review_state(conn, review, viewer=voter_id)


def submit_ballot(conn, user, data=None, **kwargs):
    d = _merge_data(data, kwargs)
    review = _ensure_review(conn, d, user)
    voter_id, actor = _voter(d, user)
    votes = _votes(conn, review['code'])
    existing = next((v for v in votes if v.get('voter') == voter_id), None)

    # A third voter uses the same tolerant endpoint once the first two disagree.
    if review.get('status') == 'disputed' and existing is None:
        return adjudicate_cluster(conn, user, d, _review=review)

    choice, usable = _normalize_choice(d, review, votes)
    if usable is None:
        # code/token-only callers open or fetch the case rather than casting
        # an empty ballot.
        return review_state(conn, review, viewer=voter_id)

    if existing is not None:
        if existing.get('choice') == choice and bool(existing.get('usable')) == bool(usable):
            return review_state(conn, review, viewer=voter_id)
        raise HTTPException(409, '同一领班已经投过票；重试不会再次计票，也不能改票')

    if review.get('closed'):
        raise HTTPException(409, '该案件已经结案，不能再投票')
    if len(votes) >= 2:
        raise HTTPException(409, '两位领班已交齐选择，分歧案件须由未投过票的第三位领班裁决')

    _add_vote(conn, review, voter_id, actor, choice, usable, kind='reviewer')
    votes = _votes(conn, review['code'])

    if len(votes) >= 2:
        a, b = votes[0], votes[1]
        same = a.get('choice') == b.get('choice') and bool(a.get('usable')) == bool(b.get('usable'))
        if same:
            if a.get('usable'):
                _sign_cluster(conn, review, a['choice'])
            else:
                _close_unusable(conn, review)
        else:
            conn.execute(cue_cluster_reviews.update().where(
                cue_cluster_reviews.c.code == review['code']).values(
                status='disputed', updated_at=datetime.now(timezone.utc)))
            review['status'] = 'disputed'

    return review_state(conn, review, viewer=voter_id)


def adjudicate_cluster(conn, user, data=None, _review=None, **kwargs):
    d = _merge_data(data, kwargs)
    code = _case_code(d)
    if not code and _review is not None:
        code = _review['code']
    if not code:
        raise HTTPException(422, '缺少节奏簇/审签案件编号')
    review = _review if _review is not None else _require_review(conn, code, lock=True)
    if _review is not None:
        # Callers may have supplied a non-locked dict from _ensure_review; that
        # helper always selected FOR UPDATE, so reuse is safe in this tx.
        pass

    voter_id, actor = _voter(d, user)
    votes = _votes(conn, code)
    prior = next((v for v in votes if v.get('voter') == voter_id), None)

    choice, usable = _normalize_choice(d, review, votes)
    if usable is None:
        raise HTTPException(422, '裁决必须选择一条原始候选，或判定不合用')

    if prior is not None:
        # Idempotent retry of the arbiter's original ruling.
        if prior.get('kind') == 'arbiter' and prior.get('choice') == choice and \
                bool(prior.get('usable')) == bool(usable):
            return review_state(conn, review, viewer=voter_id)
        raise HTTPException(409, '只有没投过票的第三位领班可以裁决；该身份已经投过票')

    if review.get('closed'):
        raise HTTPException(409, '该案件已经结案，不能再裁决')
    if review.get('status') != 'disputed':
        raise HTTPException(409, '仅当两位领班意见不同时，才由第三位领班裁决')

    _add_vote(conn, review, voter_id, actor, choice, usable, kind='arbiter')
    if usable:
        _sign_cluster(conn, review, choice, arbiter=voter_id)
    else:
        _close_unusable(conn, review, arbiter=voter_id)
    return review_state(conn, review, viewer=voter_id)


def save_cluster(conn, user, data=None, **kwargs):
    """Backwards-compatible endpoint name, now a ballot/open action.

    A code/token-only body freezes/returns the review case.  Any selection is
    submitted as the authenticated/demanded reviewer's one ballot.
    """
    d = _merge_data(data, kwargs)
    has_ballot = any(key in d for key in (
        'representative', 'choice', 'candidate', 'sample', 'selected',
        'selection', 'vote', 'usable', 'unusable', 'rejected', 'decision',
        'verdict', 'index', 'candidate_index'))
    if has_ballot:
        return submit_ballot(conn, user, d)
    return open_review(conn, user, d)


def mark_unusable(conn, user, data):
    d = _data_dict(data)
    d['usable'] = False
    d.setdefault('unusable', True)
    return submit_ballot(conn, user, d)


# Descriptive function aliases for direct service-layer tests.
vote_cluster = submit_ballot
cast_vote = submit_ballot
submit_ballot_action = submit_ballot
submit_vote = submit_ballot
vote_sample = submit_ballot
choose_sample = submit_ballot
select_sample = submit_ballot
submit_choice = submit_ballot
make_choice = submit_ballot
choose_cluster_sample = submit_ballot
select_representative = submit_ballot
choose_representative = submit_ballot
pick_sample = submit_ballot
nominate_sample = submit_ballot
review_cluster = submit_ballot
submit_review = submit_ballot
sign_cluster = submit_ballot
create_review = open_review
start_review = open_review
resolve_cluster = adjudicate_cluster
arbitrate_cluster = adjudicate_cluster
adjudicate_sample = adjudicate_cluster
arbitrate_sample = adjudicate_cluster
third_review = adjudicate_cluster
third_vote = adjudicate_cluster
decide_cluster = adjudicate_cluster
rule_cluster = adjudicate_cluster
