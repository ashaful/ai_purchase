from odoo import _, api, fields, models
from odoo.exceptions import UserError


PLACEHOLDER_PRODUCT_CODE = 'AI-REVIEW'
PLACEHOLDER_VENDOR_REF = 'AI-VENDOR-REVIEW'


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    ai_pdf_generated = fields.Boolean(string='AI PDF Generated', copy=False, index=True)
    ai_source_filename = fields.Char(string='Source PDF', copy=False)
    ai_source_attachment_id = fields.Many2one(
        'ir.attachment', string='Source PDF Attachment', copy=False, ondelete='set null'
    )
    ai_approver_id = fields.Many2one(
        'res.users', string='AI Approver', copy=False,
        domain="[('share', '=', False)]",
    )
    ai_approval_state = fields.Selection(
        [
            ('pending', 'Pending Human Approval'),
            ('approved', 'Human Approved'),
            ('rejected', 'Rejected'),
        ],
        string='AI Approval', copy=False,
    )
    ai_approved_by_id = fields.Many2one('res.users', string='Approved By', copy=False, readonly=True)
    ai_approved_date = fields.Datetime(string='Approved On', copy=False, readonly=True)
    ai_extraction_data = fields.Text(
        string='AI Extraction Data', copy=False,
        groups='base.group_system',
        help='Structured data returned by Odoo AI. Visible only to administrators.',
    )
    ai_unmatched_line_count = fields.Integer(
        string='Products to Review', compute='_compute_ai_review_counts'
    )
    ai_vendor_needs_review = fields.Boolean(
        string='Vendor Needs Review', compute='_compute_ai_review_counts'
    )

    @api.depends('partner_id', 'order_line.product_id', 'order_line.display_type')
    def _compute_ai_review_counts(self):
        placeholder_products = self.env['product.product'].sudo().search([
            ('default_code', '=', PLACEHOLDER_PRODUCT_CODE),
        ])
        placeholder_product_ids = set(placeholder_products.ids)
        placeholder_vendors = self.env['res.partner'].sudo().search([
            ('ref', '=', PLACEHOLDER_VENDOR_REF),
        ])
        placeholder_vendor_ids = set(placeholder_vendors.ids)
        for order in self:
            order.ai_unmatched_line_count = len(order.order_line.filtered(
                lambda line: not line.display_type and (
                    not line.product_id or line.product_id.id in placeholder_product_ids
                )
            ))
            order.ai_vendor_needs_review = (
                not order.partner_id or order.partner_id.id in placeholder_vendor_ids
            )

    def _check_ai_approver(self):
        self.ensure_one()
        if not self.ai_pdf_generated:
            return
        is_manager = self.env.user.has_group('purchase.group_purchase_manager')
        if self.ai_approver_id and self.env.user != self.ai_approver_id and not is_manager:
            raise UserError(_(
                'This RFQ is assigned to %(approver)s for approval.',
                approver=self.ai_approver_id.display_name,
            ))

    def _check_ai_ready_for_confirmation(self):
        self.ensure_one()
        if not self.ai_pdf_generated:
            return
        if self.ai_vendor_needs_review:
            raise UserError(_(
                'Select the correct vendor before approving this AI-generated RFQ.'
            ))
        if self.ai_unmatched_line_count:
            lines = self.order_line.filtered(
                lambda line: not line.display_type and (
                    not line.product_id or line.product_id.default_code == PLACEHOLDER_PRODUCT_CODE
                )
            )
            descriptions = '\n'.join(
                '- %s' % ((line.ai_source_description or line.name or _('Unmatched line')).split('\n')[0])
                for line in lines[:10]
            )
            raise UserError(_(
                'Select a real Odoo product for every purchase line before approval.\n\n'
                'Lines requiring review:\n%(lines)s',
                lines=descriptions,
            ))

    def _close_ai_approval_activities(self, feedback):
        todo_type = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        for order in self:
            activities = order.activity_ids.filtered(
                lambda a: (not todo_type or a.activity_type_id == todo_type)
                and (not order.ai_approver_id or a.user_id == order.ai_approver_id)
                and 'AI-generated RFQ' in (a.summary or '')
            )
            for activity in activities:
                activity.action_feedback(feedback=feedback)

    def action_ai_approve_confirm(self):
        for order in self:
            if not order.ai_pdf_generated:
                raise UserError(_('This action is only available for AI-generated RFQs.'))
            if order.state not in ('draft', 'sent'):
                raise UserError(_('Only a draft RFQ can be approved with this action.'))
            order._check_ai_approver()
            order._check_ai_ready_for_confirmation()
            order.with_context(ai_internal_approval=True).write({
                'ai_approval_state': 'approved',
                'ai_approved_by_id': self.env.user.id,
                'ai_approved_date': fields.Datetime.now(),
            })
            order.message_post(body=_('AI-generated RFQ approved by %s.') % self.env.user.display_name)
            order._close_ai_approval_activities(_('Reviewed and approved.'))

        # Use Odoo's normal purchase confirmation logic. This intentionally keeps
        # standard Odoo approval thresholds / two-step approval authoritative.
        result = self.button_confirm()
        return result

    def action_ai_reject(self):
        for order in self:
            if not order.ai_pdf_generated:
                continue
            order._check_ai_approver()
            if order.state not in ('draft', 'sent'):
                raise UserError(_('Only a draft RFQ can be rejected.'))
            order.with_context(ai_internal_approval=True).write({
                'ai_approval_state': 'rejected',
                'ai_approved_by_id': False,
                'ai_approved_date': False,
            })
            order.message_post(body=_('AI-generated RFQ rejected by %s. The RFQ remains editable.') % self.env.user.display_name)
            order._close_ai_approval_activities(_('Rejected for correction.'))
        return True

    def action_ai_request_approval(self):
        for order in self:
            if not order.ai_pdf_generated:
                continue
            if order.state not in ('draft', 'sent'):
                raise UserError(_('Approval can only be requested for a draft RFQ.'))
            order.with_context(ai_internal_approval=True).write({
                'ai_approval_state': 'pending',
                'ai_approved_by_id': False,
                'ai_approved_date': False,
            })
            order._create_ai_approval_activity()
            order.message_post(body=_('AI RFQ approval requested from %s.') % (order.ai_approver_id.display_name or self.env.user.display_name))
        return True

    def _create_ai_approval_activity(self):
        todo_type = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        for order in self:
            approver = order.ai_approver_id or self.env.user
            duplicate = order.activity_ids.filtered(
                lambda a: a.user_id == approver and 'AI-generated RFQ' in (a.summary or '')
            )
            if duplicate:
                continue
            order.activity_schedule(
                activity_type_id=todo_type.id if todo_type else False,
                user_id=approver.id,
                summary=_('Review AI-generated RFQ %s') % order.name,
                note=_('Review the source PDF, vendor, products, quantities and prices. Use “Approve & Confirm” only when the RFQ is correct.'),
            )

    def action_open_ai_source_pdf(self):
        self.ensure_one()
        attachment = self.ai_source_attachment_id.exists()
        if not attachment:
            raise UserError(_('The source PDF attachment is not available.'))
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=false',
            'target': 'new',
        }

    def button_confirm(self):
        blocked = self.filtered(
            lambda o: o.ai_pdf_generated and o.ai_approval_state != 'approved'
        )
        if blocked:
            raise UserError(_(
                'This RFQ was created from a PDF by Odoo AI. A human must use “Approve & Confirm” before it can be confirmed.'
            ))
        return super().button_confirm()

    def button_approve(self, force=False):
        blocked = self.filtered(
            lambda o: o.ai_pdf_generated and o.ai_approval_state != 'approved'
        )
        if blocked:
            raise UserError(_(
                'Human approval of the AI-generated RFQ is required before Odoo Purchase approval.'
            ))
        return super().button_approve(force=force)

    def button_draft(self):
        result = super().button_draft()
        self.filtered('ai_pdf_generated').with_context(ai_internal_approval=True).write({
            'ai_approval_state': 'pending',
            'ai_approved_by_id': False,
            'ai_approved_date': False,
        })
        return result


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    ai_source_description = fields.Text(
        string='PDF Description', copy=False,
        help='Original line description extracted from the uploaded PDF.',
    )
