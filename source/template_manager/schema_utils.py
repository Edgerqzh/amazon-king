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
NAME = re.compile(r'[A-Za-z0-9_]{1,160}')
GENERATED_FORMAT = 'spapi-fill-v1'
MANIFEST_SHEET = 'Schema Manifest'


class SchemaError(ValueError):
    pass


def strict_json(raw, limit=MAX_JSON_BYTES):
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
            if depth > MAX_DEPTH or count > MAX_NODES:
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


def _merge(left, right):
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
        else:
            raise SchemaError('组合规则不明确：' + key)
    for lo, hi in [('minimum', 'maximum'), ('minItems', 'maxItems'), ('minLength', 'maxLength')]:
        if lo in out and hi in out and out[lo] > out[hi]:raise SchemaError('allOf 范围互相矛盾')
    return out


class Resolver:
    def __init__(self, schema):
        self.schema = schema if isinstance(schema, dict) else {}
        self.steps = 0

    def resolve(self, node, trail=(), depth=0):
        self.steps += 1
        if self.steps > MAX_NODES or depth > MAX_DEPTH or not isinstance(node, dict):
            raise SchemaError('规则层级或引用过多 / 非对象规则')
        node = copy.deepcopy(node)
        ref = node.pop('$ref', None)
        if ref is not None:
            if not isinstance(ref, str) or not ref.startswith('#/') or ref in trail:
                raise SchemaError('不支持外部或循环引用')
            target = self.schema
            for part in ref[2:].split('/'):
                part = part.replace('~1', '/').replace('~0', '~')
                if not isinstance(target, dict) or part not in target:raise SchemaError('本地引用不存在')
                target = target[part]
            node = _merge(self.resolve(target, trail + (ref,), depth + 1), node)
        variants = node.pop('allOf', [])
        if not isinstance(variants, list):raise SchemaError('allOf 格式无效')
        conditions = []
        for variant in variants:
            resolved = self.resolve(variant, trail, depth + 1)
            if any(key in resolved for key in ('if', 'then', 'else', 'dependentSchemas', 'dependentRequired', 'dependencies')):
                conditions.append(resolved)
            else:
                node = _merge(node, resolved)
        if conditions:
            node['allOf'] = conditions
        for kind in ('oneOf', 'anyOf'):
            if kind in node:
                variants = node.pop(kind)
                if not isinstance(variants, list) or not variants:raise SchemaError('分支规则为空')
                resolved = [self.resolve(v, trail, depth + 1) for v in variants]
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
        options = node.get('enum')
        if isinstance(options, list):
            if len(options)>5000 or any(isinstance(v, (dict, list)) or v is None for v in options):raise SchemaError('枚举过大或复杂')
            result['options'] = [str(v).lower() if isinstance(v, bool) else str(v) for v in options]
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
                        options = resolved.get('enum')
                        if name == 'marketplace_id':
                            if isinstance(options, list) and market not in options:raise SchemaError('站点规则与所选站点不同')
                            if 'const' in resolved and resolved['const'] != market:raise SchemaError('站点常量不同')
                            chosen = market
                        elif isinstance(options, list) and len(options) == 1:chosen = options[0]
                        elif 'const' in resolved:chosen = resolved['const']
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
            if '$ref' in base:raise ValueError('属性 JSON 的本地引用无法安全解析')
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
