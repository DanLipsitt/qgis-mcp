"""Guards against native crashes from null QgsTextBackgroundSettings SIP wrappers."""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def text_format_safe(plugin_handlers):
    """Import the package-root helper against the stubbed qgis (no live QGIS)."""
    import qgis_mcp_plugin.text_format_safe as mod

    return mod


@pytest.fixture
def system(plugin_handlers):
    class Server(plugin_handlers.system.SystemHandlers):
        LOG_TAG = "test"
        iface = None

    return Server()


@pytest.fixture
def layers(plugin_handlers, monkeypatch):
    class Server(plugin_handlers.layers.LayerHandlers, plugin_handlers.base.HandlerBase):
        LOG_TAG = "test"
        iface = None

    server = Server()
    return server, plugin_handlers


def test_refuse_chained_background_enabled(text_format_safe):
    with pytest.raises(text_format_safe.CommandError, match="format\\(\\)\\.background"):
        text_format_safe.refuse_unsafe_text_background_access(
            "print(layer.labeling().settings().format().background().enabled())"
        )


def test_refuse_split_assignment_then_enabled(text_format_safe):
    code = (
        "fmt = layer.labeling().settings().format()\n"
        "bg = fmt.background()\n"
        "print(bg.enabled())\n"
    )
    with pytest.raises(text_format_safe.CommandError, match="background"):
        text_format_safe.refuse_unsafe_text_background_access(code)


def test_allows_palette_background(text_format_safe):
    # Qt palette().background() has no .format in the chain — must not false-positive.
    text_format_safe.refuse_unsafe_text_background_access(
        "print(widget.palette().background().color().name())"
    )


def test_allows_fresh_background_settings_construction(text_format_safe):
    code = (
        "from qgis.core import QgsTextBackgroundSettings, QgsTextFormat\n"
        "bg = QgsTextBackgroundSettings()\n"
        "bg.setEnabled(True)\n"
        "fmt = QgsTextFormat()\n"
        "fmt.setBackground(bg)\n"
    )
    text_format_safe.refuse_unsafe_text_background_access(code)


def test_syntax_errors_are_left_for_exec(text_format_safe):
    text_format_safe.refuse_unsafe_text_background_access("def broken(")


def test_safe_helper_returns_none_for_null_sip_pointer(text_format_safe, monkeypatch):
    monkeypatch.setattr(text_format_safe, "sip_pointer", lambda obj: None)
    assert text_format_safe.safe_text_background_enabled(MagicMock()) is None


def test_safe_helper_reads_enabled_when_pointers_alive(text_format_safe, monkeypatch):
    bg = MagicMock()
    bg.enabled.return_value = True
    fmt = MagicMock()
    fmt.background.return_value = bg
    monkeypatch.setattr(text_format_safe, "sip_pointer", lambda obj: 0x1000 if obj else None)
    assert text_format_safe.safe_text_background_enabled(fmt) is True
    bg.enabled.assert_called_once_with()


def test_execute_code_refuses_unsafe_probe_before_exec(system, plugin_handlers):
    with pytest.raises(plugin_handlers.base.CommandError, match="background"):
        system.execute_code(
            "layer = QgsProject.instance().mapLayersByName('x')[0]\n"
            "print(layer.labeling().settings().format().background().enabled())\n"
        )


def test_execute_code_exposes_safe_helper(system):
    result = system.execute_code(
        "assert callable(safe_text_background_enabled)\n"
        "print(safe_text_background_enabled(None))\n"
    )
    assert result["executed"] is True
    assert result["stdout"].strip() == "None"


def test_get_layer_labeling_never_calls_background_enabled_unguarded(
    layers, monkeypatch, plugin_handlers
):
    server, handlers = layers
    layer = MagicMock()
    layer.labelsEnabled.return_value = True
    labeling = MagicMock()
    settings = MagicMock()
    text_format = MagicMock()
    text_format.size.return_value = 10
    text_format.color.return_value.name.return_value = "#000000"
    # If the handler called .background().enabled() unguarded, this would be used.
    # We assert the safe helper is what fills background_enabled instead.
    settings.format.return_value = text_format
    settings.fieldName = "name"
    settings.isExpression = False
    settings.placement = "AroundPoint"
    labeling.settings.return_value = settings
    layer.labeling.return_value = labeling
    monkeypatch.setattr(server, "_get_vector_layer", lambda layer_id: layer)
    monkeypatch.setattr(
        handlers.layers,
        "safe_text_background_enabled",
        lambda fmt: None,
    )

    result = server.get_layer_labeling("layer_1")
    assert result["enabled"] is True
    assert result["field_name"] == "name"
    assert result["font_size"] == 10
    assert result["color"] == "#000000"
    assert result["background_enabled"] is None
    text_format.background.assert_not_called()
