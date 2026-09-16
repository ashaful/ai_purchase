import base64
import difflib
import json
import logging
from datetime import datetime, time

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.ai.utils.llm_api_service import LLMApiService
from odoo.addons.ai.utils.llm_providers import PROVIDERS

from ..models.purchase_order import PLACEHOLDER_PRODUCT_CODE, PLACEHOLDER_VENDOR_REF

_logger = logging.getLogger(__name__)

MAX_PDF_BYTES = 20 * 1024 * 1024


class AIPurchaseUploadWizard(models.TransientModel):
    _name = 'ai.purchase.upload.wizard'
    _description = 'Upload Purchase PDF with Odoo AI'

    file_data = fields.Binary(string='Purchase PDF', required=True, attachment=False)
    file_name = fields.Char(string='Filename')
    approver_id = fields.Many2one(
        'res.users', string='Approver', required=True,
        default=lambda self: self.env.user,
        domain="[('share', '=', False)]",
        help='This employee (or a Purchase Manager) can approve and confirm the generated RFQ.',
    )

    def _validate_pdf(self):
        self.ensure_one()
        if not self.file_data:
            raise UserError(_('Upload a PDF file first.'))
        try:
            raw = base64.b64decode(self.file_data)
        except Exception as exc:
            raise UserError(_('The uploaded file could not be decoded.')) from exc
        if not raw.startswith(b'%PDF'):
            raise UserError(_('Only PDF files are supported.'))
        if len(raw) > MAX_PDF_BYTES:
            raise UserError(_('The PDF is larger than 20 MB. Please upload a smaller file.'))
        return raw

    @api.model
    def _get_google_model(self):
        google = next((provider for provider in PROVIDERS if provider.name == 'google'), None)
        if not google:
            raise UserError(_(
                'Google Gemini is not available in this Odoo AI installation.'
            ))
        style_map = getattr(google, 'response_style_to_llm_model_and_reasoning', {}) or {}
        for style in ('balanced', 'analytical', 'creative'):
            selection = style_map.get(style)
            if selection:
                return selection[0]
        llms = list(getattr(google, 'llms', []) or [])
        for model_id, _label in llms:
            if 'flash' in model_id.lower():
                return model_id
        if llms:
            return llms[0][0]
        raise UserError(_('No Gemini model is available in Odoo AI.'))

    @api.model
    def _extraction_schema(self):
        line = {
            'type': 'object',
            'properties': {
                'description': {'type': 'string'},
                'product_name': {'type': 'string'},
                'product_code': {'type': 'string'},
                'vendor_product_code': {'type': 'string'},
                'barcode': {'type': 'string'},
                'quantity': {'type': 'number'},
                'uom': {'type': 'string'},
                'unit_price': {'type': 'number'},
                'tax_rate': {'type': 'number'},
            },
            'required': [
                'description', 'product_name', 'product_code', 'vendor_product_code',
                'barcode', 'quantity', 'uom', 'unit_price', 'tax_rate',
            ],
            'additionalProperties': False,
        }
        return {
            'type': 'object',
            'properties': {
                'is_purchase_document': {'type': 'boolean'},
                'vendor_name': {'type': 'string'},
                'vendor_vat': {'type': 'string'},
                'vendor_email': {'type': 'string'},
                'vendor_phone': {'type': 'string'},
                'vendor_reference': {'type': 'string'},
                'document_date': {'type': 'string'},
                'currency_code': {'type': 'string'},
                'payment_terms': {'type': 'string'},
                'expected_date': {'type': 'string'},
                'notes': {'type': 'string'},
                'lines': {'type': 'array', 'items': line},
            },
            'required': [
                'is_purchase_document', 'vendor_name', 'vendor_vat', 'vendor_email',
                'vendor_phone', 'vendor_reference', 'document_date', 'currency_code',
                'payment_terms', 'expected_date', 'notes', 'lines',
            ],
            'additionalProperties': False,
        }

    def _extract_with_odoo_gemini(self):
        self.ensure_one()
        llm_api = LLMApiService(self.env, 'google')
        try:
            llm_api._get_api_token()
        except Exception as exc:
            raise UserError(_(
                'Gemini API key is not configured in Odoo.\n\n'
                'Go to Settings → AI → Providers → Use your own Google Gemini account, paste the key, and save.'
            )) from exc

        pdf_b64 = self.file_data.decode('ascii') if isinstance(self.file_data, bytes) else self.file_data
        system_prompt = '''
You are a purchase document extraction engine inside Odoo.
Read the complete uploaded PDF and return only data supported by the document.
Never invent values. Use an empty string when a text value is absent, 0 for an absent number, and an empty array when no line exists.
Extract every purchasable line item separately.
vendor_name must be the supplier/seller only. If the PDF has no supplier, leave vendor fields empty; never use the buyer, requester, internal department, ship-to company, or document owner as the vendor.
For product_code use the supplier-visible or internal product/SKU code when clearly present.
For vendor_product_code use the supplier item code when clearly present.
Dates must be YYYY-MM-DD when known, otherwise empty.
Currency code must be ISO-like (BDT, USD, EUR, etc.) when identifiable.
Quantity and unit_price must be numeric and must not include thousands separators or currency symbols.
A quotation, supplier offer, purchase requirement, purchase requisition, pro-forma or purchase-order-like document counts as a purchase document.
'''.strip()
        user_prompt = '''
Extract the vendor, reference, dates, currency, payment terms, expected delivery date, notes and every line item from this PDF so Odoo can prepare a draft RFQ for human review.
'''.strip()
        try:
            response, *_ = llm_api._request_llm(
                llm_model=self._get_google_model(),
                system_prompts=[system_prompt],
                user_prompts=[user_prompt],
                files=[{'mimetype': 'application/pdf', 'value': pdf_b64}],
                schema=self._extraction_schema(),
                temperature=0.0,
            )
        except Exception as exc:
            _logger.exception('Odoo Gemini PDF extraction failed')
            raise UserError(_(
                'Odoo Gemini could not process this PDF.\n\nTechnical message: %(message)s',
                message=str(exc),
            )) from exc
        if not response:
            raise UserError(_('Odoo Gemini returned no extraction result.'))
        try:
            data = json.loads(response[0], strict=False)
        except (TypeError, json.JSONDecodeError) as exc:
            raise UserError(_('Odoo Gemini returned an invalid structured response.')) from exc
        if not data.get('is_purchase_document'):
            raise UserError(_('The uploaded PDF was not recognized as a purchase-related document.'))
        if not data.get('lines'):
            raise UserError(_('No purchase line items were found in the PDF.'))
        return data

    def _best_fuzzy(self, records, target, attr='name', threshold=0.88):
        if not target:
            return records.browse()
        target_norm = ' '.join(target.lower().split())
        scores = []
        for record in records:
            value = getattr(record, attr, False) or ''
            value_norm = ' '.join(value.lower().split())
            score = difflib.SequenceMatcher(None, target_norm, value_norm).ratio()
            if score >= threshold:
                scores.append((score, record))
        scores.sort(key=lambda item: item[0], reverse=True)
        if not scores:
            return records.browse()
        if len(scores) > 1 and (scores[0][0] - scores[1][0]) < 0.04:
            return records.browse()
        return scores[0][1]

    def _find_or_create_vendor(self, data):
        Partner = self.env['res.partner'].sudo().with_context(active_test=True)
        vat = (data.get('vendor_vat') or '').strip()
        email = (data.get('vendor_email') or '').strip().lower()
        name = (data.get('vendor_name') or '').strip()

        partner = Partner.browse()
        if vat:
            partner = Partner.search([('vat', '=ilike', vat)], limit=1)
        if not partner and email:
            partner = Partner.search([('email', '=ilike', email)], limit=1)
        if not partner and name:
            partner = Partner.search([('name', '=ilike', name)], limit=1)
        if not partner and name:
            candidates = Partner.search([
                ('is_company', '=', True),
                ('name', 'ilike', name.split()[0]),
            ], limit=30)
            partner = self._best_fuzzy(candidates, name, threshold=0.91)
        if partner:
            if partner.supplier_rank <= 0:
                partner.supplier_rank = 1
            return partner

        if name:
            # Creating the vendor keeps the requested one-click flow. We do not set
            # VAT automatically because localisation VAT constraints can reject an
            # AI-extracted value; the human reviewer can verify/add it safely.
            return Partner.create({
                'name': name,
                'email': email or False,
                'phone': (data.get('vendor_phone') or '').strip() or False,
                'supplier_rank': 1,
                'comment': _('Created automatically from an uploaded purchase PDF by Odoo AI. Please review the vendor master data.'),
            })

        placeholder = Partner.search([('ref', '=', PLACEHOLDER_VENDOR_REF)], limit=1)
        if not placeholder:
            placeholder = Partner.create({
                'name': _('AI Vendor Match Required'),
                'ref': PLACEHOLDER_VENDOR_REF,
                'supplier_rank': 1,
                'comment': _('Technical placeholder used when Odoo AI cannot identify a vendor. Replace it before approval.'),
            })
        return placeholder

    def _placeholder_product(self):
        Product = self.env['product.product'].sudo()
        product = Product.search([('default_code', '=', PLACEHOLDER_PRODUCT_CODE)], limit=1)
        if product:
            return product
        tmpl = self.env['product.template'].sudo().create({
            'name': _('AI Product Match Required'),
            'default_code': PLACEHOLDER_PRODUCT_CODE,
            'purchase_ok': True,
            'sale_ok': False,
            'type': 'service',
            'description_purchase': _('Replace this placeholder with the correct Odoo product before approving the RFQ.'),
        })
        return tmpl.product_variant_id

    def _find_product(self, line, partner):
        Product = self.env['product.product'].sudo().with_context(active_test=True)
        company = self.env.company
        base_domain = [
            ('purchase_ok', '=', True),
            '|', ('company_id', '=', False), ('company_id', '=', company.id),
        ]
        codes = [
            (line.get('product_code') or '').strip(),
            (line.get('vendor_product_code') or '').strip(),
        ]
        barcode = (line.get('barcode') or '').strip()
        name = (line.get('product_name') or line.get('description') or '').strip()

        for code in [c for c in codes if c]:
            product = Product.search(base_domain + [('default_code', '=ilike', code)], limit=1)
            if product:
                return product
        if barcode:
            product = Product.search(base_domain + [('barcode', '=', barcode)], limit=1)
            if product:
                return product

        # Match vendor-specific codes/names from standard Odoo supplierinfo.
        for code in [c for c in codes if c]:
            seller = self.env['product.supplierinfo'].sudo().search([
                ('partner_id', 'child_of', partner.commercial_partner_id.id),
                ('product_code', '=ilike', code),
            ], limit=1)
            if seller:
                if seller.product_id:
                    return seller.product_id
                if seller.product_tmpl_id:
                    return seller.product_tmpl_id.product_variant_id

        if name:
            product = Product.search(base_domain + [('name', '=ilike', name)], limit=1)
            if product:
                return product
            token = name.split()[0] if name.split() else name
            candidates = Product.search(base_domain + [('name', 'ilike', token)], limit=50)
            product = self._best_fuzzy(candidates, name, threshold=0.86)
            if product:
                return product
        return Product.browse()

    def _find_currency(self, code):
        if not code:
            return self.env.company.currency_id
        currency = self.env['res.currency'].sudo().search([
            ('name', '=', code.strip().upper()), ('active', '=', True)
        ], limit=1)
        return currency or self.env.company.currency_id

    def _find_payment_term(self, text):
        if not text:
            return self.env['account.payment.term'].browse()
        Term = self.env['account.payment.term'].sudo()
        term = Term.search([('name', '=ilike', text.strip())], limit=1)
        if not term:
            term = Term.search([('name', 'ilike', text.strip())], limit=1)
        return term

    def _parse_date(self, value):
        if not value:
            return False
        try:
            return fields.Date.to_date(value)
        except Exception:
            return False

    def _create_rfq(self, data):
        partner = self._find_or_create_vendor(data)
        currency = self._find_currency(data.get('currency_code'))
        payment_term = self._find_payment_term(data.get('payment_terms'))

        po_vals = {
            'partner_id': partner.id,
            'partner_ref': (data.get('vendor_reference') or '').strip() or False,
            'currency_id': currency.id,
            'payment_term_id': payment_term.id or False,
            'ai_pdf_generated': True,
            'ai_source_filename': self.file_name or _('Uploaded Purchase PDF'),
            'ai_approver_id': self.approver_id.id,
            'ai_approval_state': 'pending',
            'ai_extraction_data': json.dumps(data, ensure_ascii=False, indent=2),
        }
        po = self.env['purchase.order'].create(po_vals)

        expected_date = self._parse_date(data.get('expected_date'))
        if expected_date:
            planned_dt = datetime.combine(expected_date, time(hour=12))
        else:
            planned_dt = fields.Datetime.now()

        placeholder = self._placeholder_product()
        for extracted in data.get('lines', []):
            product = self._find_product(extracted, partner) or placeholder
            description = (
                extracted.get('description')
                or extracted.get('product_name')
                or extracted.get('product_code')
                or _('Purchase item from source PDF')
            ).strip()
            qty = float(extracted.get('quantity') or 0.0)
            if qty <= 0:
                qty = 1.0
            price = float(extracted.get('unit_price') or 0.0)
            uom = getattr(product, 'uom_po_id', False) or product.uom_id

            vals = {
                'order_id': po.id,
                'product_id': product.id,
                'name': description,
                'product_qty': qty,
                'product_uom_id': uom.id,
                'price_unit': price,
                'date_planned': planned_dt,
                'ai_source_description': description,
            }
            line = self.env['purchase.order.line'].create(vals)
            # Keep the PDF values after standard purchase-product computations.
            line.with_context(skip_uom_conversion=True).write({
                'name': description,
                'product_qty': qty,
                'price_unit': price,
                'date_planned': planned_dt,
            })

        attachment = self.env['ir.attachment'].create({
            'name': self.file_name or 'purchase_source.pdf',
            'type': 'binary',
            'datas': self.file_data,
            'mimetype': 'application/pdf',
            'res_model': 'purchase.order',
            'res_id': po.id,
        })
        po.ai_source_attachment_id = attachment.id

        source_date = self._parse_date(data.get('document_date'))
        notes = []
        if source_date:
            notes.append(_('Source document date: %s') % source_date)
        if data.get('payment_terms') and not payment_term:
            notes.append(_('PDF payment terms (not matched automatically): %s') % data['payment_terms'])
        if data.get('notes'):
            notes.append(_('AI extracted notes: %s') % data['notes'])
        if po.ai_unmatched_line_count:
            notes.append(_('%s product line(s) use the “AI Product Match Required” placeholder. Replace them before approval.') % po.ai_unmatched_line_count)
        if po.ai_vendor_needs_review:
            notes.append(_('AI could not identify a vendor. Replace the placeholder vendor before approval.'))
        po.message_post(
            body=_('Draft RFQ created from <b>%s</b> using Odoo native Gemini AI.<br/>%s') % (
                po.ai_source_filename,
                '<br/>'.join(notes) if notes else _('Please review vendor, products, quantities, prices and taxes before approval.'),
            ),
            attachment_ids=[attachment.id],
        )
        po._create_ai_approval_activity()
        return po

    def action_create_draft_rfq(self):
        self.ensure_one()
        self._validate_pdf()
        data = self._extract_with_odoo_gemini()
        po = self._create_rfq(data)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Draft RFQ'),
            'res_model': 'purchase.order',
            'res_id': po.id,
            'view_mode': 'form',
            'view_id': self.env.ref('purchase.purchase_order_form').id,
            'target': 'current',
        }
