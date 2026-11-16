"""Shared fixtures. Coroutine tests run under ``asyncio.run`` (no pytest-asyncio in the shared dev lock)."""

from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta
from typing import Any

import pytest

from atlas_hostd.config import Config, SleepConfig
from atlas_hostd.models import WakeSource
from atlas_hostd.platform.fake import EPOCH, FakePlatform
from atlas_hostd.service import HostdApp


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        kwargs: dict[str, Any] = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(pyfuncitem.obj(**kwargs))
        return True
    return None


@pytest.fixture
def platform() -> FakePlatform:
    """An ATLAS-woken host: resumed 30 minutes ago from a network wake, nobody at the keyboard."""
    fake = FakePlatform()
    fake.set_resume(EPOCH - timedelta(minutes=30), WakeSource.NETWORK)
    return fake


@pytest.fixture
def config() -> Config:
    return Config(sleep=SleepConfig(sleep_delay_s=0.0))


@pytest.fixture
def app(platform: FakePlatform, config: Config) -> HostdApp:
    return HostdApp(platform, config)
