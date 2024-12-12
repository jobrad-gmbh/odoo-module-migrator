# Copyright (C) 2024 - Today: NextERP Romania (https://nexterp.ro)
# @author: Mihai Fekete (https://github.com/NextERP-Romania)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import ast
import json
import os
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


def add_asset_to_manifest(assets: dict, asset_type: str, asset_file: str) -> None:
    """Add an asset to a manifest file."""
    assets[asset_type].append(asset_file)


def remove_asset_file_from_manifest(data: list, file: str) -> None:
    """Remove asset file from manifest views."""
    data.remove(file)


def remove_node_from_xml(record_node, node):
    """Remove a node from an XML tree."""
    to_remove = True
    if node.getchildren():
        to_remove = False
    if to_remove:
        parent = node.getparent() if node.getparent() is not None else record_node
        parent.remove(node)


def find_assets_inject_index(manifest_source: str) -> int:
    ast_tree: ast.Module = asttokens.ASTTokens(manifest_source, parse=True).tree

    ast_dict_expr, = ast_tree.body
    ast_dict_expr: ast.Expr

    ast_dict: ast.Dict = ast_dict_expr.value
    last_token: Token = ast_dict.values[-1].last_token

    assets_inject_index = last_token.endpos
    if manifest_source[assets_inject_index] == ",":
        assets_inject_index += 1

    return assets_inject_index


def find_data_index_range(manifest_source: str) -> Tuple[int, int]:
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


def inject_assets_dict(manifest_source: str, assets: dict, quote_char: str, indentation: int) -> str:
    index = find_assets_inject_index(manifest_source)
    assets_dict_str = format_dict({"assets": assets}, quote_char, indentation)
    assets_dict_str = assets_dict_str[1:len(assets_dict_str) - 1].rstrip()

    delimiter = "," if manifest_source[index] != "," else ""

    manifest_source_new = manifest_source[:index] + delimiter + assets_dict_str + manifest_source[index:]
    return manifest_source_new


def replace_data_list(manifest_source: str, data: list, quote_char: str, indentation: int) -> str:
    index_start, index_end = find_data_index_range(manifest_source)
    data_list_str = format_dict({"data": data}, quote_char, indentation)
    data_list_str = data_list_str[1:len(data_list_str) - 1].rstrip()

    manifest_source_new = manifest_source[:index_start] + data_list_str + manifest_source[index_end:]

    return manifest_source_new


def format_dict(assets: dict, quote_char: str, indentation: int) -> str:
    assets_str = json.dumps(assets, indent=indentation).replace(": true", ": True").replace(
        ": false", ": False"
    )

    if quote_char == "'":
        assets_str = assets_str.replace('"', quote_char)

    return assets_str


def determine_quote_char(manifest_source: str) -> str:
    single_count = manifest_source.count("'")
    double_count = manifest_source.count('"')
    return '"' if double_count >= single_count else "'"


def determine_indentation(manifest_source: str) -> int:
    indentations = []
    for line in manifest_source.splitlines():
        if not line.strip():
            continue

        leading_spaces = len(line) - len(line.lstrip(" "))
        if leading_spaces == 0:
            continue

        indentation_ok = False
        for factor in ALLOWED_INDENTATIONS:
            if leading_spaces % factor == 0:
                indentations.append(factor)
                indentation_ok = True

        if not indentation_ok:
            raise RuntimeError("Unexpected indentation a manifest file.")

    element: int
    (element, _), = Counter(indentations).most_common(1)
    return element


def reformat_assets_definition(
        logger, module_path, module_name, manifest_path, migration_steps, tools
):
    """Reformat assets declaration in XML files."""

    parser = et.XMLParser(
        remove_blank_text=False,
        resolve_entities=False,
        remove_comments=False,
        remove_pis=False,
        strip_cdata=False
    )

    with open(manifest_path, "rt") as f:
        manifest_source = f.read()

    quote_char = determine_quote_char(manifest_source)
    indentation = determine_indentation(manifest_source)

    manifest = tools._get_manifest_dict(manifest_path)

    assets_dict = defaultdict(list)
    data_list: list = manifest.get("data", [])

    for file_path in data_list.copy():
        if not file_path.endswith(".xml"):
            continue

        xml_file = open(os.path.join(module_path, file_path), "r")
        tree = et.parse(xml_file, parser)
        record_node = tree.getroot()
        for node in record_node.getchildren():
            inherit_id = node.get("inherit_id")
            if inherit_id not in ASSET_VIEWS:
                continue

            for xpath_elem in node.xpath("xpath[@expr]"):
                for file in xpath_elem.getchildren():
                    if elem_file_path := file.get("src") or file.get("href"):
                        add_asset_to_manifest(assets_dict, inherit_id, elem_file_path)
                        remove_node_from_xml(record_node, file)

                remove_node_from_xml(record_node, xpath_elem)
            remove_node_from_xml(record_node, node)

        # write back the node to the XML file
        with open(os.path.join(module_path, file_path), "wb") as f:
            et.indent(tree)
            tree.write(f, encoding="utf-8", xml_declaration=True, pretty_print=True)

        if not record_node.getchildren():
            remove_asset_file_from_manifest(data_list, file_path)
            os.remove(os.path.join(module_path, file_path))

    # update the manifest
    if assets_dict:
        manifest_source = inject_assets_dict(manifest_source, assets_dict, quote_char, indentation)
    if data_list:
        manifest_source = replace_data_list(manifest_source, data_list, quote_char, indentation)

    tools._write_content(manifest_path, manifest_source)


class MigrationScript(BaseMigrationScript):
    _GLOBAL_FUNCTIONS = [reformat_assets_definition]
