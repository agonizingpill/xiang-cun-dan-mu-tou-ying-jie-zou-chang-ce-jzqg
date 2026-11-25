from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Table,
    UniqueConstraint,
)
from datetime import datetime, timezone

from core.db import metadata


cue_clusters = Table('cue_clusters', metadata,
    Column('code', String(64), primary_key=True),
    Column('track', String(160), ForeignKey('cue_tracks.code'), nullable=False),
    Column('representative', String(160), ForeignKey('cue_tracks.code'), nullable=False),
    Column('distance', Float, nullable=False),
    Column('members', JSON, nullable=False),
    Column('token', String(64), nullable=False, unique=True),
)


# A review case freezes the candidate set from one comparison run.  Keeping
# the case and ballots in first-class tables also makes simultaneous retries
# race-safe through the (case, voter) unique constraint.
cue_cluster_comparisons = Table('cue_cluster_comparisons', metadata,
    Column('token', String(64), primary_key=True),
    Column('owner', String(160), nullable=True),
    Column('track', String(160), nullable=True),
    Column('status', String(32), nullable=False),
    Column('run', JSON, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False,
           default=lambda: datetime.now(timezone.utc)),
    Column('updated_at', DateTime(timezone=True), nullable=False,
           default=lambda: datetime.now(timezone.utc),
           onupdate=lambda: datetime.now(timezone.utc)),
)


cue_cluster_reviews = Table('cue_cluster_reviews', metadata,
    Column('code', String(64), primary_key=True),
    Column('token', String(64), nullable=False),
    Column('track', String(160), ForeignKey('cue_tracks.code'), nullable=False),
    Column('candidates', JSON, nullable=False),
    Column('suggested_representative', String(160), nullable=True),
    Column('status', String(32), nullable=False, default='pending'),
    Column('outcome', String(32), nullable=True),
    Column('closed', Boolean, nullable=False, server_default='0', default=False),
    Column('usable', Boolean, nullable=True),
    Column('representative', String(160), nullable=True),
    Column('distance', Float, nullable=True),
    Column('arbiter', String(160), nullable=True),
    Column('decided_at', DateTime(timezone=True), nullable=True),
    Column('created_at', DateTime(timezone=True), nullable=False,
           default=lambda: datetime.now(timezone.utc)),
    Column('updated_at', DateTime(timezone=True), nullable=False,
           default=lambda: datetime.now(timezone.utc),
           onupdate=lambda: datetime.now(timezone.utc)),
)


cue_cluster_votes = Table('cue_cluster_votes', metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('review_code', String(64),
           ForeignKey('cue_cluster_reviews.code', ondelete='CASCADE'),
           nullable=False),
    Column('voter', String(160), nullable=False),
    Column('actor', String(160), nullable=True),
    Column('kind', String(16), nullable=False, server_default='reviewer',
           default='reviewer'),
    Column('choice', String(160), nullable=True),
    Column('usable', Boolean, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False,
           default=lambda: datetime.now(timezone.utc)),
    UniqueConstraint('review_code', 'voter',
                     name='uq_cue_cluster_vote_reviewer'),
)


# Compatibility aliases for code/tests that name these as ballots/reviews.
cue_cluster_review = cue_cluster_reviews
cue_cluster_vote = cue_cluster_votes
cue_cluster_ballot = cue_cluster_votes
cue_cluster_ballots = cue_cluster_votes
cue_cluster_choices = cue_cluster_votes
cue_cluster_signoffs = cue_cluster_votes
cue_cluster_review_votes = cue_cluster_votes
cue_cluster_review_ballots = cue_cluster_votes
cue_cluster_review_cases = cue_cluster_reviews
cue_cluster_signoff_cases = cue_cluster_reviews
cue_cluster_arbitrations = cue_cluster_reviews
