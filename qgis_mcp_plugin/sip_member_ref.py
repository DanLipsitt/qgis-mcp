"""Guards for SIP member-reference lifetime bugs in PyQGIS.

SIP copies a ``const T &`` return value into a Python-owned object, but
wraps a non-const ``T &`` as-is: the wrapper points into its parent and
does not keep the parent alive. When the parent is a temporary - e.g. the
Python-owned ``QgsTextFormat`` copy that ``settings.format()`` returns -
it is freed as soon as the expression moves on, and the next method call
on the wrapper reads freed memory. That is a native crash, which
``try``/``except`` cannot catch::

    layer.labeling().settings().format().background().enabled()

Binding the parent to a name keeps it alive, so the same calls are safe
one step at a time. ``copy_member`` takes a Python-owned copy instead.

``MEMBER_REF_ACCESSORS`` lists the non-const reference getters of the
classes ``execute_code`` scripts reach for when inspecting labels. Find
more by grepping QGIS headers for a method returning a non-const ``T &``.
"""

from __future__ import annotations

import ast

from .errors import CommandError

MEMBER_REF_ACCESSORS = {
    "QgsTextFormat": frozenset({"background", "buffer", "shadow", "mask", "dataDefinedProperties"}),
    "QgsPalLayerSettings": frozenset(
        {
            "dataDefinedProperties",
            "lineSettings",
            "obstacleSettings",
            "placementSettings",
            "pointSettings",
            "thinningSettings",
        }
    ),
}
MEMBER_REF_NAMES = frozenset().union(*MEMBER_REF_ACCESSORS.values())

_REFUSAL = (
    "Refusing execute_code: {call}() is called directly on the result of "
    "another call. It returns a C++ reference into that result, which is a "
    "temporary that is freed once the expression moves on; using the "
    "reference can crash QGIS. Bind the parent to a variable first "
    "(fmt = settings.format(); fmt.{name}().enabled()), or take a "
    "Python-owned copy with copy_member(parent, '{name}')."
)


def copy_member(parent, name):
    """Return a Python-owned copy of ``parent.name()``.

    The copy is taken while ``parent`` is held by this call, so it is safe
    even when the caller passes a temporary.
    """
    if name not in MEMBER_REF_NAMES:
        raise CommandError(
            f"Unknown member-reference getter: {name!r}. Use one of {sorted(MEMBER_REF_NAMES)}"
        )
    member = getattr(parent, name)()
    return type(member)(member)


def refuse_unsafe_member_refs(code: str) -> None:
    """Raise CommandError if *code* calls a member-reference getter on a temporary.

    A temporary here is any call result used without being bound to a name:
    ``x.format().background()`` is refused, ``fmt.background()`` is not.
    Syntax errors are left for ``exec`` to report.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        name = node.func.attr
        if name in MEMBER_REF_NAMES and isinstance(node.func.value, ast.Call):
            call = ast.unparse(node.func)
            raise CommandError(_REFUSAL.format(call=call, name=name))
