"""The starter template must keep compiling and honoring the SDK contract —
this is the test new contributors copy for their own connector."""

from __future__ import annotations

from backend.connectors.base import MarketDataConnector
from backend.connectors.base import TestResult as ConnectorTestResult
from backend.connectors.market_data._template import TemplateConnector


def test_template_implements_the_market_data_interface():
    connector = TemplateConnector(config={})
    assert isinstance(connector, MarketDataConnector)

    refresh = connector.refresh_prices([])
    assert set(refresh) == {"prices", "errors"}

    history = connector.fetch_history("1m", ["AAPL"], {})
    assert set(history) == {"history", "errors"}
    assert any("AAPL" in err for err in history["errors"])

    assert connector.quote("AAPL", "stock") is None


def test_template_test_button_asks_for_key_first():
    connector = TemplateConnector(config={})
    result = connector.test()
    assert isinstance(result, ConnectorTestResult)
    assert result.ok is False

    configured = TemplateConnector(config={"api_key": "k"})
    assert configured.test().ok is True


def test_template_is_not_registered_in_the_portal():
    from backend.connectors import registry

    assert not registry.has("_template")
