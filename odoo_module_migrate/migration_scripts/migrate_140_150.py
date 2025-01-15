# Copyright (C) 2024 - Today: NextERP Romania (https://nexterp.ro)
# @author: Mihai Fekete (https://github.com/NextERP-Romania)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import ast
import json
import os
import re
from collections import defaultdict, Counter
from typing import Tuple

import asttokens
import lxml.etree as et
from asttokens.util import Token

from odoo_module_migrate.base_migration_script import BaseMigrationScript

ASSET_VIEWS = [
    "mass_mailing.assets_backend",
    "mass_mailing.assets_mail_themes",
    "mass_mailing.assets_mail_themes_edition",
    "mrp.assets_common",
    "point_of_sale.assets",
    "point_of_sale.pos_assets_backend",
    "snailmail.report_assets_snailmail",
    "stock.assets_stock_print_report",
    "survey.survey_assets",
    "survey.survey_user_input_session_assets",
    "web.assets_backend",
    "web.assets_backend_prod_only",
    "web.assets_common",
    "web.assets_common_lazy",
    "web.assets_common_minimal_js",
    "web.assets_frontend",
    "web.assets_frontend_lazy",
    "web.assets_frontend_minimal_js",
    "web.assets_qweb",
    "web.assets_tests",
    "web.pdf_js_lib",
    "web.qunit_mobile_suite_tests",
    "web.qunit_suite_tests",
    "web.report_assets_common",
    "web.report_assets_pdf",
    "web.tests_assets",
    "website.assets_frontend",
    "website.assets_editor",
    "website.assets_frontend_editor",
    "website.assets_wysiwyg",
    "website_slides.slide_embed_assets",
    "website.test_bundle",
    "web_editor.assets_summernote",
    "web_editor.assets_wysiwyg",
    "web_enterprise.assets_backend",
    "web_enterprise.assets_common",
    "web_enterprise._assets_backend_helpers",
]

ALLOWED_INDENTATIONS = [4, 2, 3]
EMPTY_LINE_PLACEHOLDER = "<?empty-line-oixbckyh?>"


def reformat_assets_definition(
        logger, module_path, module_name, manifest_path, migration_steps, tools
):
    """Reformat assets declaration in XML files."""

    with open(manifest_path, "rt") as f:
        manifest_source = f.read()

    quote_char = _determine_quote_char(manifest_source)
    indentation = _determine_indentation(manifest_source)

    manifest = tools._get_manifest_dict(manifest_path)

    assets_dict = defaultdict(list)
    data_list_original: list = manifest.get("data", [])
    data_list = data_list_original.copy()

    for file_path in data_list_original:
        if not file_path.endswith(".xml"):
            continue

        xml_file_path: str = os.path.join(module_path, file_path)
        tree = _parse_xml_file(xml_file_path)
        for node in tree.getchildren():
            inherit_id = node.get("inherit_id")
            if inherit_id not in ASSET_VIEWS:
                continue

            for xpath_elem in node.xpath("xpath[@expr]"):
                for file in xpath_elem.getchildren():
                    if elem_file_path := file.get("src") or file.get("href"):
                        _add_asset_to_manifest(assets_dict, inherit_id, elem_file_path)
                        _remove_node_from_xml(tree, file)

                _remove_node_from_xml(tree, xpath_elem)
            _remove_node_from_xml(tree, node)

        # write back the node to the XML file
        xml_source = _serialize_xml_tree(tree)
        with open(xml_file_path, "wt") as f:
            print(xml_source, file=f, end="")

        if not _has_regular_subnodes(tree):
            _remove_asset_file_from_manifest(data_list, file_path)
            os.remove(os.path.join(module_path, file_path))

    # move templates from "qweb" to "web.assets_qweb" inside "assets"
    has_qweb_list: bool = "qweb" in manifest
    if has_qweb_list:
        assets_dict["web.assets_qweb"].extend(manifest["qweb"])

    # update the manifest
    if assets_dict:
        _ensure_asset_paths_start_with_module_name(assets_dict, module_name)
        manifest_source = _inject_assets_dict(manifest_source, assets_dict, quote_char, indentation)
    if data_list_original and data_list != data_list_original:
        manifest_source = _replace_data_list(manifest_source, data_list, quote_char, indentation)
    if has_qweb_list:
        manifest_source = _remove_qweb_list(manifest_source)

    tools._write_content(manifest_path, manifest_source)


def _parse_xml_file(path: str) -> et.ElementTree:
    parser = et.XMLParser(
        remove_blank_text=False,
        resolve_entities=False,
        remove_comments=False,
        remove_pis=False,
        strip_cdata=False
    )

    xml_with_placeholders = ""
    with open(path, "rt") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                raw_line = EMPTY_LINE_PLACEHOLDER + '\n'
            xml_with_placeholders += raw_line

    tree = et.fromstring(xml_with_placeholders.encode("utf-8"), parser=parser)
    return tree


def _serialize_xml_tree(tree: et.ElementTree, indent_spaces=2) -> str:
    et.indent(tree, space=" " * indent_spaces)
    xml_formatted_with_placeholders = et.tostring(
        tree,
        pretty_print=True,
        xml_declaration=True,
        with_tail=True,
        encoding="utf-8"
    ).decode("utf-8")

    xml_formatted = ""
    for raw_line in xml_formatted_with_placeholders.splitlines(keepends=True):
        line = raw_line.strip()
        if line == EMPTY_LINE_PLACEHOLDER:
            raw_line = "\n"
        elif line.startswith("<?xml"):
            raw_line = re.sub(r"(\w+)='([^']*)'", "\\1=\"\\2\"", raw_line)
        xml_formatted += raw_line

    return xml_formatted


def _add_asset_to_manifest(assets: dict, asset_type: str, asset_file: str) -> None:
    """Add an asset to a manifest file."""
    assets[asset_type].append(asset_file)


def _ensure_asset_paths_start_with_module_name(assets: dict, module_name: str) -> None:
    for asset_paths in assets.values():
        for i, asset_path in enumerate(asset_paths):
            root_path = f"/{module_name}"
            if not asset_path.startswith(root_path) and not asset_path.startswith(module_name):
                asset_paths[i] = os.path.join(root_path, asset_path)


def _remove_asset_file_from_manifest(data: list, file: str) -> None:
    """Remove asset file from manifest views."""
    data.remove(file)


def _remove_node_from_xml(record_node, node):
    """Remove a node from an XML tree."""
    to_remove = True
    if _has_regular_subnodes(node):
        to_remove = False
    if to_remove:
        parent = node.getparent() if node.getparent() is not None else record_node
        parent.remove(node)


def _has_regular_subnodes(node) -> bool:
    """Check if node has regular children, i.e. nodes that are not comments,
    processing instructions, etc."""
    regular_subnodes = [elem for elem in node.iter(tag=et.Element) if elem != node]
    return len(regular_subnodes) != 0


def _find_assets_inject_index(manifest_source: str) -> int:
    ast_tree: ast.Module = asttokens.ASTTokens(manifest_source, parse=True).tree

    ast_dict_expr, = ast_tree.body
    ast_dict_expr: ast.Expr

    ast_dict: ast.Dict = ast_dict_expr.value
    last_token: Token = ast_dict.values[-1].last_token

    assets_inject_index = last_token.endpos
    if manifest_source[assets_inject_index] == ",":
        assets_inject_index += 1

    return assets_inject_index


def _find_data_index_range(manifest_source: str) -> Tuple[int, int]:
    ast_tree: ast.Module = asttokens.ASTTokens(manifest_source, parse=True).tree

    ast_dict_expr, = ast_tree.body
    ast_dict_expr: ast.Expr

    ast_dict: ast.Dict = ast_dict_expr.value
    assert len(ast_dict.keys) == len(ast_dict.values)

    previous_last_token: Token = ast_dict.first_token
    for key, value in zip(ast_dict.keys, ast_dict.values):
        key: ast.Constant
        if key.value == "data":
            first_token: Token = previous_last_token
            last_token: Token = value.last_token
            index_start: int = first_token.startpos + 1
            if manifest_source[index_start] == ",":
                index_start += 1
            index_end: int = last_token.endpos
            return index_start, index_end

        previous_last_token = value.last_token

    raise RuntimeError("Unable to find data list in the manifest.")


def _find_qweb_index_range(manifest_source: str) -> Tuple[int, int]:
    ast_tree: ast.Module = asttokens.ASTTokens(manifest_source, parse=True).tree

    ast_dict_expr, = ast_tree.body
    ast_dict_expr: ast.Expr

    ast_dict: ast.Dict = ast_dict_expr.value
    assert len(ast_dict.keys) == len(ast_dict.values)

    previous_last_token: Token = ast_dict.first_token
    for key, value in zip(ast_dict.keys, ast_dict.values):
        key: ast.Constant
        if key.value == "qweb":
            first_token: Token = previous_last_token
            last_token: Token = value.last_token
            index_start: int = first_token.startpos + 1
            index_end: int = last_token.endpos
            return index_start, index_end

        previous_last_token = value.last_token

    raise RuntimeError("Unable to find qweb list in the manifest.")


def _inject_assets_dict(manifest_source: str, assets: dict, quote_char: str, indentation: int) -> str:
    index = _find_assets_inject_index(manifest_source)
    assets_dict_str = _format_dict({"assets": assets}, quote_char, indentation)
    assets_dict_str = assets_dict_str[1:len(assets_dict_str) - 1].rstrip()

    delimiter = "," if manifest_source[index - 1] != "," else ""

    manifest_source_new = manifest_source[:index] + delimiter + assets_dict_str + manifest_source[index:]
    return manifest_source_new


def _replace_data_list(manifest_source: str, data: list, quote_char: str, indentation: int) -> str:
    index_start, index_end = _find_data_index_range(manifest_source)
    data_list_str = _format_dict({"data": data}, quote_char, indentation)
    data_list_str = data_list_str[1:len(data_list_str) - 1].rstrip()

    manifest_source_new = manifest_source[:index_start] + data_list_str + manifest_source[index_end:]

    return manifest_source_new


def _remove_qweb_list(manifest_source) -> str:
    index_start, index_end = _find_qweb_index_range(manifest_source)
    manifest_source_new = manifest_source[:index_start] + manifest_source[index_end:]
    return manifest_source_new


def _format_dict(dictionary: dict, quote_char: str, indentation: int) -> str:
    assets_str = json.dumps(dictionary, indent=indentation).replace(": true", ": True").replace(
        ": false", ": False"
    )

    if quote_char == "'":
        assets_str = assets_str.replace('"', quote_char)

    return assets_str


def _determine_quote_char(manifest_source: str) -> str:
    single_count = manifest_source.count("'")
    double_count = manifest_source.count('"')
    return '"' if double_count >= single_count else "'"


def _determine_indentation(manifest_source: str) -> int:
    indentations = []
    for line in manifest_source.splitlines():
        if not line.strip():
            continue

        leading_spaces = len(line) - len(line.lstrip(" "))
        if leading_spaces == 0:
            continue

        for factor in ALLOWED_INDENTATIONS:
            if leading_spaces % factor == 0:
                indentations.append(factor)

    if not indentations:
        raise RuntimeError(f"Could not determine the indentation in a manifest file:\n{manifest_source}")

    element: int
    (element, _), = Counter(indentations).most_common(1)
    return element


# Function to replace pattern in XML text or attributes
def replace_pattern_in_xml(xml_file: str, pattern_to_match: str, replacement_text: str):
    # Open and read the XML file
    with open(xml_file, "r", encoding="UTF-8") as file:
        xml_content = file.read()

    # Use re.sub to replace the pattern in the XML content
    modified_content = re.sub(pattern_to_match, replacement_text, xml_content)

    # Write the modified content back to the same XML file
    with open(xml_file, "w", encoding="UTF-8") as file:
        file.write(modified_content)


def search_directories(logger, module_path: str):
    abs_path = os.path.abspath(module_path)
    value_match_found = []
    t_raw_match_found = []
    t_esc_match_found = []
    value_pattern_to_match = r"\${([^}|]+)(\| ?safe)?}"
    value_replacement_text = r"{{ \1 }}"
    t_raw = "t-raw"
    t_esc = "t-esc"
    t_out = "t-out"
    file_type_to_match = "^.*\.(xml)$"
    for root, dirs, files in os.walk(abs_path):
        for file_name in files:
            file_path = os.path.join(root, file_name)
            if re.search(file_type_to_match, file_name):
                xml_file = open(file_path, "r")
                data = xml_file.read()
                if data and re.search(value_pattern_to_match, data):
                    value_match_found.append(file_path)
                    replace_pattern_in_xml(file_path, value_pattern_to_match, value_replacement_text)
                if data and re.search(t_raw, data):
                    t_raw_match_found.append(file_path)
                    replace_pattern_in_xml(file_path, t_raw, t_out)
                if data and re.search(t_esc, data):
                    t_esc_match_found.append(file_path)
                    replace_pattern_in_xml(file_path, t_esc, t_out)
    logger.debug("value match_found {0}".format(len(value_match_found)))
    logger.debug("t-raw match_found {0}".format(len(t_raw_match_found)))
    logger.debug("t-esc match_found {0}".format(len(t_esc_match_found)))
    return value_match_found, t_raw_match_found, t_esc_match_found


def reformat_email_template(logger, module_path, module_name, manifest_path, migration_steps, tools):
    """Reformat email templates. jinja expression to qweb expression."""
    logger.info("Starting email template reformating (Jinja to qweb expression) for ------  {0}!".format(module_name))
    updated_files = {}
    value_data, t_raw_data, t_esc_data = search_directories(logger, module_path)
    updated_files.update({
        "Value Expression Files": value_data,
        "Raw Expression Files": t_raw_data,
        "Esc Expression Files": t_esc_data
    })
    logger.debug("Ending email template reformating (Jinja to qweb expression) for ------- {0}!".format(module_name))
    logger.debug("Result for {0}:\n{1}".format(module_name, updated_files))

class MigrationScript(BaseMigrationScript):
    _GLOBAL_FUNCTIONS = [reformat_assets_definition, reformat_email_template]
