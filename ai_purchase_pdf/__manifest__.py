{
    'name': 'AI Purchase PDF to RFQ',
    'version': '19.0.1.0.0',
    'category': 'Purchases',
    'summary': 'Create draft RFQs from supplier PDFs with AI and require human approval before confirmation',
    'description': '''
AI Purchase PDF to RFQ
======================
Upload a supplier quotation or purchase-related PDF, extract structured purchase data with AI,
match existing Odoo vendors/products, create a draft RFQ, request human approval, and only then
allow the standard Odoo Purchase Order confirmation flow.
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
