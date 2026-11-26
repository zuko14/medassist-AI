"""Repo-wide test setup.

The CallMedex master switch (migration 158, app/integrations/callmedex/switch.py) is OFF by default in production.
The existing CallMedex suites test the integration's behaviour once the owner has switched it on, so every test
runs with the switch ON unless it is marked `callmedex_switch_real` (tests/test_callmedex_switch.py), which then
exercises the real read path and the OFF default.
"""

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "callmedex_switch_real: use the real CallMedex owner switch (default OFF)")


@pytest.fixture(autouse=True)
def _callmedex_switch_on(request):
    if request.node.get_closest_marker("callmedex_switch_real"):
        yield
        return
    from app.integrations.callmedex import switch
    saved = dict(switch._cache)
    switch._cache.update(on=True, at=float("inf"), row={"enabled": True})   # at=inf: never refreshed in the test
    yield
    switch._cache.clear()
    switch._cache.update(saved)
