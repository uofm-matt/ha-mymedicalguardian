"""pytest fixtures for MyMedicalGuardian tests."""

from collections.abc import Iterator
from unittest.mock import patch

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Iterator[None]:
    """Make the bundled `custom_components.mymedicalguardian` loadable in tests."""
    yield


@pytest.fixture
def bypass_setup_fixture() -> Iterator[None]:
    """Skip the integration's own setup hook so tests that don't need a real
    coordinator can use a bare MockConfigEntry."""
    with patch(
        "custom_components.mymedicalguardian.async_setup_entry", return_value=True
    ):
        yield
