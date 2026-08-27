# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo_module_migrate.base_migration_script import BaseMigrationScript
from odoo_module_migrate import ast_tools
import lxml.etree as et
from pathlib import Path
import logging
import re
import ast
from typing import Any, NamedTuple

empty_list = ast.parse("[]").body[0].value


class AbstractVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        # ((line, line_end, col_offset, end_col_offset), replace_by) NO OVERLAPS
        self.change_todo = []

    def post_process(self, all_code: str, file: str) -> str:
        all_lines = all_code.split("\n")
        for (lineno, line_end, col_offset, end_col_offset), new_substring in sorted(
            self.change_todo, reverse=True
        ):
            if lineno == line_end:
                line = all_lines[lineno - 1]
                all_lines[lineno - 1] = (
                    line[:col_offset] + new_substring + line[end_col_offset:]
                )
            else:
                print(
                    f"Ignore replacement {file}: {(lineno, line_end, col_offset, end_col_offset), new_substring}"
                )
        return "\n".join(all_lines)

    def add_change(self, old_node: ast.AST, new_node: ast.AST | str):
        position = (
            old_node.lineno,
            old_node.end_lineno,
            old_node.col_offset,
            old_node.end_col_offset,
        )
        if isinstance(new_node, str):
            self.change_todo.append((position, new_node))
        else:
            self.change_todo.append((position, ast.unparse(new_node)))


class VisitorToPrivateReadGroup(AbstractVisitor):
    def post_process(self, all_code: str, file: str) -> str:
        all_lines = all_code.split("\n")
        for i, line in enumerate(all_lines):
            if "super(" not in line:
                all_lines[i] = line.replace(".read_group(", "._read_group(")
        return "\n".join(all_lines)


class VisitorInverseGroupbyFields(AbstractVisitor):
    def visit_Call(self, node: ast.Call) -> Any:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "_read_group":
            # Should have the same number of args/keywords
            # Inverse fields/groupby order
            keywords_by_key = {keyword.arg: keyword.value for keyword in node.keywords}
            key_i_by_key = {keyword.arg: i for i, keyword in enumerate(node.keywords)}
            if len(node.args) >= 3:
                self.add_change(node.args[2], node.args[1])
                self.add_change(node.args[1], node.args[2])
            elif len(node.args) == 2:
                new_args_value = keywords_by_key.get("groupby", empty_list)
                if "groupby" in keywords_by_key:
                    fields_args = ast.keyword("fields", node.args[1])
                    self.add_change(node.args[1], new_args_value)
                    self.add_change(node.keywords[key_i_by_key["groupby"]], fields_args)
                else:
                    self.add_change(
                        node.args[1],
                        f"{ast.unparse(new_args_value)}, {ast.unparse(node.args[1])}",
                    )
            else:  # len(node.args) <= 2
                if (
                    "groupby" in key_i_by_key
                    and "fields" in key_i_by_key
                    and key_i_by_key["groupby"] > key_i_by_key["fields"]
                ):
                    self.add_change(
                        node.keywords[key_i_by_key["groupby"]],
                        node.keywords[key_i_by_key["fields"]],
                    )
                    self.add_change(
                        node.keywords[key_i_by_key["fields"]],
                        node.keywords[key_i_by_key["groupby"]],
                    )
                else:
                    raise ValueError(f"{key_i_by_key}, {keywords_by_key}, {node.args}")
        self.generic_visit(node)


class VisitorRenameKeywords(AbstractVisitor):
    def visit_Call(self, node: ast.Call) -> Any:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "_read_group":
            # Replace fields by aggregate and orderby by order
            for keyword in node.keywords:
                if keyword.arg == "fields":
                    new_keyword = ast.keyword("aggregates", keyword.value)
                    self.add_change(keyword, new_keyword)
                if keyword.arg == "orderby":
                    new_keyword = ast.keyword("order", keyword.value)
                    self.add_change(keyword, new_keyword)
        self.generic_visit(node)


class VisitorRemoveLazy(AbstractVisitor):
    def post_process(self, all_code: str, file: str) -> str:
        # remove extra comma ',' and extra line if possible
        all_code = super().post_process(all_code, file)
        all_lines = all_code.split("\n")
        for (lineno, __, col_offset, __), __ in sorted(self.change_todo, reverse=True):
            comma_find = False
            line = all_lines[lineno - 1]
            remaining = line[col_offset:]
            line = line[:col_offset]
            while not comma_find:
                if "," not in line:
                    all_lines.pop(lineno - 1)
                    lineno -= 1
                    line = all_lines[lineno - 1]
                else:
                    comma_find = True
            last_index_comma = -(line[::-1].index(",") + 1)
            all_lines[lineno - 1] = line[:last_index_comma] + remaining

        return "\n".join(all_lines)

    def visit_Call(self, node: ast.Call) -> Any:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "_read_group":
            # Replace fields by aggregate and orderby by order
            if len(node.args) == 7:
                self.add_change(node.args[6], "")
            else:
                for keyword in node.keywords:
                    if keyword.arg == "lazy":
                        self.add_change(keyword, "")
        self.generic_visit(node)


class VisitorAggregatesSpec(AbstractVisitor):
    def visit_Call(self, node: ast.Call) -> Any:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "_read_group":

            keywords_by_key = {keyword.arg: keyword.value for keyword in node.keywords}
            aggregate_values = None
            if len(node.args) >= 3:
                aggregate_values = node.args[2]
            elif "aggregates" in keywords_by_key:
                aggregate_values = keywords_by_key["aggregates"]

            groupby_values = empty_list
            if len(node.args) >= 2:
                groupby_values = node.args[1]
            elif "groupby" in keywords_by_key:
                groupby_values = keywords_by_key["groupby"]

            if aggregate_values:
                aggregates = None
                try:
                    aggregates = ast.literal_eval(ast.unparse(aggregate_values))
                    if not isinstance(aggregates, (list, tuple)):
                        raise ValueError(
                            f"{aggregate_values} is not a list but literal ?"
                        )

                    aggregates = [
                        f"{field_spec.split('(')[1][:-1]}:{field_spec.split(':')[1].split('(')[0]}"
                        if "(" in field_spec
                        else field_spec
                        for field_spec in aggregates
                    ]
                    aggregates = [
                        "__count"
                        if field_spec in ("id:count", "id:count_distinct")
                        else field_spec
                        for field_spec in aggregates
                    ]

                    groupby = ast.literal_eval(ast.unparse(groupby_values))
                    if isinstance(groupby, str):
                        groupby = [groupby]

                    aggregates = [
                        f"{field}:sum"
                        if (":" not in field and field != "__count")
                        else field
                        for field in aggregates
                        if field not in groupby
                    ]
                    if not aggregates:
                        aggregates = ["__count"]
                except SyntaxError:
                    pass
                except ValueError:
                    pass

                if aggregates is not None:
                    self.add_change(aggregate_values, repr(aggregates))
        self.generic_visit(node)


Steps_visitor: list[AbstractVisitor] = [
    VisitorToPrivateReadGroup,
    VisitorInverseGroupbyFields,
    VisitorRenameKeywords,
    VisitorAggregatesSpec,
    VisitorRemoveLazy,
]


def replace_read_group_signature(logger, filename):
    with open(filename, mode="rt") as file:
        new_all = all_code = file.read()
        if ".read_group(" in all_code or "._read_group(" in all_code:
            for Step in Steps_visitor:
                visitor = Step()
                try:
                    visitor.visit(ast.parse(new_all))
                except Exception:
                    logger.info(
                        f"ERROR in {filename} at step {visitor.__class__}: \n{new_all}"
                    )
                    raise
                new_all = visitor.post_process(new_all, filename)
            if new_all == all_code:
                logger.info("read_group detected but not changed in file %s" % filename)

    if new_all != all_code:
        logger.info("Script read_group replace applied in file %s" % filename)
        with open(filename, mode="wt") as file:
            file.write(new_all)


def _get_files(module_path, reformat_file_ext):
    """Get files to be reformatted."""
    file_paths = list()
    if not module_path.is_dir():
        raise Exception(f"'{module_path}' is not a directory")
    file_paths.extend(module_path.rglob("*" + reformat_file_ext))
    return file_paths


def _check_open_form_view(logger, file_path: Path):
    """Check if the view has a button to open a form reg in a tree view `file_path`."""
    parser = et.XMLParser(remove_blank_text=True)
    tree = et.parse(str(file_path.resolve()), parser)
    record_node = tree.getroot()[0]
    f_arch = record_node.find('field[@name="arch"]')
    root = f_arch if f_arch is not None else record_node
    for button in root.findall(".//button[@name='get_formview_action']"):
        logger.warning(
            (
                "Button to open a form reg form a tree view detected in file %s line %s, probably should be changed by open_form_view='True'. More info here https://github.com/odoo/odoo/commit/258e6a019a21042bf4f6cf70fcce386d37afd50c"
            )
            % (file_path.name, button.sourceline)
        )


def _check_open_form(
    logger, module_path, module_name, manifest_path, migration_steps, tools
):
    reformat_file_ext = ".xml"
    file_paths = _get_files(module_path, reformat_file_ext)
    logger.debug(f"{reformat_file_ext} files found:\n" f"{list(map(str, file_paths))}")

    for file_path in file_paths:
        _check_open_form_view(logger, file_path)


def _reformat_read_group(
    logger, module_path, module_name, manifest_path, migration_steps, tools
):
    """Reformat read_group method in py files."""

    reformat_file_ext = ".py"
    file_paths = _get_files(module_path, reformat_file_ext)
    logger.debug(f"{reformat_file_ext} files found:\n" f"{list(map(str, file_paths))}")

    reformatted_files = list()
    for file_path in file_paths:
        reformatted_file = replace_read_group_signature(logger, file_path)
        if reformatted_file:
            reformatted_files.append(reformatted_file)
    logger.debug("Reformatted files:\n" f"{list(reformatted_files)}")


def replace_pattern_in_xml(
    xml_file: str, pattern_to_match: str, replacement_text: str
) -> bool:
    with open(xml_file, "r") as file:
        xml_content = file.read()

        if not xml_content or not re.search(pattern_to_match, xml_content):
            return False

    modified_content = re.sub(pattern_to_match, replacement_text, xml_content)

    with open(xml_file, "w") as file:
        file.write(modified_content)

    return True


def modify_files_with_tesc(module_path: str) -> list[str]:
    t_esc_match_found = []
    t_esc = r"t-esc(?![-\w])"
    t_out = "t-out"

    files = _get_files(module_path, ".xml")

    for file_path in files:
        is_t_esc_replaced = replace_pattern_in_xml(str(file_path), t_esc, t_out)

        if is_t_esc_replaced:
            t_esc_match_found.append(str(file_path))

    return t_esc_match_found


def _replace_tesc_attribute_by_tout(
    logger: logging.Logger,
    module_path: Path,
    module_name: str,
    manifest_path: Path,
    migration_steps,
    tools,
):
    logger.debug(f"Starting t-esc to t-out replacement for {module_name}")
    t_esc_data = modify_files_with_tesc(module_path)

    for file_path in t_esc_data:
        logger.info(f"Replaced t-esc by t-out in file {file_path}")

    logger.debug(f"Result for {module_name}:\n{{'Esc Expression Files': t_esc_data}}")


QWEB_BUNDLE = "web.assets_qweb"
BACKEND_BUNDLE = "web.assets_backend"


class Bundle(NamedTuple):
    """An `assets` entry of a manifest, its nodes being None when it is absent."""

    key: ast.AST | None
    value: ast.AST | None

    @property
    def exists(self) -> bool:
        return self.key is not None

    @property
    def items(self) -> list[ast.AST]:
        return self.value.elts if isinstance(self.value, ast.List) else []

    @property
    def is_list(self) -> bool:
        return isinstance(self.value, ast.List)


class Replacement(NamedTuple):
    """A replacement of the `[start:end]` slice of the manifest source."""

    start: int
    end: int
    replacement: str


def _get_assets_bundles(manifest_content: str) -> tuple[Bundle, Bundle] | None:
    """Return the (qweb, backend) bundles, or None if there is nothing to move."""
    manifest_node = ast_tools.get_dict_node(manifest_content)

    if manifest_node is None:
        return None

    __, assets_node = ast_tools.get_dict_entry(manifest_node, "assets")

    if not isinstance(assets_node, ast.Dict):
        return None

    qweb_bundle = Bundle(*ast_tools.get_dict_entry(assets_node, QWEB_BUNDLE))
    backend_bundle = Bundle(*ast_tools.get_dict_entry(assets_node, BACKEND_BUNDLE))

    if not qweb_bundle.exists:
        return None

    return qweb_bundle, backend_bundle


def _rename_bundle(positions: ast_tools.SourcePositions, qweb: Bundle) -> Replacement:
    """Rename the qweb bundle, used when there is no backend bundle yet."""
    start, end = positions.span(qweb.key)
    renamed_key = positions.get_node_segment(qweb.key).replace(
        QWEB_BUNDLE, BACKEND_BUNDLE
    )

    return Replacement(start, end, renamed_key)


def _merge_bundles(
    positions: ast_tools.SourcePositions, qweb: Bundle, backend: Bundle
) -> list[Replacement]:
    # Nothing to merge
    if not qweb.items:
        return []

    # if backend has no items, items from qweb content just will be reindented and moved into backend
    if not backend.items:
        start, end = positions.span(backend.value)
        moved_items = positions.reindent_segment(
            qweb.value, positions.indent(backend.value)
        )

        return [Replacement(start, end, moved_items)]

    known_items = {ast.dump(node) for node in backend.items}

    # Only merge items that do not exist yet in backend bundle
    new_items = [node for node in qweb.items if ast.dump(node) not in known_items]

    if not new_items:
        return []

    offset, text = positions.insert_items(backend.value, new_items)

    return [Replacement(offset, offset, text)]


def _apply_replacements(content: str, replacements: list[Replacement]) -> str:
    """Apply the replacements from the last one to the first one, to keep offsets valid."""
    for start, end, replacement in sorted(replacements, reverse=True):
        content = content[:start] + replacement + content[end:]

    return content


def _merge_qweb_backend_bundle(
    logger: logging.Logger, manifest_content: str, module_name: str
) -> str:
    """Merge the `web.assets_qweb` bundle of a manifest into `web.assets_backend`.

    Only the bundles themselves are rewritten, the rest of the manifest (comments,
    quoting, formatting) is left untouched.
    """
    bundles = _get_assets_bundles(manifest_content)

    if bundles is None:
        return manifest_content

    qweb, backend = bundles
    positions = ast_tools.SourcePositions(manifest_content)

    # Rename qweb bundle as backend due to the inexistence of backend bundle
    if not backend.exists:
        return _apply_replacements(manifest_content, [_rename_bundle(positions, qweb)])

    if not qweb.is_list or not backend.is_list:
        logger.warning(
            f"{module_name}: {QWEB_BUNDLE} and/or {BACKEND_BUNDLE} is not defined as a list, the assets have to be moved manually."
        )

        return manifest_content

    # qweb bundle will be replaced by empty string first and then will be merge into backend bundle
    replacements = [
        Replacement(*positions.entry_removal_span(qweb.key, qweb.value), ""),
        *_merge_bundles(positions, qweb, backend),
    ]

    return _apply_replacements(manifest_content, replacements)


def _migrate_qweb_assets(
    logger: logging.Logger,
    module_path: Path,
    module_name: str,
    manifest_path: Path,
    migration_steps,
    tools,
):
    if not manifest_path or not manifest_path.exists():
        return

    manifest_content = tools._read_content(manifest_path)
    modified_manifest_content = _merge_qweb_backend_bundle(
        logger, manifest_content, module_name
    )

    if modified_manifest_content != manifest_content:
        logger.info(
            f"Moving the assets of {QWEB_BUNDLE} into {BACKEND_BUNDLE}"
            f" for {module_name}"
        )
        tools._write_content(manifest_path, modified_manifest_content)


class MigrationScript(BaseMigrationScript):

    _GLOBAL_FUNCTIONS = [
        _check_open_form,
        _migrate_qweb_assets,
        _reformat_read_group,
        _replace_tesc_attribute_by_tout,
    ]
