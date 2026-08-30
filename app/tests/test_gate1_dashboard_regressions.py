"""Gate 1 source regressions for the framework-free dashboard.

Normal runs record known bugs as expected failures. ``SANAD_GATE1_STRICT=1``
exposes raw failures during the reshape. A fixed contract is an unexpected
success until its marker is deliberately removed.
"""

from __future__ import annotations

import functools
import inspect
import os
import re
import sys
import unittest
from pathlib import Path
from typing import Callable


APP_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (APP_ROOT / "web" / "dashboard.html").read_text(encoding="utf-8")

_SCRIPT_BLOCKS = list(re.finditer(
    r"<script(?:\s[^>]*)?>(?P<body>.*?)</script\s*>",
    DASHBOARD,
    re.IGNORECASE | re.DOTALL,
))
if len(_SCRIPT_BLOCKS) != 1:
    raise AssertionError(
        "dashboard source contract requires exactly one inline script; "
        f"found {len(_SCRIPT_BLOCKS)}"
    )
JAVASCRIPT = _SCRIPT_BLOCKS[0].group("body")


def _mask_javascript(source: str, *, strings: bool) -> str:
    """Offset-preserving JS lexer for comments, strings, regexes and templates."""
    out = list(source)
    prefix_words = {
        "await", "case", "delete", "do", "else", "in", "instanceof",
        "new", "of", "return", "throw", "typeof", "void", "yield",
    }

    def blank(left: int, right: int) -> None:
        for at in range(left, right):
            if source[at] != "\n":
                out[at] = " "

    def quoted(at: int, quote: str) -> int:
        cursor = at + 1
        while cursor < len(source):
            if source[cursor] == "\\":
                cursor += 2
            elif source[cursor] == quote:
                return cursor + 1
            elif source[cursor] == "\n":
                break
            else:
                cursor += 1
        raise AssertionError("unterminated JavaScript string literal")

    def regex_literal(at: int) -> int:
        cursor, in_class = at + 1, False
        while cursor < len(source) and source[cursor] != "\n":
            char = source[cursor]
            if char == "\\":
                cursor += 2
                continue
            in_class = True if char == "[" else False if char == "]" else in_class
            if char == "/" and not in_class:
                cursor += 1
                while cursor < len(source) and source[cursor].isalpha():
                    cursor += 1
                return cursor
            cursor += 1
        raise AssertionError("unterminated JavaScript regex literal")

    def template(at: int) -> int:
        if strings:
            blank(at, at + 1)
        cursor = at + 1
        while cursor < len(source):
            if source[cursor] == "\\":
                if strings:
                    blank(cursor, min(cursor + 2, len(source)))
                cursor += 2
            elif source[cursor] == "`":
                if strings:
                    blank(cursor, cursor + 1)
                return cursor + 1
            elif source.startswith("${", cursor):
                if strings:
                    blank(cursor, cursor + 1)
                cursor = code(cursor + 2, interpolation=True)
            else:
                if strings:
                    blank(cursor, cursor + 1)
                cursor += 1
        raise AssertionError("unterminated JavaScript template literal")

    def code(at: int, *, interpolation: bool = False) -> int:
        cursor, depth, regex_ok = at, int(interpolation), True
        while cursor < len(source):
            char = source[cursor]
            if char.isspace():
                cursor += 1
            elif source.startswith("//", cursor):
                left = cursor
                end = source.find("\n", cursor + 2)
                cursor = len(source) if end < 0 else end
                blank(left, cursor)
            elif source.startswith("/*", cursor):
                end = source.find("*/", cursor + 2)
                if end < 0:
                    raise AssertionError("unterminated JavaScript block comment")
                blank(cursor, end + 2)
                cursor = end + 2
            elif char in "'\"":
                end = quoted(cursor, char)
                if strings:
                    blank(cursor, end)
                cursor, regex_ok = end, False
            elif char == "`":
                cursor, regex_ok = template(cursor), False
            elif char == "/" and regex_ok:
                end = regex_literal(cursor)
                if strings:
                    blank(cursor, end)
                cursor, regex_ok = end, False
            elif char.isalpha() or char in "_$":
                end = cursor + 1
                while end < len(source) and (source[end].isalnum() or source[end] in "_$"):
                    end += 1
                regex_ok, cursor = source[cursor:end] in prefix_words, end
            elif char.isdigit():
                end = cursor + 1
                while end < len(source) and (source[end].isalnum() or source[end] in ".xX_"):
                    end += 1
                cursor, regex_ok = end, False
            elif interpolation and char == "{":
                depth, cursor, regex_ok = depth + 1, cursor + 1, True
            elif interpolation and char == "}":
                depth, cursor, regex_ok = depth - 1, cursor + 1, False
                if depth == 0:
                    return cursor
            else:
                regex_ok = char not in ")]}."
                cursor += 1
        if interpolation:
            raise AssertionError("unterminated JavaScript template expression")
        return cursor

    code(0)
    return "".join(out)


def _without_comments(source: str) -> str:
    return _mask_javascript(source, strings=False)


def _code_only(source: str) -> str:
    return _mask_javascript(source, strings=True)


def _matching(code: str, opening: int, left: str, right: str) -> int:
    if opening >= len(code) or code[opening] != left:
        raise AssertionError(f"expected {left!r} at JavaScript offset {opening}")
    depth = 0
    for cursor in range(opening, len(code)):
        if code[cursor] == left:
            depth += 1
        elif code[cursor] == right:
            depth -= 1
            if depth == 0:
                return cursor
    raise AssertionError(f"unterminated JavaScript {left}{right} block")


def _unique_function(name: str) -> str:
    functions = _named_functions()
    if name not in functions:
        raise AssertionError(f"expected one function {name}, found 0")
    return functions[name]


def _element_calls(source: str, element_id: str) -> list[tuple[int, int]]:
    """Return offset spans for real ``el(<id>)`` calls, excluding literals."""
    code = _code_only(source)
    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"\bel\s*\(", code):
        opening = code.find("(", match.start())
        closing = _matching(code, opening, "(", ")")
        call = source[match.start():closing + 1]
        if _string_values(call) == [element_id]:
            spans.append((match.start(), closing + 1))
    return spans


def _string_values(source: str) -> list[str]:
    """Return live JavaScript string contents; comments have already vanished."""
    live = _without_comments(source)
    values: list[str] = []
    i = 0
    while i < len(live):
        if live[i] not in ("'", '"', "`"):
            i += 1
            continue
        quote = live[i]
        i += 1
        value: list[str] = []
        while i < len(live):
            if live[i] == "\\" and i + 1 < len(live):
                value.extend((live[i], live[i + 1]))
                i += 2
                continue
            if live[i] == quote:
                i += 1
                break
            value.append(live[i])
            i += 1
        values.append("".join(value))
    return values


def _rejecting_guards(source: str) -> list[tuple[int, str]]:
    """Return ``if`` conditions whose branch rejects with throw or return."""
    code = _code_only(source)
    guards: list[tuple[int, str]] = []
    for match in re.finditer(r"\bif\s*\(", code):
        opening = code.find("(", match.start())
        closing = _matching(code, opening, "(", ")")
        cursor = closing + 1
        while cursor < len(code) and code[cursor].isspace():
            cursor += 1
        if cursor < len(code) and code[cursor] == "{":
            branch_end = _matching(code, cursor, "{", "}")
            branch = code[cursor + 1:branch_end]
        else:
            branch_end = code.find(";", cursor)
            branch_end = len(code) if branch_end < 0 else branch_end
            branch = code[cursor:branch_end]
        if re.search(r"\b(?:throw|return)\b", branch):
            guards.append((match.start(), code[opening + 1:closing]))
    return guards


def _function_body(function_source: str) -> str:
    code = _code_only(function_source)
    opening_paren = code.find("(")
    closing_paren = _matching(code, opening_paren, "(", ")")
    opening_brace = code.find("{", closing_paren + 1)
    closing_brace = _matching(code, opening_brace, "{", "}")
    return function_source[opening_brace + 1:closing_brace]


def _named_functions() -> dict[str, str]:
    """Index real top-level-style named declarations; duplicates fail closed."""
    code = _code_only(JAVASCRIPT)
    found: dict[str, str] = {}
    for match in re.finditer(
        r"\b(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\(",
        code,
    ):
        name = match.group("name")
        opening_paren = code.find("(", match.start())
        closing_paren = _matching(code, opening_paren, "(", ")")
        opening_brace = code.find("{", closing_paren + 1)
        closing_brace = _matching(code, opening_brace, "{", "}")
        if name in found:
            raise AssertionError(f"duplicate named JavaScript function {name}")
        found[name] = JAVASCRIPT[match.start():closing_brace + 1]
    return found


def _named_call_counts(source: str, names: set[str]) -> dict[str, int]:
    code = _code_only(source)
    return {
        name: len(re.findall(
            rf"(?<![\w$.]){re.escape(name)}\s*\(", code
        ))
        for name in names
        if re.search(rf"(?<![\w$.]){re.escape(name)}\s*\(", code)
    }


def _reachable_graph(root: str) -> tuple[dict[str, str], set[str], dict[str, dict[str, int]]]:
    functions = _named_functions()
    if root not in functions:
        raise AssertionError(f"required JavaScript entrypoint {root} is missing")
    names = set(functions)
    edges = {
        name: _named_call_counts(_function_body(body), names)
        for name, body in functions.items()
    }
    reachable: set[str] = set()
    pending = [root]
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        pending.extend(edges[name])
    return functions, reachable, edges


def _path_count(
    root: str,
    target: str,
    reachable: set[str],
    edges: dict[str, dict[str, int]],
) -> int:
    can_reach = {target}
    while True:
        prior = len(can_reach)
        can_reach |= {
            caller for caller in reachable
            if any(callee in can_reach for callee in edges[caller])
        }
        if len(can_reach) == prior:
            break
    if root not in can_reach:
        return 0
    def count(name: str, trail: set[str]) -> int:
        if name == target:
            return 1
        if name in trail:
            raise AssertionError("recursive snapshot call path is not auditable")
        return sum(
            multiplicity * count(callee, trail | {name})
            for callee, multiplicity in edges[name].items()
            if callee in can_reach
        )
    return count(root, set())


def _split_arguments(source: str, opening: int, closing: int) -> list[str]:
    if not source[opening + 1:closing].strip():
        return []
    code = _code_only(source)
    starts = [opening + 1]
    parens = brackets = braces = 0
    for cursor in range(opening + 1, closing):
        char = code[cursor]
        if char == "(":
            parens += 1
        elif char == ")":
            parens -= 1
        elif char == "[":
            brackets += 1
        elif char == "]":
            brackets -= 1
        elif char == "{":
            braces += 1
        elif char == "}":
            braces -= 1
        elif char == "," and parens == brackets == braces == 0:
            starts.append(cursor + 1)
    ends = [start - 1 for start in starts[1:]] + [closing]
    return [source[start:end].strip() for start, end in zip(starts, ends)]


def _call_records(source: str) -> list[tuple[str, list[str], int, int]]:
    """Return real calls with callee, arguments and source offsets."""
    code = _code_only(source)
    records: list[tuple[str, list[str], int, int]] = []
    pattern = re.compile(
        r"(?<![\w$.])(?P<callee>[A-Za-z_$][\w$]*"
        r"(?:\s*\.\s*[A-Za-z_$][\w$]*)*)\s*\("
    )
    excluded = {"catch", "for", "function", "if", "switch", "while", "with"}
    for match in pattern.finditer(code):
        callee = re.sub(r"\s+", "", match.group("callee"))
        if callee in excluded:
            continue
        opening = code.find("(", match.start())
        closing = _matching(code, opening, "(", ")")
        records.append((
            callee, _split_arguments(source, opening, closing),
            match.start(), closing + 1,
        ))
    return records


def _rhs_after(source: str, equals: int) -> str:
    """Extract one assignment RHS through its top-level semicolon."""
    code = _code_only(source)
    parens = brackets = braces = 0
    for cursor in range(equals + 1, len(code)):
        char = code[cursor]
        if char == "(":
            parens += 1
        elif char == ")":
            parens -= 1
        elif char == "[":
            brackets += 1
        elif char == "]":
            brackets -= 1
        elif char == "{":
            braces += 1
        elif char == "}":
            braces -= 1
        elif char == ";" and parens == brackets == braces == 0:
            return source[equals + 1:cursor]
    raise AssertionError("JavaScript assignment is missing its semicolon")


def _binding_map(source: str) -> dict[str, list[str]]:
    """Collect every simple binding/rebinding for conservative data flow."""
    code = _code_only(source)
    bindings: dict[str, list[str]] = {}
    pattern = re.compile(
        r"(?<![\w$.])(?P<name>[A-Za-z_$][\w$]*)\s*"
        r"(?P<op>\+=|-=|\*=|/=|=(?!=|>))"
    )
    excluded = {"case", "const", "let", "return", "throw", "var"}
    for match in pattern.finditer(code):
        name = match.group("name")
        if name in excluded:
            continue
        bindings.setdefault(name, []).append(_rhs_after(source, match.end("op") - 1))
    return bindings


def _dependency_fragments(source: str, expression: str) -> list[str]:
    """Trace local aliases/rebinds and reachable zero-argument helper returns."""
    fragments: list[str] = []
    expanded_vars: set[tuple[int, str]] = set()
    expanded_helpers: set[str] = set()
    functions = _named_functions()

    def visit(context: str, fragment: str) -> None:
        fragments.append(fragment)
        code = _code_only(fragment)
        for name, values in _binding_map(context).items():
            key = (id(context), name)
            if key not in expanded_vars and re.search(
                rf"(?<![\w$.]){re.escape(name)}\b", code
            ):
                expanded_vars.add(key)
                for value in values:
                    visit(context, value)
        for callee, args, _, _ in _call_records(fragment):
            if callee not in functions or args or callee in expanded_helpers:
                continue
            expanded_helpers.add(callee)
            body = _function_body(functions[callee])
            body_code = _code_only(body)
            returns = list(re.finditer(r"\breturn\b", body_code))
            if len(returns) != 1:
                raise AssertionError(
                    f"helper {callee} needs one auditable return expression"
                )
            visit(body, _rhs_after(body, returns[0].end() - 1))

    visit(source, expression)
    return fragments


def _property_in(fragments: list[str], property_name: str) -> bool:
    for fragment in fragments:
        if re.search(
            rf"(?:\?\.|\.)\s*{re.escape(property_name)}\b",
            _code_only(fragment),
        ):
            return True
        if re.search(
            rf"\[\s*(['\"]){re.escape(property_name)}\1\s*\]",
            _without_comments(fragment),
        ):
            return True
    return False


def _root_identifier_in(fragments: list[str], name: str) -> bool:
    return any(
        re.search(rf"(?<![\w$.]){re.escape(name)}\b", _code_only(fragment))
        is not None
        for fragment in fragments
    )


def _dom_sink_expressions(
    function_source: str, id_matches: Callable[[str], bool]
) -> list[tuple[str, str]]:
    """Find text/HTML/value assignments to matching ``el(id)`` nodes/aliases."""
    code = _code_only(function_source)
    element_spans: list[tuple[int, int, str]] = []
    for match in re.finditer(r"\bel\s*\(", code):
        opening = code.find("(", match.start())
        closing = _matching(code, opening, "(", ")")
        values = _string_values(function_source[match.start():closing + 1])
        if len(values) == 1 and id_matches(values[0]):
            element_spans.append((match.start(), closing + 1, values[0]))

    aliases: dict[str, str] = {}
    for start, _, element_id in element_spans:
        prefix = code[max(0, start - 100):start]
        assigned = re.search(r"(?P<name>[A-Za-z_$][\w$]*)\s*=\s*$", prefix)
        if assigned:
            aliases[assigned.group("name")] = element_id

    sinks: list[tuple[str, str]] = []
    for _, after_call, element_id in element_spans:
        suffix = re.match(
            r"\s*\.\s*(?:innerHTML|textContent|value)\s*(?P<eq>=(?!=|>))",
            code[after_call:],
        )
        if suffix:
            equals = after_call + suffix.start("eq")
            sinks.append((element_id, _rhs_after(function_source, equals)))
    for alias, element_id in aliases.items():
        for match in re.finditer(
            rf"(?<![\w$.]){re.escape(alias)}\s*\.\s*"
            r"(?:innerHTML|textContent|value)\s*(?P<eq>=(?!=|>))",
            code,
        ):
            sinks.append((element_id, _rhs_after(function_source, match.start("eq"))))
    return sinks


def _callback_at(source: str, start: int) -> str:
    """Extract an inline function/arrow callback, or resolve a named one."""
    code = _code_only(source)
    cursor = start
    while cursor < len(code) and code[cursor].isspace():
        cursor += 1
    async_match = re.match(r"async\b", code[cursor:])
    if async_match:
        cursor += async_match.end()
        while cursor < len(code) and code[cursor].isspace():
            cursor += 1
    function_match = re.match(r"function\s*\(", code[cursor:])
    if function_match:
        opening = code.find("(", cursor)
        closing = _matching(code, opening, "(", ")")
        opening_brace = code.find("{", closing + 1)
        closing_brace = _matching(code, opening_brace, "{", "}")
        return source[opening_brace + 1:closing_brace]

    named = re.match(r"(?P<name>[A-Za-z_$][\w$]*)\b", code[cursor:])
    if named:
        name = named.group("name")
        functions = _named_functions()
        if name in functions:
            return _function_body(functions[name])
    arrow = re.search(r"=>", code[cursor:])
    if arrow:
        arrow_at = cursor + arrow.start()
        opening_brace = code.find("{", arrow_at + 2)
        if opening_brace < 0:
            return source[arrow_at + 2:].strip()
        closing_brace = _matching(code, opening_brace, "{", "}")
        return source[opening_brace + 1:closing_brace]
    raise AssertionError("reset event callback is not statically auditable")


def _unique_event_handler(element_id: str) -> str:
    """Accept one onclick or click-listener callback for the named control."""
    code = _code_only(JAVASCRIPT)
    handlers: list[str] = []
    for _, after_call in _element_calls(JAVASCRIPT, element_id):
        onclick = re.match(
            r"\s*\.\s*onclick\s*=", code[after_call:]
        )
        if onclick:
            handlers.append(_callback_at(JAVASCRIPT, after_call + onclick.end()))
            continue

        listener = re.match(
            r"\s*\.\s*addEventListener\s*\(", code[after_call:]
        )
        if listener:
            opening = after_call + listener.end() - 1
            closing = _matching(code, opening, "(", ")")
            args = _split_arguments(JAVASCRIPT, opening, closing)
            if len(args) >= 2 and _string_values(args[0]) == ["click"]:
                handlers.append(_callback_at(args[1], 0))
    if len(handlers) != 1:
        raise AssertionError(
            f"expected one click handler for {element_id}, found {len(handlers)}"
        )
    return handlers[0]


def _handler_reachable_sources(handler: str) -> list[str]:
    """Follow bare calls from an event body into named local wrappers."""
    functions = _named_functions()
    names = set(functions)
    sources = [handler]
    pending = list(_named_call_counts(handler, names))
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        body = _function_body(functions[name])
        sources.append(body)
        pending.extend(_named_call_counts(body, names))
    return sources


def gate1_live_bug(test: Callable) -> Callable:
    """Expect assertion failures only; fixture/runtime errors remain hard errors."""
    if os.environ.get("SANAD_GATE1_STRICT") == "1":
        return test

    def record(case: unittest.TestCase, error=None) -> None:
        outcome = case._outcome
        if outcome is None:
            raise RuntimeError("Gate 1 outcome is unavailable")
        if error is None:
            if outcome.success:
                case._addUnexpectedSuccess(outcome.result)
        else:
            case._addExpectedFailure(outcome.result, error)
        outcome.success = False

    if inspect.iscoroutinefunction(test):
        @functools.wraps(test)
        async def async_wrapper(case, *args, **kwargs):
            try:
                await test(case, *args, **kwargs)
            except case.failureException:
                record(case, sys.exc_info())
            else:
                record(case)
        return async_wrapper

    @functools.wraps(test)
    def wrapper(case, *args, **kwargs):
        try:
            test(case, *args, **kwargs)
        except case.failureException:
            record(case, sys.exc_info())
        else:
            record(case)
    return wrapper


class OneTruthfulDashboardSnapshot(unittest.TestCase):
    @gate1_live_bug
    def test_dashboard_state_is_committed_from_one_versioned_snapshot(self) -> None:
        functions, reachable, edges = _reachable_graph("poll")
        bodies = {name: _function_body(functions[name]) for name in reachable}
        legacy_paths = (
            "/health", "/board", "/cards", "/feed", "/patient/",
            "/reports", "/settings", "/summary",
        )
        old_reads = [
            f"{name}:{value}"
            for name, body in bodies.items()
            for value in _string_values(body)
            if any(value.startswith(path) for path in legacy_paths)
        ]
        self.assertEqual(
            [], old_reads,
            "poll still reaches independent workspace reads: " + ", ".join(old_reads),
        )

        snapshot_calls: list[tuple[str, str]] = []
        for name, body in bodies.items():
            for callee, args, _, _ in _call_records(body):
                if args and any(
                    value.startswith("/api/v2/workspace-snapshot")
                    for value in _string_values(args[0])
                ):
                    snapshot_calls.append((name, callee))
        self.assertEqual(
            1, len(snapshot_calls),
            "poll must reach exactly one workspace-snapshot network call",
        )
        loader = snapshot_calls[0][0]
        self.assertEqual(
            1, _path_count("poll", loader, reachable, edges),
            "poll invokes its workspace-snapshot loader more than once",
        )

        fields = ("snapshot_id", "schema_version", "as_of")
        validators: set[str] = set()
        for name, body in bodies.items():
            guards = [condition for _, condition in _rejecting_guards(body)]
            if all(any(
                    re.search(rf"\b{field}\b", condition)
                    and (
                        re.search(rf"!\s*[^|&;]*\b{field}\b", condition)
                        or re.search(
                            rf"\b{field}\b\s*={2,3}\s*(?:null|undefined)\b",
                            condition,
                        )
                        or re.search(
                            rf"\btypeof\b[^;]*\b{field}\b[^;]*!=", condition
                        )
                    )
                    for condition in guards
                ) for field in fields):
                validators.add(name)
        self.assertTrue(validators, "no reachable validator rejects all snapshot metadata")

        commit_owners: list[str] = []
        legacy_fields = (
            "board", "cards", "summary", "events", "reports", "settings",
            "monitor", "patient", "since",
        )
        for name, body in bodies.items():
            code = _code_only(body)
            commit_owners.extend([name] * len(re.findall(
                r"\bS\s*\.\s*(?:workspaceSnapshot|workspace_snapshot|snapshot)"
                r"\s*=\s*[A-Za-z_$][\w$]*\s*;"
                r"|\bObject\s*\.\s*assign\s*\(\s*S\s*,\s*"
                r"[A-Za-z_$][\w$]*\s*\)",
                code,
            )))
            self.assertNotRegex(
                code,
                rf"\bS\s*\.\s*(?:{'|'.join(legacy_fields)})\s*=",
                f"{name} still writes snapshot components piecemeal",
            )
        self.assertEqual(
            1, len(commit_owners),
            "poll must reach one whole-object WorkspaceSnapshot commit",
        )
        self.assertEqual(
            1, _path_count("poll", commit_owners[0], reachable, edges),
            "poll invokes its atomic snapshot commit more than once",
        )

        poll = bodies["poll"]
        poll_code = _code_only(poll)
        loader_call = loader if loader != "poll" else snapshot_calls[0][1]
        orchestration: list[tuple[str, int, int]] = []
        for binding in re.finditer(
            r"\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=(?!=|>)",
            poll_code,
        ):
            name = binding.group("name")
            equals = binding.end() - 1
            rhs = _rhs_after(poll, equals)
            statement_end = equals + 1 + len(rhs)
            rhs_calls = _call_records(rhs)
            if not any(callee == loader_call for callee, _, _, _ in rhs_calls):
                continue
            validation_positions = [
                statement_end for callee, _, _, _ in rhs_calls if callee in validators
            ]
            validation_positions.extend(
                start for callee, args, start, _ in _call_records(poll)
                if callee in validators and args
                and _root_identifier_in([args[0]], name)
                and start > statement_end
            )
            commit_positions = [
                start for callee, args, start, _ in _call_records(poll)
                if callee == commit_owners[0] and args
                and _root_identifier_in([args[0]], name)
            ] if commit_owners[0] != "poll" else [
                match.start() for match in re.finditer(
                    rf"\bS\s*\.\s*(?:workspaceSnapshot|workspace_snapshot|snapshot)"
                    rf"\s*=\s*{re.escape(name)}\s*;",
                    poll_code,
                )
            ]
            orchestration.extend(
                (name, validation, commit)
                for validation in validation_positions for commit in commit_positions
                if statement_end <= validation < commit
            )
        self.assertEqual(
            1, len(orchestration),
            "poll must load, rejectingly validate, then commit the same identifier",
        )


class ResetTargetsTheBoardBeingViewed(unittest.TestCase):
    @gate1_live_bug
    def test_test_doctor_dashboard_never_resets_dr_mohamed(self) -> None:
        """Reset is actor-scoped; the browser need not supply a trusted name."""
        sources = _handler_reachable_sources(_unique_event_handler("resetBtn"))
        reset_terminals: list[tuple[str, bool]] = []
        global_admin_reset = re.search(
            r"\b(?:[A-Za-z_$][\w$]*\s*\.\s*)*"
            r"(?:fetch|post|request|[A-Za-z_$][\w$]*(?:post|request)[\w$]*)"
            r"\s*\(\s*(['\"])/admin/reset",
            _without_comments(JAVASCRIPT),
            re.IGNORECASE,
        ) is not None
        for source in sources:
            for callee, args, _, _ in _call_records(source):
                bus = re.match(r"^[Cc]ommandBus\.", callee) is not None
                http = re.search(r"(?:fetch|post|request)", callee, re.I) is not None
                if not args or not (bus or http):
                    continue
                fragments = [
                    part for arg in args for part in _dependency_fragments(source, arg)
                ] if bus else _dependency_fragments(source, args[0])
                reset_semantics = "reset" in callee.lower() or any(
                    "reset" in value.lower()
                    for part in fragments for value in _string_values(part)
                )
                if not reset_semantics:
                    continue
                if bus:
                    reset_terminals.append((callee, not global_admin_reset))
                else:
                    base_origin = any(re.match(
                        r"\s*(?:\(\s*)*BASE\b", _without_comments(part)
                    ) for part in fragments)
                    reset_terminals.append((callee, base_origin))
        self.assertEqual(
            1, len(reset_terminals),
            "resetBtn must reach exactly one HTTP or CommandBus reset terminal",
        )
        self.assertTrue(
            reset_terminals[0][1],
            "reset scope must be carried by endpoint argument 0 (BASE + route) "
            "or by the actual CommandBus call",
        )


class ClosedTodayMeansToday(unittest.TestCase):
    @gate1_live_bug
    def test_historical_green_loops_are_not_described_as_closed_today(self) -> None:
        """Every heroFoot value comes from canonical ``closed_today`` only."""
        hero = _unique_function("renderHero")
        sinks = _dom_sink_expressions(hero, lambda value: value == "heroFoot")
        self.assertTrue(sinks, "renderHero has no auditable heroFoot sink")
        fragments = [
            part for _, sink in sinks for part in _dependency_fragments(hero, sink)
        ]
        self.assertTrue(
            _property_in(fragments, "closed_today"),
            "the displayed heroFoot closure value does not depend on closed_today",
        )
        self.assertFalse(
            _property_in(fragments, "green")
            or _root_identifier_in(fragments, "green"),
            "heroFoot still depends on cumulative .green, even through an alias/rebind",
        )


class BloodPressureTileSelectsBloodPressure(unittest.TestCase):
    @gate1_live_bug
    def test_a_weight_or_glucose_monitor_cannot_populate_the_bp_tile(self) -> None:
        """Only the server BP projection may reach a BP DOM sink."""
        render = _unique_function("renderBP")
        sinks = _dom_sink_expressions(render, lambda value: value.lower().startswith("bp"))
        self.assertTrue(sinks, "renderBP has no auditable bp* DOM sink")
        evidence = [
            (element_id, _dependency_fragments(render, sink))
            for element_id, sink in sinks
        ]
        fragments = [part for _, parts in evidence for part in parts]
        self.assertTrue(
            _property_in(fragments, "bp_tile")
            or _property_in(fragments, "blood_pressure_tile"),
            "no rendered bp* sink depends on the server BP projection",
        )
        forbidden = [
            name for name in ("monitor", "weight", "weight_tile", "glucose", "glucose_tile")
            if _property_in(fragments, name) or _root_identifier_in(fragments, name)
        ]
        self.assertEqual(
            [], forbidden,
            "a bp* sink still depends on a non-BP clinical source: " + ", ".join(forbidden),
        )
        for element_id, parts in evidence:
            code = "\n".join(_code_only(part) for part in parts)
            clinical = re.search(r"(?:\?\.|\.)\s*[A-Za-z_$][\w$]*_tile\b", code)
            clinical = clinical or any(
                _property_in(parts, name) for name in ("monitor", "weight", "glucose")
            )
            if element_id in {"bpBody", "bpRight"} and clinical:
                self.assertTrue(
                    _property_in(parts, "bp_tile")
                    or _property_in(parts, "blood_pressure_tile"),
                    f"{element_id} consumes a non-BP clinical projection",
                )


if __name__ == "__main__":
    unittest.main()
