{
    'name': 'AI Purchase PDF - Native Odoo AI',
    'version': '19.0.2.0.0',
    'category': 'Purchases',
    'summary': 'Upload a purchase PDF, let Odoo Gemini AI create a draft RFQ, then approve and confirm it.',
    'description': '''
Native Odoo 19 AI purchase workflow:
- Upload a supplier quotation / purchase PDF
- Odoo native AI (Google Gemini provider) extracts purchase data
- Draft RFQ is created immediately
- Original PDF is attached to the RFQ
- A human approver reviews the RFQ
- Approve & Confirm calls Odoo's standard purchase confirmation
''',
    'author': 'Custom',
    'license': 'LGPL-3',
    'depends': ['purchase', 'mail', 'ai_app'],
    'data': [
        'security/ir.model.access.csv',
        'views/ai_purchase_upload_wizard_views.xml',
        'views/purchase_order_views.xml',
    ],
    'installable': True,
    'application': True,
}
