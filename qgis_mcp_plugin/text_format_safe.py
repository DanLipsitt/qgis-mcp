"""Guards around QgsTextFormat.background() for QGIS 4.2.x.

On QGIS final-4_2_3 (observed 2026-11-23 on macOS ARM),
``labeling().settings().format().background()`` can return a SIP wrapper
whose C++ pointer is null. Calling ``.enabled()`` (or other methods) then
SIGSEGVs the GUI process; Python ``try``/``except`` cannot catch it because
the fault is native.

This module:

* probes the SIP pointer before touching background methods
* refuses ``execute_code`` scripts that call ``.background()`` on a text
  format (or methods on a name bound from that call) before they reach
  ``exec``, so an agent probing labels cannot take down QGIS
"""

from __future__ import annotations

import ast

from .errors import CommandError

# Methods agents commonly call on QgsTextBackgroundSettings. Calling any of
# them on a null wrapper is a native crash on the affected builds.
_BACKGROUND_METHODS = frozenset(
    {
        "enabled",
        "setEnabled",
        "type",
        "setType",
        "sizeType",
        "setSizeType",
        "size",
        "setSize",
        "fillColor",
        "setFillColor",
        "strokeColor",
        "setStrokeColor",
        "strokeWidth",
        "setStrokeWidth",
        "opacity",
        "setOpacity",
        "offset",
        "setOffset",
        "radii",
        "setRadii",
        "joinStyle",
        "setJoinStyle",
        "blendMode",
        "setBlendMode",
        "readXml",
        "writeXml",
        "setFillType",
        "fillType",
        "borderWidthUnits",
        "setBorderWidthUnits",
    }
)

_REFUSAL = (
    "Refusing execute_code that probes QgsTextFormat.background() (or methods "
    "on its return value). On QGIS 4.2.x that accessor can return an invalid "
    "C++ object; calling .enabled() SIGSEGVs the whole GUI and is not "
    "catchable from Python. Use get_layer_labeling for label config, or the "
    "injected safe_text_background_enabled(text_format) helper. To *set* a "
    "background, construct a fresh QgsTextBackgroundSettings and pass it to "
    "setBackground() — do not call format().background()."
)


def _sip_module():
    try:
        from qgis.PyQt import sip
    except ImportError:
        try:
            import sip  # type: ignore[no-redef]
        except ImportError:
            return None
    return sip


def sip_pointer(obj) -> int | None:
    """Return the underlying C++ pointer for *obj*, or None if unknown/null."""
    if obj is None:
        return None
    sip = _sip_module()
    if sip is None:
        return None
    isdeleted = getattr(sip, "isdeleted", None)
    if callable(isdeleted):
        try:
            if isdeleted(obj):
                return None
        except Exception:
            return None
    unwrap = getattr(sip, "unwrapinstance", None)
    if not callable(unwrap):
        return None
    try:
        ptr = unwrap(obj)
    except Exception:
        return None
    if ptr is None or ptr == 0:
        return None
    # Some SIP builds return a void* capsule / int; treat falsy as null.
    try:
        if int(ptr) == 0:
            return None
    except (TypeError, ValueError):
        pass
    return ptr


def safe_text_background_enabled(text_format) -> bool | None:
    """Return whether a text background is enabled, or None if unsafe to ask.

    Never calls methods on a background wrapper whose SIP pointer is null —
    that path SIGSEGVs on QGIS 4.2.x. Callers must treat None as "unknown".
    """
    if text_format is None:
        return None
    if sip_pointer(text_format) is None:
        # format() itself can be a dead wrapper; do not call .background().
        return None
    background = getattr(text_format, "background", None)
    if not callable(background):
        return None
    try:
        bg = background()
    except Exception:
        return None
    if sip_pointer(bg) is None:
        return None
    enabled = getattr(bg, "enabled", None)
    if not callable(enabled):
        return None
    try:
        return bool(enabled())
    except Exception:
        return None


def _attr_names_outward(node):
    """Yield attribute names walking from the call site toward the root."""
    while True:
        if isinstance(node, ast.Call):
            node = node.func
            continue
        if isinstance(node, ast.Attribute):
            yield node.attr
            node = node.value
            continue
        break


def _is_format_call(node: ast.AST) -> bool:
    """True when *node* is a Call to an attribute named ``format``."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    return node.func.attr == "format"


def _is_format_background_call(node: ast.AST, format_names=frozenset()) -> bool:
    """True when *node* is a Call to ``.background(...)`` on a text format.

    Matches ``settings.format().background()`` and ``fmt.background()`` when
    ``fmt`` was bound from a ``.format()`` call. ``palette().background()``
    (Qt) is left alone: it has no ``format`` in the chain and the receiver is
    not a format-bound name.
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr != "background":
        return False
    receiver = node.func.value
    if isinstance(receiver, ast.Name) and receiver.id in format_names:
        return True
    return "format" in list(_attr_names_outward(receiver))


def _assignment_target_names(target: ast.AST):
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _assignment_target_names(elt)


def _bound_names_from(tree, predicate):
    """Names assigned (or walrus-bound) from an expression matching *predicate*."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and predicate(node.value):
            for target in node.targets:
                names.update(_assignment_target_names(target))
            continue
        is_annotated = isinstance(node, (ast.AnnAssign, ast.NamedExpr))
        if is_annotated and node.value is not None and predicate(node.value):
            names.update(_assignment_target_names(node.target))
    return names


def _call_on_background_result(node: ast.AST, is_bg_call) -> bool:
    """``format().background().enabled()`` — method Call on a background() Call."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    return isinstance(node.func.value, ast.Call) and is_bg_call(node.func.value)


def _call_on_tainted_name(node: ast.AST, tainted) -> bool:
    """``bg.enabled()`` after ``bg = format().background()``."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in _BACKGROUND_METHODS:
        return False
    receiver = node.func.value
    return isinstance(receiver, ast.Name) and receiver.id in tainted


def refuse_unsafe_text_background_access(code: str) -> None:
    """Raise CommandError if *code* would call into QgsTextFormat.background().

    Catches both chained forms (``format().background().enabled()``) and the
    split form (``fmt = format(); bg = fmt.background(); bg.enabled()``).
    Syntax errors are left for ``exec`` to report.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return

    format_names = _bound_names_from(tree, _is_format_call)

    def is_bg_call(node):
        return _is_format_background_call(node, format_names)

    tainted = _bound_names_from(tree, is_bg_call)

    for node in ast.walk(tree):
        if is_bg_call(node):
            raise CommandError(_REFUSAL)
        if _call_on_background_result(node, is_bg_call):
            raise CommandError(_REFUSAL)
        if _call_on_tainted_name(node, tainted):
            raise CommandError(_REFUSAL)
