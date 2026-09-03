import os
import sys

import pytest

# The modules under test (server.py, dataprovider.py) live at the project
# root, not inside a package, so make the project root importable.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    """Run the whole suite with the outbound rate limiter disabled.

    The upstream token bucket (default 20 req/s, i.e. ~50 ms spacing) would
    make tests sleep far too long if left at production settings, so each test
    starts from a disabled limiter.  Individual rate-limit tests override this
    by monkeypatching a specific RateLimiter.
    """
    import dataprovider as dp

    dp._RATE_LIMITER = dp.RateLimiter(0.0)  # disabled: acquire() is a no-op
    yield