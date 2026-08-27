"""Compatibility tests for the legacy continuous-panel import path."""

import common.commodity.panel as commodity_panel
import cta_continuous.panel as continuous_panel


def test_legacy_panel_exports_every_shared_public_name():
    assert continuous_panel.__all__ == commodity_panel.__all__


def test_legacy_panel_exports_preserve_object_identity():
    for name in commodity_panel.__all__:
        assert getattr(continuous_panel, name) is getattr(commodity_panel, name)
