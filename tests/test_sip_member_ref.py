"""SIP member-reference guards: refuse getters on temporaries, copy_member."""

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

    return Server()


@pytest.mark.parametrize(
    "code",
    [
        "print(layer.labeling().settings().format().background().enabled())",
        "bg = settings.format().buffer()",
        "settings.format().shadow()",
        "settings.format().mask()",
        "settings.format().dataDefinedProperties()",
        "layer.labeling().settings().placementSettings().overlapHandling()",
        "layer.labeling().settings().lineSettings()",
        "layer.labeling().settings().pointSettings()",
        "layer.labeling().settings().obstacleSettings()",
        "layer.labeling().settings().thinningSettings()",
        "layer.labeling().settings().dataDefinedProperties()",
    ],
)
def test_refuses_getter_on_a_call_result(sip_member_ref, code):
    with pytest.raises(sip_member_ref.CommandError, match="Refusing execute_code"):
        sip_member_ref.refuse_unsafe_member_refs(code)


def test_refusal_names_the_call_and_the_fix(sip_member_ref):
    with pytest.raises(sip_member_ref.CommandError) as err:
        sip_member_ref.refuse_unsafe_member_refs("s.format().background().enabled()")
    assert "s.format().background()" in str(err.value)
    assert "copy_member(parent, 'background')" in str(err.value)


@pytest.mark.parametrize(
    "code",
    [
        "fmt = settings.format()\nprint(fmt.background().enabled())\n",
        "s = layer.labeling().settings()\ns.placementSettings().overlapHandling()\n",
        "print(copy_member(settings.format(), 'background').enabled())",
        "fmt = QgsTextFormat()\nbg = QgsTextBackgroundSettings()\nfmt.setBackground(bg)\n",
        "print('{}'.format(1))",
    ],
)
def test_allows_getter_on_a_bound_name(sip_member_ref, code):
    sip_member_ref.refuse_unsafe_member_refs(code)


def test_syntax_errors_are_left_for_exec(sip_member_ref):
    sip_member_ref.refuse_unsafe_member_refs("def broken(")


def test_copy_member_returns_a_copy(sip_member_ref):
    class Bg:
        def __init__(self, other=None):
            self.on = True if other is None else other.on

    member = Bg()
    parent = MagicMock()
    parent.background.return_value = member

    copied = sip_member_ref.copy_member(parent, "background")
    assert copied is not member
    assert isinstance(copied, Bg)
    assert copied.on is True


def test_copy_member_rejects_unknown_getter(sip_member_ref):
    with pytest.raises(sip_member_ref.CommandError, match="Unknown member-reference getter"):
        sip_member_ref.copy_member(MagicMock(), "nope")


def test_execute_code_refuses_before_exec(system, plugin_handlers):
    with pytest.raises(plugin_handlers.base.CommandError, match="background"):
        system.execute_code("print(layer.labeling().settings().format().background().enabled())\n")


def test_execute_code_exposes_copy_member(system):
    result = system.execute_code("print(callable(copy_member))\n")
    assert result["executed"] is True
    assert result["stdout"].strip() == "True"


def test_get_layer_labeling_reports_background(layers, monkeypatch):
    layer = MagicMock()
    layer.labelsEnabled.return_value = True
    settings = layer.labeling.return_value.settings.return_value
    settings.fieldName = "name"
    settings.isExpression = False
    settings.placement = "AroundPoint"
    text_format = settings.format.return_value
    text_format.size.return_value = 10
    text_format.color.return_value.name.return_value = "#000000"
    text_format.background.return_value.enabled.return_value = True
    monkeypatch.setattr(layers, "_get_vector_layer", lambda layer_id: layer)

    result = layers.get_layer_labeling("layer_1")
    assert result["background_enabled"] is True
