"""A product type schema shaped like Amazon's real ones: many attributes, $defs, selectors, nested allOf conditions."""
import json
import random


def attr(title, value, required=('value',), max_items=1, extra_props=None):
    props = {'value': value, 'marketplace_id': {'$ref': '#/$defs/marketplace_id'}, 'language_tag': {'$ref': '#/$defs/language_tag'}}
    props.update(extra_props or {})
    return {'title': title, 'description': title + ' description', 'examples': ['x'], 'type': 'array', 'minItems': 1, 'minUniqueItems': 1,
            'maxUniqueItems': max_items, 'selectors': ['marketplace_id', 'language_tag'],
            'items': {'type': 'object', 'required': list(required), 'properties': props, 'additionalProperties': False}}


def schema(market='A2VIGQ35RCS4UG', attributes=400, enum_size=60):
    defs = {'marketplace_id': {'default': market, 'editable': False, 'hidden': True, 'examples': [market], 'type': 'string', 'anyOf': [{'type': 'string'}, {'type': 'string', 'enum': [market], 'enumNames': ['Amazon.ae']}]},
            'language_tag': {'default': 'en_AE', 'editable': False, 'hidden': True, 'examples': ['en_AE'], 'type': 'string', 'anyOf': [{'type': 'string'}, {'type': 'string', 'enum': ['en_AE', 'ar_AE'], 'enumNames': ['English', 'Arabic']}]}}
    properties = {'item_name': attr('Item Name', {'title': 'Item Name', 'type': 'string', 'minLength': 0, 'maxLength': 200}),
                  'brand': attr('Brand', {'title': 'Brand', 'type': 'string', 'maxLength': 100}),
                  'bullet_point': attr('Bullet Point', {'title': 'Bullet Point', 'type': 'string', 'maxLength': 700}, max_items=5),
                  'batteries_required': attr('Batteries Required', {'title': 'Batteries Required', 'type': 'boolean', 'enum': [False, True], 'enumNames': ['No', 'Yes']})}
    for n in range(attributes):
        properties['attr_%03d' % n] = attr('Attribute %d' % n, {'title': 'Attribute %d' % n, 'type': 'string',
                                                                 'enum': ['option_%d' % k for k in range(enum_size)], 'enumNames': ['Option %d' % k for k in range(enum_size)]})
    conditions = [{'if': {'required': ['batteries_required'], 'properties': {'batteries_required': {'contains': {'required': ['value'], 'properties': {'value': {'enum': [True]}}}}}},
                   'then': {'required': ['attr_000']}}]
    nested = {'allOf': [{'if': {'required': ['attr_001']}, 'then': {'required': ['attr_002']}}, {'if': {'required': ['attr_003']}, 'then': {'required': ['attr_004']}}]}
    nested2 = {'allOf': [{'if': {'required': ['attr_005']}, 'then': {'required': ['attr_006']}}]}
    return {'$schema': 'https://schemas.amazon.com/selling-partners/definitions/product-types/meta-schema/v1', '$id': 'https://schemas.amazon.com/x',
            '$comment': 'Amazon product type definition', '$defs': defs, 'description': 'demo', 'type': 'object',
            'required': ['item_name', 'brand'], 'properties': properties, 'allOf': conditions + [nested, nested2, {'not': {'required': ['attr_007', 'attr_008']}}]}


def _leaf(title, kind='string', **extra):
    node = {'title': title, 'description': title + ' - provide the information as it appears on the product.', 'editable': True, 'hidden': False,
            'examples': ['example'], 'type': kind}
    node.update(extra)
    return node


def _items(properties, required, selectors, max_unique=1, title=''):
    """Amazon's top-level attribute: an array with selectors and maxUniqueItems, no maxItems."""
    return {'title': title, 'description': title + ' attribute', 'examples': ['x'], 'type': 'array', 'minItems': 1, 'minUniqueItems': 1,
            'maxUniqueItems': max_unique, 'selectors': list(selectors),
            'items': {'type': 'object', 'required': list(required), 'properties': properties, 'additionalProperties': False}}


MARKETPLACE_REF, LANGUAGE_REF = {'$ref': '#/$defs/marketplace_id'}, {'$ref': '#/$defs/language_tag'}


def _text(title, max_unique=1, language=True, max_length=200):
    properties = {'value': _leaf(title, minLength=1, maxLength=max_length, maxUtf8ByteLength=max_length * 4)}
    if language:properties['language_tag'] = dict(LANGUAGE_REF)
    properties['marketplace_id'] = dict(MARKETPLACE_REF)
    return _items(properties, ['language_tag', 'value'] if language else ['value'], ['marketplace_id', 'language_tag'] if language else ['marketplace_id'], max_unique, title)


def _choice(title, values, kind='string', names=None):
    value = _leaf(title, kind, enum=list(values), enumNames=list(names or [str(v).title() for v in values]))
    return _items({'value': value, 'marketplace_id': dict(MARKETPLACE_REF)}, ['value'], ['marketplace_id'], 1, title)


def _measure(title, units):
    return {'title': title, 'type': 'object', 'required': ['unit', 'value'], 'additionalProperties': False,
            'properties': {'value': _leaf(title + ' Value', 'number', minimum=0), 'unit': _leaf('Unit', enum=list(units), enumNames=[u.title() for u in units])}}


def _price(title):
    schedule = {'type': 'array', 'minItems': 1, 'maxItems': 1, 'items': {'type': 'object', 'required': ['value_with_tax'], 'additionalProperties': False,
                                                                          'properties': {'value_with_tax': _leaf(title, 'number', minimum=0)}}}
    return {'title': title, 'type': 'array', 'minItems': 0, 'maxItems': 1,
            'items': {'type': 'object', 'required': ['schedule'], 'properties': {'schedule': schedule}, 'additionalProperties': False}}


def realistic_schema(market='A2VIGQ35RCS4UG', languages=('en_AE', 'ar_AE'), currency='AED', n_text=60, n_enum=20, enum_size=30, n_conditions=80, seed=1):
    """A LISTING schema with Amazon's real shapes, for regression tests on generation and conversion.

    - $defs marketplace_id / language_tag are Amazon's open enums: anyOf [{type string}, {type string, enum, enumNames}];
    - top-level attributes use selectors + maxUniqueItems (no maxItems); text attributes require language_tag;
    - nested purchasable_offer / dimensions, an identifier with value + type, a color with standardized_values
      (array of strings: an unsupported shape), one genuinely ambiguous anyOf, if/then/else, nested allOf and not.
    """
    rng = random.Random(seed)
    defs = {'marketplace_id': {'default': market, 'editable': False, 'hidden': True, 'examples': ['Amazon'], 'type': 'string',
                               'anyOf': [{'type': 'string'}, {'type': 'string', 'enum': [market], 'enumNames': ['Amazon store']}]},
            'language_tag': {'default': languages[0], 'editable': False, 'hidden': True, 'examples': ['English'], 'type': 'string',
                             'anyOf': [{'type': 'string'}, {'type': 'string', 'enum': list(languages), 'enumNames': ['Language ' + l for l in languages]}]}}
    properties = {
        'item_name': _text('Item Name'),
        'brand': _text('Brand Name', max_length=100),
        'bullet_point': _text('Bullet Point', max_unique=5, max_length=700),
        'product_description': _text('Product Description', max_length=2000),
        'generic_keyword': _text('Generic Keyword', max_length=500),
        'special_feature': _text('Special Features', max_unique=5),
        'manufacturer': _text('Manufacturer'),
        'model_number': _text('Model Number', language=False),
        'merchant_suggested_asin': _items({'value': _leaf('Merchant Suggested ASIN', minLength=10, maxLength=10), 'marketplace_id': dict(MARKETPLACE_REF)},
                                          ['value'], ['marketplace_id'], 1, 'Merchant Suggested ASIN'),
        'externally_assigned_product_identifier': _items({'type': _leaf('External Product ID Type', enum=['ean', 'gtin', 'upc'], enumNames=['EAN', 'GTIN', 'UPC']),
                                                          'value': _leaf('External Product ID'), 'marketplace_id': dict(MARKETPLACE_REF)},
                                                         ['type', 'value'], ['marketplace_id', 'type'], 1, 'External Product ID'),
        'supplier_declared_has_product_identifier_exemption': _choice('Product Exemption Status', [False, True], 'boolean', ['No', 'Yes']),
        'condition_type': _choice('Item Condition', ['new_new', 'used_like_new', 'used_good']),
        'batteries_required': _choice('Are batteries required?', [False, True], 'boolean', ['No', 'Yes']),
        'country_of_origin': _choice('Country of Origin', ['CN', 'AE', 'SA', 'US', 'DE'], names=['China', 'United Arab Emirates', 'Saudi Arabia', 'United States', 'Germany']),
        'number_of_items': _choice('Number of Items', [1, 2, 3, 4, 5, 6], 'integer', ['1', '2', '3', '4', '5', '6']),
        'parentage_level': _choice('Parentage Level', ['child', 'parent']),
        'child_parent_sku_relationship': _items({'child_relationship_type': _leaf('Relationship Type', enum=['variation'], enumNames=['Variation']),
                                                 'parent_sku': _leaf('Parent SKU'), 'marketplace_id': dict(MARKETPLACE_REF)},
                                                ['child_relationship_type'], ['marketplace_id'], 1, 'Child Parent SKU Relationship'),
        'list_price': _items({'currency': _leaf('Currency', enum=[currency], enumNames=[currency]), 'value_with_tax': _leaf('List Price', 'number', minimum=0),
                              'marketplace_id': dict(MARKETPLACE_REF)}, ['currency', 'value_with_tax'], ['marketplace_id', 'currency'], 1, 'List Price'),
        'purchasable_offer': _items({'audience': _leaf('Audience', enum=['ALL', 'B2B'], enumNames=['Sell on Amazon', 'Amazon Business (B2B)']),
                                     'currency': _leaf('Currency', enum=[currency], enumNames=[currency]),
                                     'our_price': _price('Your Price'), 'minimum_seller_allowed_price': _price('Minimum Seller Allowed Price'),
                                     'start_at': {'type': 'object', 'required': ['value'], 'additionalProperties': False, 'properties': {'value': _leaf('Offer Start', format='date-time')}},
                                     'marketplace_id': dict(MARKETPLACE_REF)},
                                    ['audience', 'currency', 'marketplace_id'], ['marketplace_id', 'currency', 'audience'], 1, 'Purchasable Offer'),
        'fulfillment_availability': _items({'fulfillment_channel_code': _leaf('Fulfillment Channel Code', minLength=1), 'quantity': _leaf('Quantity', 'integer', minimum=0),
                                            'lead_time_to_ship_max_days': _leaf('Handling Time', 'integer', minimum=0),
                                            'is_inventory_available': _leaf('Inventory Always Available', 'boolean', enum=[False, True], enumNames=['No', 'Yes'])},
                                           ['fulfillment_channel_code'], ['fulfillment_channel_code'], 1, 'Fulfillment Availability'),
        'item_package_dimensions': _items({'length': _measure('Length', ['centimeters', 'inches']), 'width': _measure('Width', ['centimeters', 'inches']),
                                           'height': _measure('Height', ['centimeters', 'inches']), 'marketplace_id': dict(MARKETPLACE_REF)},
                                          ['length', 'width', 'height'], ['marketplace_id'], 1, 'Item Package Dimensions'),
        'main_product_image_locator': _items({'media_location': _leaf('Main Image Location', format='uri', maxLength=4000), 'marketplace_id': dict(MARKETPLACE_REF)},
                                             ['media_location'], ['marketplace_id'], 1, 'Main Image Location'),
        # An open enum on the value itself: any text, suggested codes with display names.
        'material': _items({'value': _leaf('Material', anyOf=[{'type': 'string'}, {'type': 'string', 'enum': ['leather', 'polyester'], 'enumNames': ['Leather', 'Polyester']}]),
                            'language_tag': dict(LANGUAGE_REF), 'marketplace_id': dict(MARKETPLACE_REF)}, ['language_tag', 'value'], ['marketplace_id', 'language_tag'], 1, 'Material'),
        # standardized_values is an array of strings: no fixed object columns, stays advanced JSON.
        'color': _items({'value': _leaf('Color'), 'standardized_values': {'type': 'array', 'items': _leaf('Color Map', enum=['black', 'white'], enumNames=['Black', 'White'])},
                         'language_tag': dict(LANGUAGE_REF), 'marketplace_id': dict(MARKETPLACE_REF)}, ['language_tag', 'value'], ['marketplace_id', 'language_tag'], 1, 'Color'),
        # Branches with different types really constrain: stays advanced JSON.
        'capacity_hint': _items({'value': {'title': 'Capacity', 'anyOf': [{'type': 'string', 'maxLength': 20}, {'type': 'number', 'minimum': 0}]}, 'marketplace_id': dict(MARKETPLACE_REF)},
                                ['value'], ['marketplace_id'], 1, 'Capacity'),
    }
    for i in range(n_text):
        properties['text_attr_%03d' % i] = _text('Text Attribute %d' % i, max_unique=rng.choice([1, 1, 1, 3, 5]))
    for i in range(n_enum):
        properties['enum_attr_%03d' % i] = _choice('Enum Attribute %d' % i, ['option_%d_%d' % (i, k) for k in range(enum_size)])
    names = sorted(properties)
    conditions = []
    for i in range(n_conditions):
        trigger, target = rng.sample(names, 2)
        conditions.append({'if': {'required': [trigger], 'properties': {trigger: {'contains': {'required': ['marketplace_id', 'value'], 'properties': {
                               'marketplace_id': {'enum': [market]}, 'value': {'enum': ['option_%d' % i]}}}}}},
                           'then': {'required': [target]}, 'else': {'properties': {target: {'items': {'required': ['value']}}}}})
    parentage = {'if': {'required': ['parentage_level'], 'properties': {'parentage_level': {'contains': {'required': ['marketplace_id', 'value'],
                                                                                                      'properties': {'marketplace_id': {'enum': [market]}, 'value': {'enum': ['child']}}}}}},
                 'then': {'required': ['child_parent_sku_relationship']}}
    identifier = {'if': {'not': {'required': ['merchant_suggested_asin']}}, 'then': {'required': ['externally_assigned_product_identifier']}}
    required = ['item_name', 'brand', 'bullet_point', 'product_description', 'condition_type', 'country_of_origin', 'batteries_required', 'list_price', 'purchasable_offer', 'fulfillment_availability']
    return {'$schema': 'https://schemas.amazon.com/selling-partners/definitions/product-types/meta-schema/v1',
            '$id': 'https://schemas.amazon.com/selling-partners/definitions/product-types/schema/v1/SUITCASE',
            '$comment': 'Amazon product type definition for SUITCASE product type', '$defs': defs,
            'description': 'Amazon product type definition for SUITCASE product type', 'examples': [{}], 'type': 'object',
            'required': required, 'properties': properties,
            'allOf': conditions + [{'allOf': [parentage, identifier]}, {'not': {'required': ['supplier_declared_has_product_identifier_exemption', 'externally_assigned_product_identifier']}}]}


if __name__ == '__main__':
    data = json.dumps(schema()).encode()
    print(len(data), 'bytes')
    print(len(json.dumps(realistic_schema()).encode()), 'bytes (realistic_schema)')
