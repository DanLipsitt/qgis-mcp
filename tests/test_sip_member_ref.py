"""SIP member-ref lifetime guards (format().background/buffer/shadow/mask)."""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def sip_member_ref(plugin_handlers):
    import qgis_mcp_plugin.sip_member_ref as mod

    return mod


@pytest.fixture
def system(plugin_handlers):
    class Server(plugin_handlers.system.SystemHandlers):
        LOG_TAG = "test"
        iface = None

    return Server()


@pytest.fixture
def layers(plugin_handlers):
    class Server(plugin_handlers.layers.LayerHandlers, plugin_handlers.base.HandlerBase):
        LOG_TAG = "test"
        iface = None

    return Server(), plugin_handlers


def test_refuse_chained_background_enabled(sip_member_ref):
    with pytest.raises(sip_member_ref.CommandError, match="background"):
        sip_member_ref.refuse_unsafe_text_format_member_refs(
            "print(layer.labeling().settings().format().background().enabled())"
        )


def test_refuse_all_text_format_member_refs(sip_member_ref):
    for component in ("buffer", "shadow", "mask", "dataDefinedProperties"):
        with pytest.raises(sip_member_ref.CommandError, match="member-reference"):
            sip_member_ref.refuse_unsafe_text_format_member_refs(
                f"print(settings.format().{component}().enabled())"
            )


def test_refuse_split_assignment_then_any_method(sip_member_ref):
    code = (
        "fmt = layer.labeling().settings().format()\n"
        "bg = fmt.background()\n"
        "print(bg.fillColor())\n"
    )
    with pytest.raises(sip_member_ref.CommandError, match="member-reference"):
        sip_member_ref.refuse_unsafe_text_format_member_refs(code)


def test_allows_palette_background(sip_member_ref):
    sip_member_ref.refuse_unsafe_text_format_member_refs(
        "print(widget.palette().background().color().name())"
    )


def test_allows_fresh_settings_construction(sip_member_ref):
    code = (
        "from qgis.core import QgsTextBackgroundSettings, QgsTextFormat\n"
        "bg = QgsTextBackgroundSettings()\n"
        "bg.setEnabled(True)\n"
        "fmt = QgsTextFormat()\n"
        "fmt.setBackground(bg)\n"
    )
    sip_member_ref.refuse_unsafe_text_format_member_refs(code)


def test_syntax_errors_are_left_for_exec(sip_member_ref):
    sip_member_ref.refuse_unsafe_text_format_member_refs("def broken(")


def test_copy_returns_none_for_null_sip_pointer(sip_member_ref, monkeypatch):
    monkeypatch.setattr(sip_member_ref, "sip_pointer", lambda obj: None)
    assert sip_member_ref.copy_text_format_component(MagicMock(), "background") is None


def test_copy_returns_python_owned_instance(sip_member_ref, monkeypatch):
    class Bg:
        def __init__(self, other=None):
            self._enabled = True if other is None else other._enabled

        def enabled(self):
            return self._enabled

    member = Bg()
    fmt = MagicMock()
    fmt.background.return_value = member
    monkeypatch.setattr(sip_member_ref, "sip_pointer", lambda obj: 0x1000 if obj else None)

    copied = sip_member_ref.copy_text_format_component(fmt, "background")
    assert copied is not member
    assert isinstance(copied, Bg)
    assert copied.enabled() is True


def test_copy_rejects_unknown_component(sip_member_ref):
    with pytest.raises(sip_member_ref.CommandError, match="Unknown text-format component"):
        sip_member_ref.copy_text_format_component(MagicMock(), "nope")


def test_execute_code_refuses_unsafe_probe_before_exec(system, plugin_handlers):
    with pytest.raises(plugin_handlers.base.CommandError, match="background"):
        system.execute_code(
            "print(layer.labeling().settings().format().background().enabled())\n"
        )


def test_execute_code_exposes_copy_helper(system):
    result = system.execute_code(
        "assert callable(copy_text_format_component)\n"
        "print(copy_text_format_component(None, 'background'))\n"
    )
    assert result["executed"] is True
    assert result["stdout"].strip() == "None"


def test_get_layer_labeling_uses_owned_copy(layers, monkeypatch, plugin_handlers):
    server, handlers = layers
    layer = MagicMock()
    layer.labelsEnabled.return_value = True
    labeling = MagicMock()
    settings = MagicMock()
    text_format = MagicMock()
    text_format.size.return_value = 10
    text_format.color.return_value.name.return_value = "#000000"
    settings.format.return_value = text_format
    settings.fieldName = "name"
    settings.isExpression = False
    settings.placement = "AroundPoint"
    labeling.settings.return_value = settings
    layer.labeling.return_value = labeling
    monkeypatch.setattr(server, "_get_vector_layer", lambda layer_id: layer)

    class Bg:
        def enabled(self):
            return True

    monkeypatch.setattr(
        handlers.layers,
        "copy_text_format_component",
        lambda fmt, component: Bg() if component == "background" else None,
    )

    result = server.get_layer_labeling("layer_1")
    assert result["background_enabled"] is True
    text_format.background.assert_not_called()
