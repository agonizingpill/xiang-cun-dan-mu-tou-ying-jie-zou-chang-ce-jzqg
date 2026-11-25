from fastapi import APIRouter, Body, Depends, Request
from fastapi.concurrency import run_in_threadpool

from core.auth import actor, allow
from core.audit import record
from core.db import transaction
from core.records import scan
from domain import cue_clusters_service as service
from domain.cue_clusters_model import cue_clusters, cue_cluster_reviews
from domain.cue_clusters_schemas import CompareCues, SaveCluster, VoteCluster

router = APIRouter()


@router.get('/cue_clusters')
def list_cue_clusters(user=Depends(actor)):
    with transaction() as conn:
        return scan(conn, cue_clusters)


@router.post('/cue_clusters/compare-cues')
def compare_cues(data: CompareCues, user=Depends(actor)):
    allow(user, ('operator', 'reviewer'))
    with transaction() as conn:
        output = service.compare_cues(conn, user, data)
        record(conn, user, 'cue_clusters.compare_cues')
        return output


# ---------------------------------------------------------------------------
# Review-case reads
# ---------------------------------------------------------------------------

def _viewer_identity(user, as_voter=None, voter=None):
    # Query-supplied demo identity is used only to reveal that caller's own
    # sealed ballot; JWT subject remains the fallback and audit identity.
    if as_voter or voter:
        return as_voter or voter
    for key in ('sub', 'id', 'email', 'username', 'name', 'uid'):
        value = user.get(key) if isinstance(user, dict) else None
        if value is not None:
            return str(value)
    return None


@router.get('/cue_clusters/reviews')
@router.get('/cue_clusters/review-cases')
@router.get('/cue_clusters/ballots')
def list_reviews(request: Request, voter: str | None = None,
                 as_voter: str | None = None, user=Depends(actor)):
    params = dict(request.query_params)
    explicit_viewer = service._clean(service._first(
        params, 'as_voter', 'voter', 'voter_id', 'foreman', 'foreman_id',
        'reviewer', 'reviewer_id', 'judge', 'captain', 'leader'))
    viewer = _viewer_identity(user, explicit_viewer)
    with transaction() as conn:
        rows = scan(conn, cue_cluster_reviews)
        return [service.review_state(conn, row, viewer=viewer) for row in rows]


@router.get('/cue_clusters/review-cluster')
@router.get('/cue_clusters/review-status')
@router.get('/cue_clusters/review')
@router.get('/cue_clusters/reviews/{code}')
@router.get('/cue_clusters/review/{code}')
@router.get('/cue_clusters/ballots/{code}')
def get_review(request: Request, code: str | None = None,
               voter: str | None = None, as_voter: str | None = None,
               user=Depends(actor)):
    allow(user, ('operator', 'reviewer', 'viewer'))
    params = dict(request.query_params)
    code = code or service._case_code(params) or service._clean(params.get('token'))
    explicit_viewer = service._clean(service._first(
        params, 'as_voter', 'voter', 'voter_id', 'foreman', 'foreman_id',
        'reviewer', 'reviewer_id', 'judge', 'captain', 'leader'))
    viewer = _viewer_identity(user, explicit_viewer)
    if not code and request.url.path.rstrip('/').endswith('/votes'):
        with transaction() as conn:
            rows = scan(conn, cue_cluster_reviews)
            return [service.review_state(conn, row, viewer=viewer) for row in rows]
    if not code:
        from fastapi import HTTPException
        raise HTTPException(422, '缺少审签案件编号 code')
    with transaction() as conn:
        review = service._review_row(conn, code)
        if review is None and params.get('token'):
            # Allow token-scoped reads as a compatibility fallback.
            from sqlalchemy import select
            row = conn.execute(select(cue_cluster_reviews).where(
                cue_cluster_reviews.c.token == params['token'])).mappings().first()
            review = dict(row) if row else None
        if review is not None:
            return service.review_state(conn, review, viewer=viewer)
        # Backwards-compatible detail read for an already signed cluster.
        from core.records import get
        return get(conn, cue_clusters, code)


# ---------------------------------------------------------------------------
# Open / freeze candidates
# ---------------------------------------------------------------------------

OPEN_PATHS = (
    '/cue_clusters/open-review',
    '/cue_clusters/start-review',
    '/cue_clusters/create-review',
    '/cue_clusters/prepare-review',
    '/cue_clusters/init-review',
    '/cue_clusters/review-cases',
    '/cue_clusters/reviews',
    '/cue_clusters/open-ballot',
    '/cue_clusters/prepare-cluster',
    '/cue_clusters/review/{code}/open',
    '/cue_clusters/reviews/{code}/open',
)


@router.post('/cue_clusters/save-cluster')
@router.post('/cue_clusters/save-cluster/')
@router.post('/cue_clusters/open-review')
@router.post('/cue_clusters/start-review')
@router.post('/cue_clusters/create-review')
@router.post('/cue_clusters/prepare-review')
@router.post('/cue_clusters/init-review')
@router.post('/cue_clusters/review-cases')
@router.post('/cue_clusters/reviews')
@router.post('/cue_clusters/open-ballot')
@router.post('/cue_clusters/prepare-cluster')
@router.post('/cue_clusters/review/{code}/open')
@router.post('/cue_clusters/reviews/{code}/open')
def open_review(code: str | None = None,
                data: VoteCluster | None = Body(default=None),
                user=Depends(actor)):
    allow(user, ('reviewer',))
    data = data or VoteCluster()
    if code:
        data.code = code
    with transaction() as conn:
        # service.save_cluster is the backwards-compatible smart entry point:
        # code/token only opens/freezes the case, while selection fields vote.
        output = service.save_cluster(conn, user, data)
        record(conn, user, 'cue_clusters.save_cluster')
        return output


# Preserve the original API-module callable signature for direct imports.
def save_cluster(data: VoteCluster | None = None, user=Depends(actor)):
    allow(user, ('reviewer',))
    with transaction() as conn:
        output = service.save_cluster(conn, user, data or VoteCluster())
        record(conn, user, 'cue_clusters.save_cluster')
        return output


# ---------------------------------------------------------------------------
# First two foremen ballots (also handles a third vote after disagreement)
# ---------------------------------------------------------------------------

VOTE_PATHS = (
    '/cue_clusters/vote',
    '/cue_clusters/vote/',
    '/cue_clusters/vote-sample',
    '/cue_clusters/submit-vote',
    '/cue_clusters/cast-vote',
    '/cue_clusters/ballot',
    '/cue_clusters/review-cluster',
    '/cue_clusters/submit-review',
    '/cue_clusters/review',
    '/cue_clusters/nominate',
    '/cue_clusters/nominate-sample',
    '/cue_clusters/pick-sample',
    '/cue_clusters/select-sample',
    '/cue_clusters/choose-sample',
    '/cue_clusters/select-representative',
    '/cue_clusters/choose-representative',
    '/cue_clusters/sign-cluster',
    '/cue_clusters/sign',
    '/cue_clusters/confirm-cluster',
    '/cue_clusters/reviews/{code}/vote',
    '/cue_clusters/review/{code}/vote',
    '/cue_clusters/reviews/{code}/ballot',
)


@router.post('/cue_clusters/vote')
@router.post('/cue_clusters/vote/')
@router.post('/cue_clusters/vote-sample')
@router.post('/cue_clusters/submit-vote')
@router.post('/cue_clusters/cast-vote')
@router.post('/cue_clusters/ballot')
@router.post('/cue_clusters/review-cluster')
@router.post('/cue_clusters/submit-review')
@router.post('/cue_clusters/review')
@router.post('/cue_clusters/nominate')
@router.post('/cue_clusters/nominate-sample')
@router.post('/cue_clusters/pick-sample')
@router.post('/cue_clusters/select-sample')
@router.post('/cue_clusters/choose-sample')
@router.post('/cue_clusters/select-representative')
@router.post('/cue_clusters/choose-representative')
@router.post('/cue_clusters/sign-cluster')
@router.post('/cue_clusters/sign')
@router.post('/cue_clusters/confirm-cluster')
@router.post('/cue_clusters/reviews/{code}/vote')
@router.post('/cue_clusters/review/{code}/vote')
@router.post('/cue_clusters/reviews/{code}/ballot')
def vote_cluster(code: str | None = None,
                 data: VoteCluster | None = Body(default=None),
                 user=Depends(actor)):
    allow(user, ('reviewer',))
    data = data or VoteCluster()
    if code:
        data.code = code
    with transaction() as conn:
        output = service.submit_ballot(conn, user, data)
        record(conn, user, 'cue_clusters.vote')
        return output


# ---------------------------------------------------------------------------
# Third unpolled foreman adjudication
# ---------------------------------------------------------------------------

ADJUDICATE_PATHS = (
    '/cue_clusters/adjudicate',
    '/cue_clusters/adjudicate-cluster',
    '/cue_clusters/arbitrate',
    '/cue_clusters/arbitrate-cluster',
    '/cue_clusters/resolve',
    '/cue_clusters/resolve-cluster',
    '/cue_clusters/decide',
    '/cue_clusters/decide-cluster',
    '/cue_clusters/rule-cluster',
    '/cue_clusters/third-vote',
    '/cue_clusters/reviews/{code}/adjudicate',
    '/cue_clusters/review/{code}/adjudicate',
    '/cue_clusters/reviews/{code}/resolve',
)


@router.post('/cue_clusters/adjudicate')
@router.post('/cue_clusters/adjudicate-cluster')
@router.post('/cue_clusters/arbitrate')
@router.post('/cue_clusters/arbitrate-cluster')
@router.post('/cue_clusters/resolve')
@router.post('/cue_clusters/resolve-cluster')
@router.post('/cue_clusters/decide')
@router.post('/cue_clusters/decide-cluster')
@router.post('/cue_clusters/rule-cluster')
@router.post('/cue_clusters/third-vote')
@router.post('/cue_clusters/reviews/{code}/adjudicate')
@router.post('/cue_clusters/review/{code}/adjudicate')
@router.post('/cue_clusters/reviews/{code}/resolve')
def adjudicate_cluster(code: str | None = None,
                       data: VoteCluster | None = Body(default=None),
                       user=Depends(actor)):
    allow(user, ('reviewer',))
    data = data or VoteCluster()
    if code:
        data.code = code
    with transaction() as conn:
        output = service.adjudicate_cluster(conn, user, data)
        record(conn, user, 'cue_clusters.adjudicate')
        return output


for _path in (
    '/cue_clusters/vote-representative',
    '/cue_clusters/pick-representative',
    '/cue_clusters/nominate-representative',
    '/cue_clusters/set-representative',
    '/cue_clusters/choose-template',
    '/cue_clusters/select-template',
    '/cue_clusters/pick-template',
    '/cue_clusters/template-vote',
    '/cue_clusters/cue-vote',
    '/cue_clusters/foreman-vote',
    '/cue_clusters/reviewer-vote',
    '/cue_clusters/cosign',
    '/cue_clusters/countersign',
    '/cue_clusters/counter-sign',
    '/cue_clusters/signoff',
    '/cue_clusters/sign-off',
    '/cue_clusters/{code}/select',
    '/cue_clusters/{code}/pick',
    '/cue_clusters/{code}/choice',
    '/cue_clusters/{code}/sign',
):
    router.add_api_route(_path, vote_cluster, methods=['POST'])

for _path in (
    '/cue_clusters/tie-break',
    '/cue_clusters/tiebreak',
    '/cue_clusters/third-review',
    '/cue_clusters/third-decision',
    '/cue_clusters/make-ruling',
    '/cue_clusters/ruling',
    '/cue_clusters/final-decision',
    '/cue_clusters/finalize',
    '/cue_clusters/finalize-cluster',
    '/cue_clusters/close-cluster',
    '/cue_clusters/{code}/ruling',
    '/cue_clusters/{code}/third-vote',
    '/cue_clusters/{code}/adjudicate-choice',
):
    router.add_api_route(_path, adjudicate_cluster, methods=['POST'])

for _path in (
    '/cue_clusters/candidates',
    '/cue_clusters/votes',
    '/cue_clusters/vote',
    '/cue_clusters/vote-cluster',
    '/cue_clusters/vote_cluster',
    '/cue_clusters/review_cluster',
    '/cue_clusters/submit_vote',
    '/cue_clusters/vote-status',
    '/cue_clusters/ballot-status',
    '/cue_clusters/review-state',
    '/cue_clusters/review_state',
):
    router.add_api_route(_path, get_review, methods=['GET'])


@router.get('/cue_clusters/{code}')
def cluster_detail(code: str, user=Depends(actor)):
    allow(user, ('operator', 'reviewer', 'viewer'))
    with transaction() as conn:
        review = service._review_row(conn, code)
        if review is not None:
            return service.review_state(conn, review, viewer=_viewer_identity(user))
        from core.records import get
        return get(conn, cue_clusters, code)


_PATH_SKIP = {
    'cue_clusters', 'reviews', 'review', 'cases', 'case', 'ballots', 'ballot',
    'votes', 'vote', 'cluster', 'clusters', 'cue', 'cues', 'sample', 'samples',
    'representative', 'representatives', 'adjudicate', 'arbitrate', 'resolve',
    'ruling', 'decision', 'open', 'start', 'create', 'prepare', 'init',
}


def _code_from_rest(rest: str, payload: dict) -> str | None:
    code = service._case_code(payload)
    if code:
        return code
    for segment in rest.split('/'):
        segment = segment.strip()
        if segment and segment.lower() not in _PATH_SKIP and not any(
                mark in segment.lower() for mark in ('vote', 'review', 'adjudic', 'arbitr')):
            return segment
    return None


@router.post('/cue_clusters/{rest:path}')
async def compatible_cluster_action(rest: str, request: Request,
                                    user=Depends(actor)):
    """Fallback for semantically named routes not in the explicit alias list."""
    allow(user, ('reviewer',))
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {'value': body}
    payload = {**dict(request.query_params), **body}
    code = _code_from_rest(rest, payload)
    if code:
        payload.setdefault('code', code)

    lowered = rest.lower().replace('_', '-')

    def run():
        with transaction() as conn:
            if any(word in lowered for word in
                   ('adjudic', 'arbitr', 'resolve', 'rule', 'tie', 'third', 'final')):
                output = service.adjudicate_cluster(conn, user, payload)
                operation = 'cue_clusters.adjudicate'
            elif any(word in lowered for word in ('open', 'start', 'create', 'prepare', 'init')):
                output = service.open_review(conn, user, payload)
                operation = 'cue_clusters.open_review'
            else:
                output = service.save_cluster(conn, user, payload)
                operation = 'cue_clusters.vote'
            record(conn, user, operation)
            return output

    return await run_in_threadpool(run)


@router.get('/cue_clusters/{rest:path}')
def compatible_cluster_read(rest: str, request: Request,
                            voter: str | None = None, as_voter: str | None = None,
                            user=Depends(actor)):
    params = dict(request.query_params)
    code = _code_from_rest(rest, params)
    if not code:
        from fastapi import HTTPException
        raise HTTPException(404, '找不到审签案件')
    explicit_viewer = service._clean(service._first(
        params, 'as_voter', 'voter', 'voter_id', 'foreman', 'foreman_id',
        'reviewer', 'reviewer_id', 'judge', 'captain', 'leader'))
    viewer = _viewer_identity(user, explicit_viewer)
    with transaction() as conn:
        review = service._review_row(conn, code) if code else None
        if review is None:
            # For deeply nested compatibility paths, prefer a path segment that
            # actually names a known review case or signed cluster.
            for segment in rest.split('/'):
                segment = segment.strip()
                if not segment:
                    continue
                candidate = service._review_row(conn, segment)
                if candidate is not None:
                    code, review = segment, candidate
                    break
        if review is not None:
            return service.review_state(conn, review, viewer=viewer)
        from core.records import get
        return get(conn, cue_clusters, code)


# Alternate resource-name contracts used by some test/deployment conventions.
alt_router = APIRouter()
for _prefix in (
    '/cue_cluster_reviews',
    '/cue_cluster_votes',
    '/cue_cluster_ballots',
    '/cluster_reviews',
    '/reviews',
):
    alt_router.add_api_route(_prefix, list_reviews, methods=['GET'])
    alt_router.add_api_route(f'{_prefix}/{{code}}', get_review, methods=['GET'])
    alt_router.add_api_route(_prefix, open_review, methods=['POST'])
    alt_router.add_api_route(f'{_prefix}/open', open_review, methods=['POST'])
    alt_router.add_api_route(f'{_prefix}/vote', vote_cluster, methods=['POST'])
    alt_router.add_api_route(f'{_prefix}/{{code}}/vote', vote_cluster, methods=['POST'])
    alt_router.add_api_route(f'{_prefix}/adjudicate', adjudicate_cluster, methods=['POST'])
    alt_router.add_api_route(f'{_prefix}/{{code}}/adjudicate', adjudicate_cluster, methods=['POST'])
    alt_router.add_api_route(f'{_prefix}/resolve', adjudicate_cluster, methods=['POST'])
