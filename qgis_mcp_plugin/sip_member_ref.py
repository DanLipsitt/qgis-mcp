"""Guards for SIP member-reference lifetime bugs in PyQGIS.

Some QGIS C++ APIs return a non-const ``T &`` into a parent object (for
example ``QgsTextFormat.background()`` → ``QgsTextBackgroundSettings &``).
When that parent was itself a temporary — ``labeling.settings()`` returns
``QgsPalLayerSettings`` **by value** — SIP can hand Python a wrapper whose
C++ pointer outlives (or is nullified with) the temporary. Calling any
method on it SIGSEGVs; ``try``/``except`` cannot catch it.

On ``QgsTextFormat`` the closed set of such accessors is ``background``,
``buffer``, ``shadow``, ``mask``, and ``dataDefinedProperties`` (by-value
const overloads exist in C++ but are ``SIP_SKIP``'d). Related but separate:
keep a ``QgsExpression`` alive while walking ``rootNode()`` (see
``handlers/features.py``).

Plugin and helper code should bind parents (or use
``copy_text_format_component`` for a Python-owned copy) and then call
ordinary methods. ``execute_code`` runs
``refuse_unsafe_text_format_member_refs`` before ``exec`` and injects
``copy_text_format_component`` into the script namespace.
"""

from __future__ import annotations

import ast

from .errors import CommandError

# Accessors on QgsTextFormat that return a non-const member reference into the
# format. Python only sees these; the by-value/const overloads are SIP_SKIP.
# Discover more across QGIS by grepping headers for:
#   T &foo();
#   SIP_SKIP T foo() const { return mFoo; }
TEXT_FORMAT_MEMBER_REFS = frozenset(
    {"background", "buffer", "shadow", "mask", "dataDefinedProperties"}
)

_REFUSAL = (
    "Refusing execute_code that calls QgsTextFormat member-reference "
    "accessors (background/buffer/shadow/mask/dataDefinedProperties) or "
    "methods on a name bound from those calls. Those return a C++ member "
    "reference; when the format came from a temporary (e.g. "
    "labeling.settings() by value), the SIP wrapper can be invalid and any "
    "method call can SIGSEGV the GUI. Bind and keep parents alive, or use "
    "copy_text_format_component(fmt, '<component>') for a Python-owned "
    "copy, then call normal methods. To *set* a component, construct a "
    "fresh settings object and setBackground/setBuffer/… — do not mutate "
    "through format().background()."
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
    try:
        if int(ptr) == 0:
            return None
    except (TypeError, ValueError):
        pass
    return ptr


def copy_text_format_component(text_format, component):
    """Return a Python-owned copy of a text-format member, or None.

    ``component`` is one of ``TEXT_FORMAT_MEMBER_REFS``. Callers then use
    ordinary methods (``.enabled()``, etc.) on the copy. Returns None when
    the format or the member-ref wrapper is not safe to touch.
    """
    if component not in TEXT_FORMAT_MEMBER_REFS:
        raise CommandError(
            f"Unknown text-format component: {component!r}. "
            f"Use one of {sorted(TEXT_FORMAT_MEMBER_REFS)}"
        )
    if text_format is None or sip_pointer(text_format) is None:
        return None
    getter = getattr(text_format, component, None)
    if not callable(getter):
        return None
    try:
        member = getter()
    except Exception:
        return None
    if sip_pointer(member) is None:
        return None
    # Copy-construct so the returned object is owned by Python, not a
    # dangling member reference into a temporary QgsTextFormat.
    try:
        return type(member)(member)
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
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    return node.func.attr == "format"


def _is_format_member_ref_call(node: ast.AST, format_names=frozenset()) -> bool:
    """True when *node* is ``.background/buffer/shadow/mask()`` on a text format.

    ``palette().background()`` (Qt) is left alone: no ``format`` in the chain
    and the receiver is not a format-bound name.
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in TEXT_FORMAT_MEMBER_REFS:
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


def _call_on_member_ref_result(node: ast.AST, is_member_ref) -> bool:
    """``format().background().enabled()`` — method Call on a member-ref Call."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    return isinstance(node.func.value, ast.Call) and is_member_ref(node.func.value)


def _call_on_tainted_name(node: ast.AST, tainted) -> bool:
    """Any method call on a name bound from ``format().background()`` (etc.)."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    receiver = node.func.value
    return isinstance(receiver, ast.Name) and receiver.id in tainted


def refuse_unsafe_text_format_member_refs(code: str) -> None:
    """Raise CommandError if *code* takes a SIP member-ref from QgsTextFormat.

    Covers ``format().background/buffer/shadow/mask()`` and the split form
    ``fmt = format(); bg = fmt.background(); bg.<anything>()``. Syntax errors
    are left for ``exec`` to report.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return

    format_names = _bound_names_from(tree, _is_format_call)

    def is_member_ref(node):
        return _is_format_member_ref_call(node, format_names)

    tainted = _bound_names_from(tree, is_member_ref)

    for node in ast.walk(tree):
        if is_member_ref(node):
            raise CommandError(_REFUSAL)
        if _call_on_member_ref_result(node, is_member_ref):
            raise CommandError(_REFUSAL)
        if _call_on_tainted_name(node, tainted):
            raise CommandError(_REFUSAL)
