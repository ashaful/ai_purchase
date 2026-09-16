from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    ai_purchase_openai_api_key = fields.Char(
        string='OpenAI API Key',
        config_parameter='ai_purchase_pdf.openai_api_key',
        groups='base.group_system',
    )
    ai_purchase_openai_model = fields.Char(
        string='OpenAI Model',
        default='gpt-5.6-luna',
        config_parameter='ai_purchase_pdf.openai_model',
        help='Vision-capable model used to read supplier PDFs.',
    )
    ai_purchase_openai_endpoint = fields.Char(
        string='OpenAI Responses Endpoint',
        default='https://api.openai.com/v1/responses',
        config_parameter='ai_purchase_pdf.openai_endpoint',
    )
    ai_purchase_timeout = fields.Integer(
        string='AI Timeout (seconds)',
        default=120,
        config_parameter='ai_purchase_pdf.timeout',
    )
    ai_purchase_max_file_mb = fields.Integer(
        string='Maximum PDF Size (MB)',
        default=20,
        config_parameter='ai_purchase_pdf.max_file_mb',
    )
    ai_purchase_match_threshold = fields.Float(
        string='Automatic Match Threshold',
        default=0.90,
        config_parameter='ai_purchase_pdf.match_threshold',
        help='Minimum similarity score (0 to 1) for automatic vendor/product matching.',
    )
    ai_purchase_default_approver_id = fields.Many2one(
        'res.users',
        string='Default Purchase Approver',
        config_parameter='ai_purchase_pdf.default_approver_id',
        domain="[('share', '=', False)]",
        help='User who receives the approval activity after the draft RFQ is created.',
    )
