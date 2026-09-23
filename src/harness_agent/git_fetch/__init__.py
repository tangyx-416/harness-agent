"""Git fetch subsystem for user-approved remote fetch operations.

This package implements v0.9.0 capability: user-approved HTTPS fetch of the
current branch's configured upstream into the local remote-tracking ref.
"""

from .broker import GitFetchBroker
from .models import FetchState, GitFetchPlan, GitFetchResult

__all__ = [
    "GitFetchBroker",
    "GitFetchPlan",
    "GitFetchResult",
    "FetchState",
]
