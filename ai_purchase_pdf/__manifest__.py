{
    'name': 'AI Purchase PDF to RFQ',
    'version': '19.0.1.1.0',
    'category': 'Purchases',
    'summary': 'Create draft RFQs from supplier PDFs in no-API test mode or AI mode with human approval',
    'description': '''
AI Purchase PDF to RFQ
======================
Upload a supplier quotation or purchase-related PDF. No API Test Mode extracts embedded PDF text locally
with rule-based parsing; OpenAI mode can be enabled later for AI extraction. Existing Odoo vendors/products
are matched, a draft RFQ is created, and human approval is required before confirmation.
    ''',
    'author': 'Custom',
    'license': 'LGPL-3',
    'depends': ['purchase', 'mail'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/ir_sequence_data.xml',
        'views/ai_purchase_document_views.xml',
        'views/purchase_order_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'external_dependencies': {'python': ['requests']},
    'installable': True,
    'application': True,
}
