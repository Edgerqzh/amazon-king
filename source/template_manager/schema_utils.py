"""Bounded, local-only product type schema interpretation shared by generation and conversion.

This is deliberately not a replacement for Amazon's JSON-schema validator. Unsupported or
ambiguous shapes stay intact as one advanced JSON attribute, never a guessed branch.
"""
import copy
import json
import math
import re

MAX_DEPTH = 32
MAX_NODES = 50000
MAX_JSON_BYTES = 256 * 1024
# Amazon's own product type schemas are far larger than any attribute JSON a person types.
SCHEMA_MAX_NODES = 3000000
SCHEMA_MAX_DEPTH = 100
MAX_RESOLVE_STEPS = 1000000
NAME = re.compile(r'[A-Za-z0-9_]{1,160}')
GENERATED_FORMAT = 'spapi-fill-v1'
MANIFEST_SHEET = 'Schema Manifest'
# Sheets a generated workbook carries for the software, not for product data: the manifest (28,000-character
# chunks), the dropdown lists and the field help. Generic upload readers skip them when the manifest is present.
MACHINE_SHEETS = (MANIFEST_SHEET, 'Enums', 'Data Definitions')
# Non-binding values from Amazon's "open enum": anyOf [{type T}, {type T, enum, enumNames}].
# Stored as [[code, display name or None], ...]; never used to reject a value.
SUGGESTIONS = 'x-suggestions'
# Informational keywords (JSON Schema annotations plus Amazon's editable / hidden / $lifecycle / enumDeprecated).
ANNOTATIONS = frozenset({'title', 'description', '$comment', 'examples', 'default', 'editable', 'hidden', 'deprecated', 'readOnly', 'writeOnly', '$lifecycle', 'enumDeprecated'})
SCALAR_TYPES = ('string', 'number', 'integer', 'boolean')


class SchemaError(ValueError):
    pass


def _scalar_ok(kind, value):
    if kind == 'boolean':
        return type(value) is bool
    if kind == 'string':
        return isinstance(value, str)
    if type(value) is bool or not isinstance(value, (int, float)) or not math.isfinite(value):
        return False
    return kind == 'number' or float(value).is_integer()


def _union_suggestions(*groups):
    result, seen = [], set()
    for pairs in groups:
        for code, name in pairs:
            marker = (type(code).__name__, code)
            if marker not in seen:
                seen.add(marker)
                result.append([code, name])
    return result


def _clean_suggestions(value, kind):
    if not isinstance(value, list) or any(not isinstance(p, list) or len(p) != 2 or not _scalar_ok(kind, p[0]) or not (p[1] is None or isinstance(p[1], str)) for p in value):
        return None
    return value


def _open_enum(base, variants):
    """Amazon's anyOf [{type T}, {type T, enum, enumNames}] accepts any T; the enum is only a list of suggestions.

    Returns {'type': T, SUGGESTIONS: [[code, name], ...]} for exactly that shape: one scalar type in every
    branch, at least one branch without enum, and branches adding nothing but enum / enumNames / annotations.
    Anything that really constrains (other types, required sets, ranges, lengths...) returns None.
    """
    declared, kinds, unconstrained, groups = base.get('type'), set(), False, []
    for variant in variants:
        kind = variant.get('type', declared)
        if not isinstance(kind, str) or kind not in SCALAR_TYPES or set(variant) - ANNOTATIONS - {'type', 'enum', 'enumNames', SUGGESTIONS}:
            return None
        kinds.add(kind)
        if SUGGESTIONS in variant:
            nested = _clean_suggestions(variant[SUGGESTIONS], kind)
            if nested is None:
                return None
            groups.append(nested)
        if 'enum' not in variant:
            unconstrained = True
            continue
        codes, names = variant['enum'], variant.get('enumNames')
        if not isinstance(codes, list) or not codes or any(not _scalar_ok(kind, code) for code in codes):
            return None
        if not isinstance(names, list) or len(names) != len(codes):
            names = [None] * len(codes)
        retired = variant.get('enumDeprecated') if isinstance(variant.get('enumDeprecated'), list) else []
        groups.append([[code, name if isinstance(name, str) else None] for code, name in zip(codes, names) if code not in retired])
    if len(kinds) != 1 or not unconstrained or (declared is not None and kinds != {declared}):
        return None
    result = {'type': kinds.pop()}
    if groups:
        result[SUGGESTIONS] = _union_suggestions(*groups)
    return result


FORMATS = 'x-formats'
# Formats no single string can satisfy twice, so oneOf over them is the same as anyOf.
EXCLUSIVE_FORMATS = frozenset({'date', 'date-time'})
FORMAT_HINTS = {'date': '日期，如 2026-11-20', 'date-time': '日期时间，如 2026-11-20T09:30:00Z'}


def _format_choice(base, variants):
    """Amazon's dates: oneOf [{type string, format date}, {type string, format date-time}] is one string column."""
    declared, kinds, formats = base.get('type'), set(), []
    for variant in variants:
        kind = variant.get('type', declared)
        if kind != 'string' or set(variant) - ANNOTATIONS - {'type', 'format'} or variant.get('format') not in EXCLUSIVE_FORMATS:
            return None
        kinds.add(kind)
        if variant['format'] in formats:return None
        formats.append(variant['format'])
    if kinds != {'string'} or (declared is not None and declared != 'string'):
        return None
    return {'type': 'string', FORMATS: formats}


def suggested(node):
    """Non-binding suggestion codes of a resolved node, or None."""
    pairs = node.get(SUGGESTIONS) if isinstance(node, dict) else None
    return [pair[0] for pair in pairs] if isinstance(pairs, list) else None


def strict_json(raw, limit=MAX_JSON_BYTES, max_nodes=MAX_NODES, max_depth=MAX_DEPTH):
    if not isinstance(raw, (str, bytes)) or len(raw.encode('utf-8') if isinstance(raw, str) else raw) > limit:
        raise ValueError('JSON 内容过大或格式无效')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('JSON 不允许重复键：' + key[:80])
            result[key] = value
        return result
    def constant(value):
        raise ValueError('JSON 不允许 NaN / Infinity')
    try:
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        count = 0
        def visit(value, depth=0):
            nonlocal count
            count += 1
            if depth > max_depth or count > max_nodes:
                raise ValueError('JSON 层级或元素过多')
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError('JSON 数字超出范围')
            if isinstance(value, dict):
                for item in value.values():visit(item, depth + 1)
            elif isinstance(value, list):
                for item in value:visit(item, depth + 1)
        visit(result)
        return result
    except (RecursionError, UnicodeError, json.JSONDecodeError) as e:
        raise ValueError('JSON 格式无效，请填写完整属性 JSON') from e


CONDITION_PARTS = ('if', 'then', 'else')


def _split_conditions(node):
    """Take a node's own if/then/else and not out as separate conditions (each still applies on its own)."""
    rest, conditions = dict(node), []
    if any(key in rest for key in CONDITION_PARTS):
        conditions.append({key: rest.pop(key) for key in CONDITION_PARTS if key in rest})
    if 'not' in rest:
        conditions.append({'not': rest.pop('not')})
    return rest, conditions


def _merge(left, right):
    if any(key in left and key in right and left[key] != right[key] for key in CONDITION_PARTS + ('not',)):
        # Amazon defines some attributes in several allOf branches, each with its own if/then
        # (e.g. fulfillment_availability items). Both conditions apply, so keep both as allOf entries.
        left, first = _split_conditions(left)
        right, second = _split_conditions(right)
        out = _merge(left, right)
        kept = out.get('allOf', []) if isinstance(out.get('allOf'), list) else []
        for condition in first + second:
            if condition not in kept:kept = kept + [condition]
        out['allOf'] = kept
        return out
    out = copy.deepcopy(left)
    for key, value in right.items():
        if key not in out:
            out[key] = copy.deepcopy(value)
        elif out[key] == value:
            continue
        elif key == 'properties' and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = {**out[key]}
            for name, node in value.items():
                out[key][name] = _merge(out[key][name], node) if name in out[key] else copy.deepcopy(node)
        elif key == 'required' and isinstance(out[key], list) and isinstance(value, list):
            out[key] = list(dict.fromkeys(out[key] + value))
        elif key == 'enum' and isinstance(out[key], list) and isinstance(value, list):
            out[key] = [v for v in out[key] if v in value]
            if not out[key]:raise SchemaError('allOf 枚举互相矛盾')
        elif key in ('minimum', 'minItems', 'minLength', 'minProperties'):
            out[key] = max(out[key], value)
        elif key in ('maximum', 'maxItems', 'maxLength', 'maxProperties'):
            out[key] = min(out[key], value)
        elif key in ('title', 'description', '$comment', 'examples', 'default', 'enumNames'):
            # Annotations never select a product fact or alter validation.
            continue
        elif key in ('items',) and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value)
        elif key == 'allOf' and isinstance(out[key], list) and isinstance(value, list):
            # Both sides only add conditions; all of them still apply.
            out[key] = out[key] + [v for v in value if v not in out[key]]
        elif key == SUGGESTIONS and isinstance(out[key], list) and isinstance(value, list):
            # Suggestions never restrict a value, so both sides' hints stay available.
            out[key] = _union_suggestions(out[key], value)
        else:
            raise SchemaError('组合规则不明确：' + key)
    for lo, hi in [('minimum', 'maximum'), ('minItems', 'maxItems'), ('minLength', 'maxLength')]:
        if lo in out and hi in out and out[lo] > out[hi]:raise SchemaError('allOf 范围互相矛盾')
    return out


class Resolver:
    def __init__(self, schema):
        self.schema = schema if isinstance(schema, dict) else {}
        self.steps = 0

    def lookup(self, ref):
        """The raw (unresolved) target of a local JSON pointer such as #/$defs/language_tag."""
        if not isinstance(ref, str) or not ref.startswith('#/'):
            raise SchemaError('不支持外部或循环引用')
        target = self.schema
        for part in ref[2:].split('/'):
            part = part.replace('~1', '/').replace('~0', '~')
            if not isinstance(target, dict) or part not in target:raise SchemaError('本地引用不存在')
            target = target[part]
        return target

    def resolve(self, node, trail=(), depth=0):
        self.steps += 1
        if self.steps > MAX_RESOLVE_STEPS or depth > MAX_DEPTH or not isinstance(node, dict):
            raise SchemaError('规则层级或引用过多 / 非对象规则')
        node = copy.deepcopy(node)
        ref = node.pop('$ref', None)
        if ref is not None:
            if not isinstance(ref, str) or not ref.startswith('#/') or ref in trail:
                raise SchemaError('不支持外部或循环引用')
            node = _merge(self.resolve(self.lookup(ref), trail + (ref,), depth + 1), node)
        variants = node.pop('allOf', [])
        if not isinstance(variants, list):raise SchemaError('allOf 格式无效')
        conditions = []
        for variant in variants:
            resolved = self.resolve(variant, trail, depth + 1)
            # Conditions nested in an allOf branch stay conditions; they are never merged into plain rules.
            nested = resolved.pop('allOf', [])
            conditions.extend(c for c in nested if c not in conditions)
            if any(key in resolved for key in ('if', 'then', 'else', 'not', 'dependentSchemas', 'dependentRequired', 'dependencies')):
                conditions.append(resolved)
            elif resolved:
                node = _merge(node, resolved)
        if conditions:
            node['allOf'] = conditions
        for kind in ('oneOf', 'anyOf'):
            if kind in node:
                variants = node.pop(kind)
                if not isinstance(variants, list) or not variants:raise SchemaError('分支规则为空')
                resolved = [self.resolve(v, trail, depth + 1) for v in variants]
                # Amazon's open enum (marketplace_id, language_tag, many values) is one plain type plus suggestions.
                collapsed = _open_enum(node, resolved) if kind == 'anyOf' and len(resolved) > 1 else None
                if collapsed is None and len(resolved) > 1:
                    collapsed = _format_choice(node, resolved)
                if collapsed is not None:
                    node = _merge(node, collapsed)
                    continue
                if (kind == 'oneOf' and len(resolved) > 1) or any(v != resolved[0] for v in resolved[1:]):raise SchemaError('分支规则有歧义，须填写完整 JSON')
                node = _merge(node, resolved[0])
        return node


def node_type(node):
    kind = node.get('type')
    if isinstance(kind, list):
        if len(kind) != 1:raise SchemaError('可空或多类型规则须完整 JSON')
        kind = kind[0]
    if kind is None:
        kind = 'object' if 'properties' in node else 'array' if 'items' in node else None
    if kind not in ('array', 'object', 'string', 'integer', 'number', 'boolean', 'null'):
        raise SchemaError('没有明确的数据类型')
    return kind


def attribute_node(schema, name):
    resolver = Resolver(schema)
    root = resolver.resolve(schema or {})
    node = (root.get('properties') or {}).get(name)
    return resolver.resolve(node) if isinstance(node, dict) else None


def leaf(schema, path):
    try:
        resolver = Resolver(schema)
        root = resolver.resolve(schema or {})
        node = (root.get('properties') or {}).get(path[0][0])
        for name, _, _ in path[1:]:
            node = resolver.resolve(node)
            if node_type(node) == 'array':node = resolver.resolve(node.get('items'))
            node = (node.get('properties') or {}).get(name)
        try:
            return resolver.resolve(node)
        except SchemaError:
            # Legacy scalar templates already support display-name enums in anyOf.
            # Generation never flattens these ambiguous leaves; retain the old conversion path.
            if isinstance(node, dict) and any(key in node for key in ('anyOf', 'oneOf')) and '$ref' not in node:
                return node
            return {}
    except (SchemaError, TypeError, KeyError):
        return {}


def conditional_names(node):
    """Only flag dependencies; never promote a conditional branch to unconditional required."""
    found = set()
    def visit(value, conditional=False, depth=0):
        if depth > MAX_DEPTH or not isinstance(value, dict):return
        if conditional:
            found.update(v for v in value.get('required', []) if isinstance(v, str))
        for key in ('then', 'else', 'if', 'dependentSchemas'):
            branch = value.get(key)
            if key == 'dependentSchemas' and isinstance(branch, dict):
                for item in branch.values():visit(item, True, depth + 1)
            else:visit(branch, True, depth + 1)
        for key in ('dependentRequired', 'dependencies'):
            for values in (value.get(key) or {}).values():
                if isinstance(values, list):found.update(v for v in values if isinstance(v, str))
                else:visit(values, True, depth + 1)
        for key in ('allOf', 'oneOf', 'anyOf'):
            for item in value.get(key, []):visit(item, conditional or key != 'allOf', depth + 1)
    visit(node)
    return found


def compile_fields(schema, market, max_columns=3000):
    resolver = Resolver(schema)
    try:root = resolver.resolve(schema)
    except SchemaError as e:
        raise ValueError('顶层字段规则无法安全展开：' + str(e)) from e
    props = root.get('properties') or {}
    if not isinstance(props, dict) or not props:raise ValueError('字段规则没有商品属性')
    if len(props) > max_columns - 2:raise ValueError('商品属性超过 3000 列上限，无法生成完整填写表')
    required = set(root.get('required') or [])
    if required - set(props):raise ValueError('字段规则缺少必填属性定义，无法生成完整填写表')
    conditional = conditional_names(schema)
    if conditional - set(props):raise ValueError('字段规则缺少条件必填属性定义，无法生成完整填写表')
    fields = [dict(key='contribution_sku', label='本软件 API 上架（非后台原版） / SKU（自行提供）', required=True, conditional=False, value_type='string', description='卖家自己的 SKU；本工具不会自动生成 SKU。'),
              dict(key='product_type', label='商品类型 / Product Type', required=True, conditional=False, value_type='string', description='结构配置；填写流程使用本表声明的唯一商品类型。')]
    warnings = ['本软件 API 上架填写表；不是 Seller Central 原版 Excel，不能直接上传卖家后台。', '条件规则由亚马逊预检最终判断；不自动填写商品事实，不自动提交。']
    per_attr = []
    def human(name):
        return str(name).replace('_', ' ').strip().title()
    def join_label(parent, child):
        child = str(child)
        return parent if parent.casefold() == child.casefold() else parent + ' / ' + child
    def field(key, node, mandatory, condition, label):
        kind = node_type(node)
        if kind in ('object', 'array', 'null'):raise SchemaError('复杂叶节点')
        result = dict(key=key, label=label, required=bool(mandatory), conditional=bool(condition and not mandatory), value_type=kind, description=str(node.get('description') or '')[:12000])
        formats = node.get(FORMATS) if isinstance(node.get(FORMATS), list) else [node['format']] if isinstance(node.get('format'), str) else []
        hints = [FORMAT_HINTS[f] for f in formats if f in FORMAT_HINTS]
        if hints:
            result['format'] = formats
            result['description'] = ('填写格式：' + ' 或 '.join(hints) + '。' + result['description'])[:12000]
        options = node.get('enum')
        if isinstance(options, list):
            if len(options)>5000 or any(isinstance(v, (dict, list)) or v is None for v in options):raise SchemaError('枚举过大或复杂')
            result['options'] = [str(v).lower() if isinstance(v, bool) else str(v) for v in options]
        else:
            # Open-enum hints become a non-enforcing dropdown; too many hints are simply not offered.
            hints = suggested(node)
            if hints and len(hints) <= 5000:
                result['suggestions'] = [str(v).lower() if isinstance(v, bool) else str(v) for v in hints]
        return result
    def expand(node, key, mandatory, condition, depth=0, root_name='', label=''):
        if depth > 12:raise SchemaError('嵌套过深')
        node = resolver.resolve(node)
        label = label or str(node.get('title') or human(root_name or key))
        kind = node_type(node)
        if kind == 'array':
            low, high = node.get('minItems', 0), node.get('maxItems')
            if type(low) is not int or low < 0 or (high is not None and (type(high) is not int or high < low or high > 5)) or low > 5:
                raise SchemaError('重复位置超过五个或范围无效')
            count = high if high is not None else max(low, 5 if root_name == 'bullet_point' and depth == 0 else 1)
            if not count:raise SchemaError('空数组规则')
            item = resolver.resolve(node.get('items'))
            if depth > 0 and node_type(item) in ('string', 'integer', 'number', 'boolean'):
                # A list of plain values inside an attribute (e.g. color.standardized_values, the colour family):
                # one column; several values are separated by ";".
                result = field(key, item, mandatory and low >= 1, condition, label)
                result['list_max'] = high
                if high != 1:
                    result['description'] = ('可填多个，用 ; 分隔' + ('（最多 %d 个）' % high if high else '') + '。' + result['description'])[:12000]
                return [result]
            if node_type(item) != 'object':raise SchemaError('数组元素不是明确对象')
            item_props = item.get('properties') or {}
            if not item_props or item.get('additionalProperties') not in (None, False) or item.get('patternProperties'):
                raise SchemaError('数组对象没有完整固定字段')
            selectors = ''
            editable = {}
            for name, child in item_props.items():
                if not NAME.fullmatch(name):raise SchemaError('字段名不能转换为路径')
                chosen = None
                if name in ('marketplace_id', 'language_tag', 'audience', 'currency'):
                    resolved = resolver.resolve(child)
                    if node_type(resolved) == 'string':
                        # A binding enum and open-enum suggestions both name the values this selector takes.
                        options, hints = resolved.get('enum'), suggested(resolved)
                        if name == 'marketplace_id':
                            if any(isinstance(values, list) and market not in values for values in (options, hints)):raise SchemaError('站点规则与所选站点不同')
                            if 'const' in resolved and resolved['const'] != market:raise SchemaError('站点常量不同')
                            chosen = market
                        elif isinstance(options, list) and len(options) == 1:chosen = options[0]
                        elif 'const' in resolved:chosen = resolved['const']
                        elif options is None and isinstance(hints, list) and len(hints) == 1:chosen = hints[0]
                if isinstance(chosen, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', chosen):selectors += '[' + name + '=' + chosen + ']'
                else:editable[name] = child
            if not editable:raise SchemaError('属性只有选择器；请提供完整 JSON')
            result = []
            item_required = set(item.get('required') or [])
            if item_required - set(item_props):raise SchemaError('缺少必填子字段定义')
            conditional_children = conditional_names(item)
            for slot in range(1, count + 1):
                for name, child in editable.items():
                    child_required = mandatory and slot <= low and name in item_required
                    slot_label = label + (' ' + str(slot) if count > 1 else '')
                    child_label = join_label(slot_label, (child.get('title') if isinstance(child, dict) else '') or human(name))
                    result += expand(child, key + selectors + '#' + str(slot) + '.' + name, child_required,
                                     condition or name in conditional_children or (name in item_required and not child_required), depth + 1, root_name, child_label)
                    if len(result)>max_columns:raise SchemaError('展开列过多')
            return result
        if kind == 'object':
            children = node.get('properties') or {}
            if not children or node.get('additionalProperties') not in (None, False) or node.get('patternProperties'):raise SchemaError('开放或不明确对象')
            child_required = set(node.get('required') or [])
            if child_required - set(children):raise SchemaError('缺少必填子字段')
            cond = conditional_names(node)
            result = []
            for name, child in children.items():
                if not NAME.fullmatch(name):raise SchemaError('字段名不能转换为路径')
                result += expand(child, key + '.' + name, mandatory and name in child_required, condition or name in cond or (name in child_required and not mandatory), depth + 1, root_name, join_label(label, (child.get('title') if isinstance(child, dict) else '') or human(name)))
                if len(result)>max_columns:raise SchemaError('展开列过多')
            return result
        return [field(key, node, mandatory, condition, label)]
    for name, node in props.items():
        def fallback(reason):
            return [dict(key=name + '::json', label=str((node.get('title') if isinstance(node, dict) else '') or human(name)) + '（高级完整 JSON）', required=name in required, conditional=name in conditional and name not in required, value_type='json', description='完整属性 JSON，不是单个 value。' + reason, json_attribute=name)]
        try:
            if not NAME.fullmatch(name) or name in ('contribution_sku', 'product_type'):raise SchemaError('属性名不支持路径')
            resolved = resolver.resolve(node)
            if node_type(resolved) != 'array':raise SchemaError('对象或标量根属性')
            expanded = expand(node, name, name in required, name in conditional, root_name=name)
        except (SchemaError, TypeError, ValueError) as e:
            expanded = fallback(str(e))
            warnings.append(name + ' 使用高级 JSON：' + str(e))
        per_attr.append((name, node, expanded, fallback))
    total = 2 + sum(len(item[2]) for item in per_attr)
    if total > max_columns:
        # Collapse the widest whole attributes first. Never leave a partial scalar expansion.
        for index in sorted(range(len(per_attr)), key=lambda i: len(per_attr[i][2]), reverse=True):
            if total <= max_columns:break
            name, node, expanded, _ = per_attr[index]
            if len(expanded) > 1:
                replacement = [dict(key=name+'::json', label=str((node.get('title') if isinstance(node, dict) else '') or human(name))+'（高级完整 JSON）', required=name in required, conditional=name in conditional and name not in required, value_type='json', description='列数上限：请填写完整属性 JSON。', json_attribute=name)]
                total -= len(expanded) - 1
                per_attr[index] = (name, node, replacement, None)
                warnings.append(name + ' 因列数上限改用高级 JSON')
    for _, _, expanded, _ in per_attr:fields.extend(expanded)
    if len({f['key'] for f in fields}) != len(fields):raise ValueError('字段规则产生重复列')
    return fields, warnings


def validate_json_attribute(value, node, schema):
    """Enforce known structural types on complete JSON, not full conditional business rules."""
    resolver = Resolver(schema)
    def check(value, node, depth=0):
        if depth > MAX_DEPTH:raise ValueError('属性 JSON 层级过深')
        try:node = resolver.resolve(node)
        except SchemaError:
            # Ambiguous branches still have a shared base type, or individually checkable alternatives.
            base = {k:v for k,v in node.items() if k not in ('allOf','oneOf','anyOf')}
            alternatives = node.get('oneOf') or node.get('anyOf')
            if alternatives:
                matches = 0
                for alternative in alternatives:
                    try:check(value, _merge(base, alternative), depth + 1);matches += 1
                    except (ValueError, TypeError):pass
                if matches == 0 or ('oneOf' in node and matches != 1):raise ValueError('属性 JSON 不符合分支类型')
                return
            if '$ref' in base:
                # A local reference whose target keeps real branches: check against that target itself,
                # exactly as if it were written inline. Constraining siblings next to $ref stay unsupported.
                try:target = resolver.lookup(base['$ref'])
                except SchemaError:target = None
                if not isinstance(target, dict) or set(base) - ANNOTATIONS - {'$ref'}:raise ValueError('属性 JSON 的本地引用无法安全解析')
                check(value, target, depth + 1)
                for alternative in node.get('allOf', []):check(value, alternative, depth + 1)
                return
            for alternative in node.get('allOf', []):check(value, alternative, depth + 1)
            node = base
        kind = node.get('type')
        types = kind if isinstance(kind, list) else [kind] if kind else ['object'] if 'properties' in node else ['array'] if 'items' in node else []
        actual = 'null' if value is None else 'boolean' if type(value) is bool else 'integer' if type(value) is int else 'number' if type(value) is float else 'string' if isinstance(value, str) else 'array' if isinstance(value, list) else 'object'
        if types and actual not in types and not (actual == 'integer' and 'number' in types):raise ValueError('属性 JSON 数据类型不符：应为 ' + '/'.join(types))
        if 'enum' in node and not any(type(value) is type(v) and value == v for v in node['enum']):raise ValueError('属性 JSON 枚举值无效')
        if 'const' in node and (type(value) is not type(node['const']) or value != node['const']):raise ValueError('属性 JSON 常量不符')
        if isinstance(value, dict):
            for name, child in value.items():
                prop = (node.get('properties') or {}).get(name)
                if isinstance(prop, dict):check(child, prop, depth + 1)
                elif node.get('additionalProperties') is False:raise ValueError('属性 JSON 包含未知子字段：' + name)
            missing = set(node.get('required') or []) - set(value)
            if missing:raise ValueError('属性 JSON 缺少必填子字段：' + ', '.join(sorted(missing)))
        if isinstance(value, list):
            if len(value) < node.get('minItems', 0) or ('maxItems' in node and len(value) > node['maxItems']):raise ValueError('属性 JSON 数组数量超出规则')
            if isinstance(node.get('items'), dict):
                for child in value:check(child, node['items'], depth + 1)
    check(value, node)
    return value
