from typing import Any, Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


class CompareCues(BaseModel):
    model_config = ConfigDict(extra="forbid")
    track: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class SaveCluster(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
    token: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class VoteCluster(BaseModel):
    """Flexible ballot body.

    The public UI uses ``code`` + ``representative`` and a boolean-ish
    ``usable``.  The many optional aliases keep the HTTP surface tolerant of
    callers that name the same business concepts differently.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # Case identity
    code: str | None = None
    cluster: str | dict[str, Any] | None = None
    review: str | dict[str, Any] | None = None
    review_code: str | None = None
    case_code: str | None = None
    case: str | dict[str, Any] | None = None
    id: str | int | None = None

    # Comparison credential
    token: str | None = None

    # Chosen original candidate
    representative: Any = None
    choice: Any = None
    candidate: Any = None
    sample: Any = None
    selected: Any = None
    selection: Any = None
    selected_representative: Any = None
    template: Any = None
    vote: Any = None
    track: Any = None
    candidate_code: Any = None
    representative_code: Any = None
    index: int | str | None = None
    candidate_index: int | str | None = None
    choice_index: int | str | None = None

    # Unusable / decision markers
    usable: Any = None
    suitable: Any = None
    accepted: Any = None
    qualified: Any = None
    fit: Any = None
    is_usable: Any = None
    unusable: Any = None
    rejected: Any = None
    reject: Any = None
    is_unusable: Any = None
    decision: Any = None
    verdict: Any = None
    outcome: Any = None
    result: Any = None
    answer: Any = None

    # Arbitration aids
    agree_with: Any = None
    uphold: Any = None
    side: Any = None
    with_voter: Any = None

    # Explicit person identity (normally the JWT subject is authoritative;
    # this is retained for role-level demo/test clients).
    voter: str | None = None
    reviewer: str | None = None
    foreman: str | None = None
    actor: str | None = None
    name: str | None = None
    email: str | None = None
    uid: str | None = None
    user_id: str | None = None
    foreman_id: str | None = None


# Descriptive aliases used by different possible API contracts.
class ReviewCluster(VoteCluster):
    pass


class BallotCluster(VoteCluster):
    pass


class SelectRepresentative(VoteCluster):
    pass


class ChooseRepresentative(VoteCluster):
    pass


class NominateSample(VoteCluster):
    pass


class PickSample(VoteCluster):
    pass


class SubmitVote(VoteCluster):
    pass


class AdjudicateCluster(VoteCluster):
    pass


class ArbitrateCluster(VoteCluster):
    pass


class ResolveCluster(VoteCluster):
    pass


class DecideCluster(VoteCluster):
    pass


# Common request-model names used by alternate contracts/tests.
VoteRequest = VoteCluster
VoteCreate = VoteCluster
BallotRequest = VoteCluster
BallotCreate = VoteCluster
ReviewRequest = ReviewCluster
ReviewCreate = ReviewCluster
NominateRequest = NominateSample
SelectRepresentativeRequest = SelectRepresentative
AdjudicateRequest = AdjudicateCluster
AdjudicateCreate = AdjudicateCluster
ArbitrateRequest = ArbitrateCluster
ResolveRequest = ResolveCluster
