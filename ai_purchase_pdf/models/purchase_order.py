from odoo import _, api, fields, models
from odoo.exceptions import UserError


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    ai_purchase_document_id = fields.Many2one(
        'ai.purchase.document',
        string='AI Source Document',
        readonly=True,
        copy=False,
        ondelete='set null',
        index=True,
    )
    ai_approval_state = fields.Selection(
        [
            ('none', 'Not Requested'),
            ('pending', 'Approval Pending'),
            ('approved', 'Human Approved'),
            ('rejected', 'Rejected'),
        ],
        string='AI Purchase Approval',
        default='none',
        tracking=True,
        copy=False,
    )
    ai_approved_by_id = fields.Many2one(
        'res.users',
        string='AI Workflow Approved By',
        readonly=True,
        copy=False,
    )
    ai_approved_date = fields.Datetime(
        string='AI Workflow Approved On',
        readonly=True,
        copy=False,
    )

    _AI_WORKFLOW_FIELDS = {
        'ai_purchase_document_id',
        'ai_approval_state',
        'ai_approved_by_id',
        'ai_approved_date',
    }

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get('ai_purchase_workflow'):
            for vals in vals_list:
                if self._AI_WORKFLOW_FIELDS.intersection(vals):
                    raise UserError(_('AI purchase workflow fields can only be set by the AI Purchase PDF module.'))
        return super().create(vals_list)

    def write(self, vals):
        if self._AI_WORKFLOW_FIELDS.intersection(vals) and not self.env.context.get('ai_purchase_workflow'):
            raise UserError(_('AI purchase workflow fields are protected and cannot be changed manually.'))

        reviewed_commercial_fields = {
            'partner_id', 'partner_ref', 'currency_id', 'payment_term_id', 'date_order', 'order_line',
        }
        if reviewed_commercial_fields.intersection(vals) and not self.env.context.get('ai_purchase_workflow'):
            locked = self.filtered(
                lambda po: po.ai_purchase_document_id
                and po.ai_approval_state == 'approved'
                and po.state == 'to approve'
            )
            if locked:
                raise UserError(_(
                    'This AI-created RFQ has already passed human review and is waiting for Odoo manager approval. '
                    'Commercial fields cannot be changed at this stage. Cancel/return it to draft and request approval again if corrections are required.'
                ))
        return super().write(vals)

    def _check_ai_purchase_human_approval(self):
        for order in self:
            if order.ai_purchase_document_id and order.ai_approval_state != 'approved':
                raise UserError(_(
                    'This RFQ was created from an AI-processed PDF and cannot be confirmed or approved '
                    'until the assigned employee completes the human approval step from the AI Source Document.'
                ))
        return True

    def button_confirm(self):
        self._check_ai_purchase_human_approval()
        return super().button_confirm()

    def button_approve(self, force=False):
        self._check_ai_purchase_human_approval()
        result = super().button_approve(force=force)
        # If Odoo's own two-step approval finishes later, also close the AI document workflow.
        for order in self.filtered(lambda po: po.state == 'purchase' and po.ai_purchase_document_id):
            document = order.ai_purchase_document_id
            if document.state == 'approved':
                document.write({'state': 'done'})
                document.message_post(body=_('Odoo standard Purchase approval completed. %s is now confirmed.') % order.display_name)
        return result

    def button_draft(self):
        result = super().button_draft()
        # A cancelled AI-created PO returned to draft must be reviewed again before reconfirmation.
        for order in self.filtered('ai_purchase_document_id'):
            order.with_context(ai_purchase_workflow=True).write({
                'ai_approval_state': 'none',
                'ai_approved_by_id': False,
                'ai_approved_date': False,
            })
            document = order.ai_purchase_document_id
            document.write({'state': 'draft_created'})
            document.message_post(body=_(
                '%s was returned to draft. The previous AI-workflow approval is no longer valid; request approval again.'
            ) % order.display_name)
        return result

    def action_open_ai_purchase_document(self):
        self.ensure_one()
        if not self.ai_purchase_document_id:
            raise UserError(_('This RFQ/PO is not linked to an AI purchase document.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('AI Purchase Document'),
            'res_model': 'ai.purchase.document',
            'res_id': self.ai_purchase_document_id.id,
            'view_mode': 'form',
        }


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    _AI_REVIEWED_LINE_FIELDS = {
        'product_id', 'name', 'product_qty', 'product_uom_id', 'price_unit',
        'discount', 'tax_ids', 'date_planned',
    }

    def _check_ai_review_lock(self, vals=None):
        if self.env.context.get('ai_purchase_workflow'):
            return
        if vals is not None and not self._AI_REVIEWED_LINE_FIELDS.intersection(vals):
            return
        locked = self.filtered(
            lambda line: line.order_id.ai_purchase_document_id
            and line.order_id.ai_approval_state == 'approved'
            and line.order_id.state == 'to approve'
        )
        if locked:
            raise UserError(_(
                'These RFQ lines already passed the AI-workflow human review and are waiting for Odoo manager approval. '
                'Return the order to draft through the normal correction process and request human approval again before changing commercial line values.'
            ))

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get('ai_purchase_workflow'):
            order_ids = [vals.get('order_id') for vals in vals_list if vals.get('order_id')]
            locked_orders = self.env['purchase.order'].browse(order_ids).filtered(
                lambda po: po.ai_purchase_document_id
                and po.ai_approval_state == 'approved'
                and po.state == 'to approve'
            )
            if locked_orders:
                raise UserError(_(
                    'You cannot add lines to an AI-created RFQ after human review while it is waiting for Odoo manager approval.'
                ))
        return super().create(vals_list)

    def write(self, vals):
        self._check_ai_review_lock(vals)
        return super().write(vals)

    def unlink(self):
        self._check_ai_review_lock()
        return super().unlink()
