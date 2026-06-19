# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import ast
import json
import re
from io import BytesIO

from lxml import etree

from odoo_module_migrate.base_migration_script import BaseMigrationScript


def migrate_expression_to_domain(
    logger, module_path, module_name, manifest_path, migration_steps, tools
):
    """Convert odoo.osv.expression usage to odoo.fields.Domain"""
    files_to_process = tools.get_files(module_path, (".py",))

    for file in files_to_process:
        try:
            content = tools._read_content(file)
            original_content = content

            content = re.sub(
                r"from odoo\.osv import expression",
                "from odoo.fields import Domain",
                content,
            )

            content = re.sub(
                r"from odoo\.osv\.expression import (AND|OR|AND, OR|OR, AND)",
                "from odoo.fields import Domain",
                content,
            )

            content = re.sub(r"expression\.AND\(", "Domain.AND(", content)
            content = re.sub(r"expression\.OR\(", "Domain.OR(", content)

            # --- Protect strings and comments from bare AND/OR replacement ---
            # Triple-quoted strings (docstrings) and comments may contain
            # example code like "AND(expr + [OR(...)])" that must NOT be
            # turned into "Domain.AND(Domain.OR(...))".
            _preserve = {}

            def _save(m):
                key = f"\x00PRESERVE_{len(_preserve)}\x00"
                _preserve[key] = m.group(0)
                return key

            content = re.sub(r'""".*?"""', _save, content, flags=re.DOTALL)
            content = re.sub(r"'''.*?'''", _save, content, flags=re.DOTALL)
            content = re.sub(r'^[ \t]*#.*$', _save, content, flags=re.MULTILINE)

            # Replace bare AND(/OR( with Domain.AND(/OR(
            # Exclude BOOL_AND/BOOL_OR (PostgreSQL aggregates) to avoid
            # corrupting SQL strings like "HAVING BOOL_OR(...)".
            content = re.sub(r"(?<!\.)(?<!BOOL_)AND\(", "Domain.AND(", content)
            content = re.sub(r"(?<!\.)(?<!BOOL_)OR\(", "Domain.OR(", content)

            # Restore preserved strings / comments
            for key, original in _preserve.items():
                content = content.replace(key, original)
            # --- End protection ---

            content = re.sub(
                r"from odoo\.fields import Domain, (AND|OR|AND, OR|OR, AND)",
                "from odoo.fields import Domain",
                content,
            )

            # Ensure Domain is imported when bare AND/OR were replaced
            # (handles import styles not covered by the patterns above).
            if "Domain." in content and "from odoo.fields import Domain" not in content:
                import_line = "from odoo.fields import Domain"
                # Insert after the last top-level import.  Stop scanning
                # once we encounter real code (class/def/decorator/assignment)
                # to avoid picking up local imports inside function bodies.
                lines = content.split("\n")
                last_import_idx = -1
                for i, line in enumerate(lines):
                    stripped = line.strip()
                    if stripped.startswith("from ") or stripped.startswith("import "):
                        last_import_idx = i
                    elif (
                        stripped
                        and not stripped.startswith("#")
                        and not stripped.startswith('"""')
                        and not stripped.startswith("'''")
                    ):
                        # Hit non-import code — end of top-level import block
                        break
                if last_import_idx >= 0:
                    lines.insert(last_import_idx + 1, import_line)
                else:
                    lines.insert(0, import_line)
                content = "\n".join(lines)

            lines = content.split("\n")
            seen_domain_import = False
            cleaned_lines = []

            for line in lines:
                if line.strip() == "from odoo.fields import Domain":
                    if not seen_domain_import:
                        cleaned_lines.append(line)
                        seen_domain_import = True
                else:
                    cleaned_lines.append(line)

            content = "\n".join(cleaned_lines)

            if content != original_content:
                tools._write_content(file, content)
                logger.info(f"Migrated expression imports to Domain in: {file}")

        except Exception as e:
            logger.error(f"Error processing file {file}: {str(e)}")


def upgrade_sql_constraints(
    logger, module_path, module_name, manifest_path, migration_steps, tools
):
    # Odoo method in which we migrate all occurrences of _sql_constraints
    files_to_process = tools.get_files(module_path, (".py",))
    # Regex pattern explanation:
    # (?m) - Multiline mode, ^ matches start of each line
    # ^(?![ \t]*#) - Negative lookahead: exclude lines starting with # (comments)
    # ([ \t]*) - Capture group 1: leading spaces/tabs (NOT newlines to avoid extra blank lines)
    # \b_sql_constraints\s*=\s*\[ - Match "_sql_constraints = ["
    # ([^\]]+) - Capture group 2: constraint content (everything until the closing bracket)
    # ] - Match closing bracket
    # re.DOTALL - Allow . to match newlines for multi-line constraints
    sql_expression_re = re.compile(
        r"(?m)^(?![ \t]*#)([ \t]*)\b_sql_constraints\s*=\s*\[([^\]]+)]", re.DOTALL
    )
    ind = " " * 4

    # Function to build the new SQL constraint definition
    def build_sql_object(match):
        # Preserve the original indentation level (e.g., 2 spaces, 4 spaces, 8 spaces for nested classes)
        leading_indent = match.group(1)
        constraints = ast.literal_eval("[" + match.group(2) + "]")
        result = []
        for name, definition, *messages in constraints:
            message = messages[0] if messages else ""
            constructor = "Constraint"
            if message:
                # format on 2 lines
                message_repr = json.dumps(
                    message, ensure_ascii=False
                )  # so that the message is in double quotes
                args = f"\n{ind * 2}{definition!r},\n{ind * 2}{message_repr},\n{ind}"
            elif len(definition) > 60:
                args = f"\n{ind * 2}{definition!r}"
            else:
                args = repr(definition)
            result.append(f"{leading_indent}_{name} = models.{constructor}({args})")
        return "\n".join(result)

    # Process each file
    for file in files_to_process:
        content = tools._read_content(file)
        content = sql_expression_re.sub(build_sql_object, content)
        if sql_expression_re.search(content):
            logger.warning("Failed to replace sql_constraints")
        tools._write_content(file, content)


def _remove_group_attrs_in_search_views(
    logger, module_path, module_name, manifest_path, migration_steps, tools
):
    """Remove `expand` and `string` attributes from <group> tags when they
    are inside a <search> view.
    """

    files_to_process = tools.get_files(module_path, (".xml",))

    for file_path in files_to_process:
        try:
            content = tools._read_content(file_path)
            parser = etree.XMLParser(recover=True)
            try:
                # lxml does not accept unicode strings with XML declaration,
                # so parse from bytes to be safe.
                tree = etree.parse(BytesIO(content.encode("utf-8")), parser)
                root = tree.getroot()
            except Exception:
                # If full-parse fails, skip this file
                continue

            changed = False

            # Find all <search> elements and remove expand/string from <group> children
            for search in root.findall(".//search"):
                for group in search.findall(".//group"):
                    for attr in ("expand", "string"):
                        if attr in group.attrib:
                            del group.attrib[attr]
                            changed = True

            if changed:
                # Write back modified tree
                new_content = etree.tostring(
                    root, encoding="utf-8", xml_declaration=True
                ).decode("utf-8")
                new_content = new_content.replace(
                    "<?xml version='1.0' encoding='utf-8'?>",
                    '<?xml version="1.0" encoding="utf-8"?>',
                )
                if not new_content.endswith("\n"):
                    new_content += "\n"
                tools._write_content(file_path, new_content)
                logger.info(
                    f"Removed expand/string attrs from <group> in search views: {file_path}"
                )

        except Exception as e:
            logger.error(f"Error processing XML file {file_path}: {e}")


def migrate_underscore_translate(
    logger, module_path, module_name, manifest_path, migration_steps, tools
):
    """In Odoo 19+ `_` moved from `odoo` to `odoo.tools.translate`.

    Converts:
        from odoo import models, _, fields   → from odoo import models, fields
                                                from odoo.tools.translate import _
        from odoo import _ as translate       → from odoo.tools.translate import _ as translate
        from odoo import (models, _, fields)  → from odoo import (models, fields)
                                                from odoo.tools.translate import _
        from odoo import _, models  # noqa    → from odoo import models  # noqa
                                                from odoo.tools.translate import _
    """
    files_to_process = tools.get_files(module_path, (".py",))

    single_import_re = re.compile(
        r'^(?P<indent>[ \t]*)from odoo import (?P<names>.+)$'
    )
    multi_import_re = re.compile(
        r'^(?P<indent>[ \t]*)from odoo import\s*\((?P<body>.*?)\)\s*$',
        re.MULTILINE | re.DOTALL,
    )

    for file in files_to_process:
        try:
            content = tools._read_content(file)
            original = content
            needs_translate = False
            _alias_name = None

            # ------------------------------------------------------------
            # Step 1: Handle multi-line from odoo import (...) blocks
            # ------------------------------------------------------------
            def _replace_multi(match):
                nonlocal needs_translate, _alias_name
                indent = match.group("indent")
                body = match.group("body")

                names = [n.strip() for n in re.split(r',\s*', body)]
                names = [n for n in names if n]

                has_underscore = False
                for n in names:
                    if n == '_':
                        has_underscore = True
                    elif n.startswith('_ as '):
                        has_underscore = True
                        _alias_name = n

                if not has_underscore:
                    return match.group(0)

                needs_translate = True
                new_names = [n for n in names if n != '_'
                             and not n.startswith('_ as ')]
                if not new_names:
                    return ""

                result_lines = [f"{indent}from odoo import ("]
                for i, name in enumerate(new_names):
                    result_lines.append(f"{indent}    {name},")
                result_lines.append(f"{indent})")
                return "\n".join(result_lines)

            content = multi_import_re.sub(_replace_multi, content)

            # ------------------------------------------------------------
            # Step 2: Handle single-line from odoo import ... lines
            # ------------------------------------------------------------
            lines = content.split("\n")
            new_lines = []

            for line in lines:
                m = single_import_re.match(line)
                if m:
                    raw_names = m.group("names")
                    comment_match = re.match(r'(.+?)(\s*#.*)$', raw_names)
                    if comment_match:
                        clean_names = comment_match.group(1)
                        trail_comment = comment_match.group(2)
                    else:
                        clean_names = raw_names
                        trail_comment = ""

                    if re.search(r'\b_\b', raw_names):
                        names = [n.strip() for n in clean_names.split(",")]
                        new_names = [n for n in names
                                     if n != '_' and not n.startswith('_ as ')]

                        for n in names:
                            if n.startswith('_ as '):
                                _alias_name = n

                        if ('_' in names and set(new_names) != set(names)) or \
                           any(n.startswith('_ as ') for n in names):
                            needs_translate = True
                            indent = m.group("indent")
                            if new_names:
                                rebuild = (
                                    f"{indent}from odoo import "
                                    f"{', '.join(new_names)}"
                                )
                                if trail_comment:
                                    rebuild += trail_comment
                                new_lines.append(rebuild)
                            continue
                new_lines.append(line)

            content = "\n".join(new_lines)

            # ------------------------------------------------------------
            # Step 3: Insert the new import
            # ------------------------------------------------------------
            if _alias_name:
                translate_import = f"from odoo.tools.translate import {_alias_name}"
                check_line = "from odoo.tools.translate import _"
            else:
                translate_import = "from odoo.tools.translate import _"
                check_line = translate_import

            if needs_translate and check_line not in content:
                if _alias_name and translate_import in content:
                    pass  # already present
                elif not _alias_name:
                    pass
                else:
                    needs_translate = False

            if needs_translate and check_line not in content:
                new_lines = content.split("\n")
                last_import_idx = -1
                for i, line in enumerate(new_lines):
                    stripped = line.strip()
                    if stripped.startswith("from ") or stripped.startswith("import "):
                        last_import_idx = i
                    elif (
                        stripped
                        and not stripped.startswith("#")
                        and not stripped.startswith('"""')
                        and not stripped.startswith("'''")
                    ):
                        break
                if last_import_idx >= 0:
                    new_lines.insert(last_import_idx + 1, translate_import)
                else:
                    new_lines.insert(0, translate_import)
                content = "\n".join(new_lines)

            if content != original:
                tools._write_content(file, content)
                logger.info(
                    f"Migrated _ translate import to odoo.tools.translate "
                    f"in: {file}"
                )

        except Exception as e:
            logger.error(f"Error processing file {file}: {str(e)}")


class MigrationScript(BaseMigrationScript):
    _GLOBAL_FUNCTIONS = [
        upgrade_sql_constraints,
        migrate_expression_to_domain,
        _remove_group_attrs_in_search_views,
        migrate_underscore_translate,
    ]
