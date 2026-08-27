"""Helpers to rewrite python sources through their ast, in place.

Unlike `ast.unparse`, editing the source with the offsets of the nodes leaves
everything that is not rewritten (comments, quoting, formatting) untouched.
"""

import ast

# Start and end offsets of a slice of the source
Span = tuple[int, int]

SPACE_OR_TAB_STRING = " \t"


def get_dict_entry(node: ast.Dict, key: str) -> tuple[ast.AST | None, ast.AST | None]:
    """Return the (key node, value node) of `key` in a dict literal, or (None, None)."""
    for key_node, value_node in zip(node.keys, node.values):
        if isinstance(key_node, ast.Constant) and key_node.value == key:
            return key_node, value_node

    return None, None


def get_dict_node(content: str) -> ast.Dict | None:
    """Return the first dict literal written as a statement of `content`, if any."""
    for node in ast.parse(content).body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Dict):
            return node.value

    return None


class SourcePositions:
    """Map the positions of the nodes of an ast to offsets in their source code."""

    def __init__(self, content: str):
        self.content = content
        self._lines = content.split("\n")
        self._line_starts = []
        offset = 0

        for line in self._lines:
            self._line_starts.append(offset)
            offset += len(line) + 1

    def get_line_offset(self, lineno: int, col_offset: int) -> int:
        line = self._lines[lineno - 1]

        return self._line_starts[lineno - 1] + len(line.encode()[:col_offset].decode())

    def span(self, node: ast.AST) -> Span:
        return (
            self.get_line_offset(node.lineno, node.col_offset),
            self.get_line_offset(node.end_lineno, node.end_col_offset),
        )

    def get_node_segment(self, node: ast.AST) -> str:
        start, end = self.span(node)

        return self.content[start:end]

    def indent(self, node: ast.AST) -> str:
        line = self._lines[node.lineno - 1]

        return line[: len(line) - len(line.lstrip())]

    def entry_removal_span(self, key_node: ast.AST, value_node: ast.AST) -> Span:
        """Span of a dict entry, trailing comma included, and whole line if it has one
        for itself."""
        key_node_start = self.span(key_node)[0]
        value_node_end = self.span(value_node)[1]
        value_node_end_with_trailing_comma = self._after_trailing_comma(value_node_end)

        line_start = self._line_start(key_node_start)

        '''
        Case 1:
        is_something_before_key_node = True
        {"a": 1, "b": 2, "c": 3} and trying to remove "b" node
        '''
        is_something_before_key_node = self.content[line_start:key_node_start].strip()

        if is_something_before_key_node:
            # Something else shares the beginning of the line, only drop the spaces
            # trailing the entry
            return key_node_start, self._after_spaces(value_node_end_with_trailing_comma)

        '''
        Case 2: the entry to remove starts its own line, but another entry
        follows it on the same line — only the removed entry's own text should
        be deleted; e.g: "b" must be deleted, but "c" must be preserved untouched.
        {
            "a": 1,
            "b": 2, "c": 3,
        }
        '''
        line_end = self._line_end(offset = value_node_end_with_trailing_comma)
        is_something_after_value_node_end = self.content[value_node_end_with_trailing_comma:line_end].strip()

        if is_something_after_value_node_end and not is_something_after_value_node_end.startswith("#"):
            # Another entry follows on the same line, leave the line itself alone
            return key_node_start, value_node_end_with_trailing_comma

        # The entry is alone on its line
        return line_start, line_end + 1

    def insert_items(
        self, target_node: ast.List, item_nodes: list[ast.AST]
    ) -> tuple[int, str]:
        """Offset and text appending items at the end of a non empty list literal."""
        if not target_node.elts:
            raise ValueError("Cannot compute insertion point for an empty list")

        last_item = target_node.elts[-1]
        indent = self.indent(last_item)
        separator = f",\n{indent}" if self._is_multiline(target_node) else ", "
        text = "".join(
            separator
            + self._reindent(self.get_node_segment(node), self.indent(node), indent)
            for node in item_nodes
        )

        return self.span(last_item)[1], text

    def reindent_segment(self, node: ast.AST, new_indent: str) -> str:
        return self._reindent(
            self.get_node_segment(node), self.indent(node), new_indent
        )

    def _after_spaces(self, offset: int) -> int:
        while (
            offset < len(self.content) and self.content[offset] in SPACE_OR_TAB_STRING
        ):
            offset += 1

        return offset

    def _after_trailing_comma(self, offset: int) -> int:
        comma = self._after_spaces(offset)

        if comma < len(self.content) and self.content[comma] == ",":
            return comma + 1

        return offset

    def _line_start(self, offset: int) -> int:
        return self.content.rfind("\n", 0, offset) + 1

    def _line_end(self, offset: int) -> int:
        line_end = self.content.find("\n", offset)

        return len(self.content) if line_end == -1 else line_end

    @staticmethod
    def _is_multiline(node: ast.AST) -> bool:
        return node.lineno != node.end_lineno

    @staticmethod
    def _reindent(segment: str, old_indent: str, new_indent: str) -> str:
        """Shift the continuation lines of a source segment to another indentation."""
        if old_indent == new_indent or "\n" not in segment:
            return segment

        first_line, *other_lines = segment.split("\n")

        return "\n".join(
            [first_line]
            + [
                new_indent + line[len(old_indent) :]
                if line.startswith(old_indent)
                else line
                for line in other_lines
            ]
        )
