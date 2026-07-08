def pytest_configure(config):
    from django.conf import settings
    if not settings.configured:
        # Single source of truth for this block lives in _codegen_settings.py
        # so the test harness and the contract-emission harness (make
        # contract) can never drift (contract-pipeline.md §3). Tests keep the
        # bare mount + historical REST_FRAMEWORK (unset), exactly as before
        # the extraction.
        from stapel_calendar._codegen_settings import settings_kwargs

        settings.configure(**settings_kwargs())
        import django
        django.setup()

        from stapel_core.comm.schemas import autoload_schemas
        autoload_schemas()


import pytest  # noqa: E402


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient
    return APIClient()


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create_user(
        username="alice", email="alice@example.com", password="x"
    )


@pytest.fixture
def other_user(db):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create_user(
        username="bob", email="bob@example.com", password="x"
    )


@pytest.fixture
def auth_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture(autouse=True)
def _reset_recurrence_presets():
    """Keep the recurrence preset registry clean between tests."""
    from stapel_calendar.recurrence import reset_presets
    reset_presets()
    yield
    reset_presets()


@pytest.fixture
def captured_events():
    """Subscribe to calendar emits (in-process) and collect the Event
    envelopes. Delivery is synchronous with OUTBOX disabled, so the list is
    populated by the time emit() returns."""
    from stapel_core.comm import action_registry, subscribe_action

    collected = []

    def _handler(event):
        collected.append(event)

    names = [
        "calendar.occurrence.materialized",
        "calendar.event.reminder_due",
    ]
    for name in names:
        subscribe_action(name, _handler)
    try:
        yield collected
    finally:
        for name in names:
            handlers = action_registry._subscribers.get(name, [])
            if _handler in handlers:
                handlers.remove(_handler)
