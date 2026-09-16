import base64
import json
import io
import re
from datetime import datetime, time
from difflib import SequenceMatcher

import requests

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class AiPurchaseDocument(models.Model):
    _name = 'ai.purchase.document'
    _description = 'AI Purchase Document'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(
        string='Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New'),
        tracking=True,
    )
    file_data = fields.Binary(
        string='Supplier PDF',
        required=True,
        attachment=True,
    )
    file_name = fields.Char(string='Filename', required=True)
    processing_mode = fields.Selection(
        [
            ('local_test', 'No API Test Mode'),
            ('openai', 'OpenAI API'),
        ],
        string='Processing Mode',
        default=lambda self: self.env['ir.config_parameter'].sudo().get_param(
            'ai_purchase_pdf.processing_mode', 'local_test'
        ),
        tracking=True,
        help='No API Test Mode uses only embedded PDF text and local rules. It does not call an external AI service.',
    )
    state = fields.Selection(
        [
            ('upload', 'Uploaded'),
            ('processing', 'Processing PDF'),
            ('review', 'Needs Review'),
            ('draft_created', 'Draft RFQ Created'),
            ('approval_requested', 'Approval Requested'),
            ('approved', 'Approved - Odoo Approval Pending'),
            ('done', 'Confirmed Purchase Order'),
            ('rejected', 'Rejected'),
            ('error', 'Error'),
        ],
        default='upload',
        tracking=True,
        required=True,
        copy=False,
    )

    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    approver_id = fields.Many2one(
        'res.users',
        string='Approver',
        tracking=True,
        domain="[('share', '=', False)]",
        help='This user receives the request to review and confirm the AI-created draft RFQ.',
    )
    approval_requested_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    approval_requested_date = fields.Datetime(readonly=True, copy=False)
    approved_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    approved_date = fields.Datetime(readonly=True, copy=False)
    rejected_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    rejected_date = fields.Datetime(readonly=True, copy=False)
    rejection_reason = fields.Text(copy=False)

    partner_id = fields.Many2one(
        'res.partner',
        string='Matched Vendor',
        tracking=True,
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
    )
    currency_id = fields.Many2one('res.currency', string='Currency')
    payment_term_id = fields.Many2one('account.payment.term', string='Payment Terms')
    purchase_order_id = fields.Many2one(
        'purchase.order',
        string='Draft RFQ / Purchase Order',
        readonly=True,
        copy=False,
        tracking=True,
    )

    extracted_document_type = fields.Char(readonly=True)
    extracted_vendor_name = fields.Char(readonly=True)
    extracted_vendor_tax_id = fields.Char(readonly=True)
    extracted_vendor_email = fields.Char(readonly=True)
    vendor_reference = fields.Char(string='Vendor Reference', readonly=True)
    document_date = fields.Date(readonly=True)
    extracted_currency_code = fields.Char(readonly=True)
    extracted_payment_terms = fields.Char(readonly=True)
    delivery_date = fields.Date(readonly=True)
    extracted_notes = fields.Text(readonly=True)
    overall_confidence = fields.Float(readonly=True, digits=(5, 4))
    ai_raw_json = fields.Text(string='Extraction Raw JSON', readonly=True, groups='base.group_system')
    local_extracted_text = fields.Text(string='Local Extracted PDF Text', readonly=True, groups='base.group_system')
    error_message = fields.Text(readonly=True, copy=False)
    matching_summary = fields.Text(readonly=True, copy=False)

    line_ids = fields.One2many(
        'ai.purchase.document.line',
        'document_id',
        string='Extracted Lines',
        copy=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        param = self.env['ir.config_parameter'].sudo()
        approver_id = int(param.get_param('ai_purchase_pdf.default_approver_id') or 0)
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('ai.purchase.document') or _('New')
            if not vals.get('approver_id') and approver_id:
                vals['approver_id'] = approver_id
        return super().create(vals_list)

    def unlink(self):
        for rec in self:
            if rec.purchase_order_id and rec.purchase_order_id.state not in ('cancel',):
                raise UserError(_('You cannot delete an AI purchase document linked to an active RFQ/PO.'))
        return super().unlink()

    @api.constrains('file_name')
    def _check_pdf_filename(self):
        for rec in self:
            if rec.file_name and not rec.file_name.lower().endswith('.pdf'):
                raise ValidationError(_('Only PDF files are supported.'))

    def _notify(self, title, message, notif_type='success', sticky=False):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': message,
                'type': notif_type,
                'sticky': sticky,
            },
        }

    def _get_param_int(self, key, default):
        value = self.env['ir.config_parameter'].sudo().get_param(key)
        try:
            return int(value) if value not in (None, False, '') else default
        except (TypeError, ValueError):
            return default

    def _get_param_float(self, key, default):
        value = self.env['ir.config_parameter'].sudo().get_param(key)
        try:
            return float(value) if value not in (None, False, '') else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_text(value):
        value = (value or '').lower().strip()
        value = re.sub(r'[^\w\s.-]+', ' ', value, flags=re.UNICODE)
        value = re.sub(r'\s+', ' ', value)
        return value

    @classmethod
    def _similarity(cls, left, right):
        left = cls._normalize_text(left)
        right = cls._normalize_text(right)
        if not left or not right:
            return 0.0
        return SequenceMatcher(None, left, right).ratio()

    @staticmethod
    def _safe_date(value):
        if not value:
            return False
        try:
            return fields.Date.to_date(value)
        except Exception:
            return False

    def _validate_pdf(self):
        self.ensure_one()
        if not self.file_data:
            raise UserError(_('Please upload a PDF first.'))
        file_b64 = self.file_data.decode() if isinstance(self.file_data, bytes) else self.file_data
        try:
            raw = base64.b64decode(file_b64)
        except Exception as exc:
            raise UserError(_('The uploaded file cannot be decoded.')) from exc
        if not raw.lstrip().startswith(b'%PDF'):
            raise UserError(_('The uploaded file is not a valid PDF.'))
        max_mb = max(self._get_param_int('ai_purchase_pdf.max_file_mb', 20), 1)
        if len(raw) > max_mb * 1024 * 1024:
            raise UserError(_('The PDF is larger than the configured maximum of %s MB.') % max_mb)
        return file_b64

    def _extraction_schema(self):
        nullable_string = {'type': ['string', 'null']}
        nullable_number = {'type': ['number', 'null']}
        return {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'is_purchase_document': {'type': 'boolean'},
                'document_type': nullable_string,
                'vendor_name': nullable_string,
                'vendor_tax_id': nullable_string,
                'vendor_email': nullable_string,
                'vendor_reference': nullable_string,
                'document_date': nullable_string,
                'currency_code': nullable_string,
                'payment_terms': nullable_string,
                'delivery_date': nullable_string,
                'notes': nullable_string,
                'overall_confidence': {'type': 'number'},
                'warning': nullable_string,
                'lines': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'additionalProperties': False,
                        'properties': {
                            'description': nullable_string,
                            'vendor_sku': nullable_string,
                            'quantity': nullable_number,
                            'uom': nullable_string,
                            'unit_price': nullable_number,
                            'tax_rate': nullable_number,
                            'confidence': {'type': 'number'},
                        },
                        'required': [
                            'description', 'vendor_sku', 'quantity', 'uom',
                            'unit_price', 'tax_rate', 'confidence',
                        ],
                    },
                },
            },
            'required': [
                'is_purchase_document', 'document_type', 'vendor_name', 'vendor_tax_id',
                'vendor_email', 'vendor_reference', 'document_date', 'currency_code',
                'payment_terms', 'delivery_date', 'notes', 'overall_confidence',
                'warning', 'lines',
            ],
        }

    def _extract_pdf_text_local(self, file_b64):
        """Extract embedded text from a PDF without any external API call.

        Odoo 19 ships with a PDF library in its supported Python dependencies. This
        intentionally does not perform OCR; image-only/scanned PDFs must be tested
        with an AI/OCR provider instead.
        """
        self.ensure_one()
        try:
            raw = base64.b64decode(file_b64)
        except Exception as exc:
            raise UserError(_('The uploaded PDF could not be decoded for local processing.')) from exc

        try:
            try:
                from pypdf import PdfReader
            except ImportError:
                from PyPDF2 import PdfReader

            reader = PdfReader(io.BytesIO(raw), strict=False)
            if getattr(reader, 'is_encrypted', False):
                try:
                    reader.decrypt('')
                except Exception as exc:
                    raise UserError(_('The PDF is password-protected. Remove the password before using No API Test Mode.')) from exc

            chunks = []
            total_chars = 0
            for page in reader.pages:
                try:
                    page_text = page.extract_text() or ''
                except Exception:
                    page_text = ''
                if page_text.strip():
                    chunks.append(page_text)
                    total_chars += len(page_text)
                if total_chars >= 200000:
                    break
            text = '\n'.join(chunks)[:200000]
        except UserError:
            raise
        except Exception as exc:
            raise UserError(_('Odoo could not read text from this PDF in No API Test Mode: %s') % str(exc)) from exc

        if len(re.sub(r'\s+', '', text)) < 10:
            raise UserError(_(
                'No readable embedded text was found in this PDF. No API Test Mode does not perform OCR on scanned/image-only PDFs. '
                'Please use a text-based PDF for testing, or switch Processing Mode to OpenAI API later.'
            ))
        return text

    @staticmethod
    def _local_clean_value(value):
        value = (value or '').strip().strip('|').strip()
        return re.sub(r'\s+', ' ', value)

    @staticmethod
    def _local_number(value):
        if value in (None, False, ''):
            return None
        value = str(value).strip()
        value = re.sub(r'(?i)\b(?:bdt|usd|eur|gbp|inr|tk|taka)\b', '', value)
        value = value.replace('৳', '').replace('$', '').replace('€', '').replace('£', '')
        value = value.replace(',', '').strip()
        match = re.search(r'-?\d+(?:\.\d+)?', value)
        if not match:
            return None
        try:
            return float(match.group(0))
        except ValueError:
            return None

    @staticmethod
    def _local_date(value):
        if not value:
            return None
        value = str(value).strip()
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%d.%m.%Y', '%m/%d/%Y', '%d/%m/%y', '%d-%m-%y'):
            try:
                return datetime.strptime(value, fmt).date().isoformat()
            except ValueError:
                continue
        iso = re.search(r'\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b', value)
        if iso:
            try:
                return datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))).date().isoformat()
            except ValueError:
                return None
        dmy = re.search(r'\b(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})\b', value)
        if dmy:
            try:
                return datetime(int(dmy.group(3)), int(dmy.group(2)), int(dmy.group(1))).date().isoformat()
            except ValueError:
                return None
        return None

    @classmethod
    def _local_find_labeled_value(cls, lines, labels):
        label_expr = '|'.join(re.escape(label) for label in labels)
        pattern = re.compile(r'^\s*(?:%s)\s*(?:[:#\-]|\bis\b)\s*(.+?)\s*$' % label_expr, re.IGNORECASE)
        for line in lines:
            match = pattern.match(line)
            if match:
                return cls._local_clean_value(match.group(1))
        return None

    def _local_parse_labeled_items(self, lines):
        """Parse repeated Product/Description + Quantity + Unit Price blocks."""
        items = []
        current = None

        def flush():
            nonlocal current
            if current and current.get('description'):
                # Only promote a block when at least quantity or price was found.
                if current.get('quantity') is not None or current.get('unit_price') is not None:
                    current.setdefault('quantity', 0.0)
                    current.setdefault('unit_price', None)
                    current.setdefault('vendor_sku', None)
                    current.setdefault('uom', None)
                    current.setdefault('tax_rate', None)
                    current.setdefault('confidence', 0.76)
                    items.append(current)
            current = None

        product_re = re.compile(r'^\s*(?:product|item|description|product description|item description)\s*[:\-]\s*(.+?)\s*$', re.IGNORECASE)
        qty_re = re.compile(r'^\s*(?:qty|quantity|units?)\s*[:\-]\s*(.+?)\s*$', re.IGNORECASE)
        price_re = re.compile(r'^\s*(?:unit price|price|rate|unit rate)\s*[:\-]\s*(.+?)\s*$', re.IGNORECASE)
        sku_re = re.compile(r'^\s*(?:sku|item code|product code|code)\s*[:\-]\s*(.+?)\s*$', re.IGNORECASE)
        uom_re = re.compile(r'^\s*(?:uom|unit of measure|unit)\s*[:\-]\s*(.+?)\s*$', re.IGNORECASE)
        tax_re = re.compile(r'^\s*(?:tax|vat|tax rate|vat rate)\s*[:\-]\s*(.+?)\s*$', re.IGNORECASE)

        for line in lines:
            m = product_re.match(line)
            if m:
                flush()
                current = {'description': self._local_clean_value(m.group(1))}
                continue
            if not current:
                continue
            m = qty_re.match(line)
            if m:
                current['quantity'] = self._local_number(m.group(1)) or 0.0
                continue
            m = price_re.match(line)
            if m:
                current['unit_price'] = self._local_number(m.group(1))
                continue
            m = sku_re.match(line)
            if m:
                current['vendor_sku'] = self._local_clean_value(m.group(1))
                continue
            m = uom_re.match(line)
            if m:
                current['uom'] = self._local_clean_value(m.group(1))
                continue
            m = tax_re.match(line)
            if m:
                current['tax_rate'] = self._local_number(m.group(1))
        flush()
        return items

    def _local_parse_table_items(self, lines):
        """Best-effort parser for simple text tables such as Description / Qty / Unit Price."""
        items = []
        header_index = None
        for index, line in enumerate(lines):
            low = self._normalize_text(line)
            if ('qty' in low or 'quantity' in low) and ('price' in low or 'rate' in low):
                header_index = index
                break
        if header_index is None:
            return items

        skip_words = ('subtotal', 'sub total', 'grand total', 'total', 'vat', 'tax', 'discount', 'shipping', 'freight', 'amount due')
        row_re = re.compile(
            r'^\s*(?P<desc>.+?)\s{1,}(?P<qty>\d+(?:\.\d+)?)\s+(?:(?P<uom>pcs?|pieces?|units?|ea|each|nos?|kg|kgs|g|ltr|litre|liter|box|boxes|set|sets)\s+)?(?P<price>(?:(?:BDT|USD|EUR|GBP|INR|TK)\s*)?[৳$€£]?[\d,]+(?:\.\d+)?)\s*$',
            re.IGNORECASE,
        )
        for line in lines[header_index + 1:]:
            clean = self._local_clean_value(line)
            if not clean:
                continue
            low = clean.lower()
            if any(low.startswith(word) for word in skip_words):
                if items:
                    break
                continue
            match = row_re.match(clean)
            if not match:
                continue
            desc = self._local_clean_value(match.group('desc'))
            # Reject obvious headers/labels accidentally matched as products.
            if len(desc) < 2 or any(key in desc.lower() for key in ('quantity', 'unit price', 'description')):
                continue
            qty = self._local_number(match.group('qty'))
            price = self._local_number(match.group('price'))
            if qty is None or price is None:
                continue
            items.append({
                'description': desc,
                'vendor_sku': None,
                'quantity': qty,
                'uom': match.group('uom') or None,
                'unit_price': price,
                'tax_rate': None,
                'confidence': 0.72,
            })
        return items

    def _parse_pdf_locally(self, file_b64):
        """Return AI-schema-compatible data using no API and no network connection."""
        self.ensure_one()
        text = self._extract_pdf_text_local(file_b64)
        self.local_extracted_text = text
        raw_lines = [line.strip() for line in text.replace('\r', '\n').split('\n')]
        lines = [line for line in raw_lines if line]

        vendor_name = self._local_find_labeled_value(lines, ['supplier', 'vendor', 'supplier name', 'vendor name', 'company'])
        vendor_tax_id = self._local_find_labeled_value(lines, ['vat', 'vat no', 'vat number', 'tax id', 'tin', 'bin'])
        vendor_email = self._local_find_labeled_value(lines, ['email', 'supplier email', 'vendor email'])
        vendor_reference = self._local_find_labeled_value(lines, ['quotation', 'quotation no', 'quotation number', 'quote no', 'quote number', 'reference', 'ref', 'rfq'])
        document_date_raw = self._local_find_labeled_value(lines, ['date', 'quotation date', 'quote date', 'document date'])
        payment_terms = self._local_find_labeled_value(lines, ['payment terms', 'payment term', 'terms of payment'])
        delivery_raw = self._local_find_labeled_value(lines, ['delivery date', 'expected delivery', 'delivery'])

        currency_code = None
        currency_patterns = [
            ('BDT', r'\bBDT\b|৳|\bTAKA\b'),
            ('USD', r'\bUSD\b|US\$'),
            ('EUR', r'\bEUR\b|€'),
            ('GBP', r'\bGBP\b|£'),
            ('INR', r'\bINR\b|₹'),
        ]
        for code, pattern in currency_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                currency_code = code
                break

        items = self._local_parse_labeled_items(lines)
        if not items:
            items = self._local_parse_table_items(lines)

        document_type = 'Supplier Purchase Document (Local Test)'
        for line in lines[:30]:
            low = line.lower()
            if 'quotation' in low or re.search(r'\bquote\b', low):
                document_type = 'Supplier Quotation'
                break
            if 'proforma' in low or 'pro-forma' in low:
                document_type = 'Pro-forma Invoice'
                break
            if 'purchase order' in low:
                document_type = 'Purchase Order Details'
                break

        # This mode is intentionally conservative. Missing fields are left empty for human review.
        confidence = 0.78 if vendor_name and items else (0.62 if items else 0.35)
        notes = _(
            'Processed in No API Test Mode. Values were extracted from embedded PDF text with local rules only. '
            'Review all fields before creating the RFQ. Scanned/image-only PDFs are not supported in this mode.'
        )
        return {
            'is_purchase_document': True,
            'document_type': document_type,
            'vendor_name': vendor_name,
            'vendor_tax_id': vendor_tax_id,
            'vendor_email': vendor_email,
            'vendor_reference': vendor_reference,
            'document_date': self._local_date(document_date_raw),
            'currency_code': currency_code,
            'payment_terms': payment_terms,
            'delivery_date': self._local_date(delivery_raw),
            'notes': notes,
            'overall_confidence': confidence,
            'warning': None if items else _('No line items were recognized automatically. Add/verify purchase lines manually in Needs Review.'),
            'lines': items,
        }

    def _call_openai(self, file_b64):
        self.ensure_one()
        param = self.env['ir.config_parameter'].sudo()
        api_key = param.get_param('ai_purchase_pdf.openai_api_key')
        if not api_key:
            raise UserError(_(
                'OpenAI API key is not configured. Go to Purchase > Configuration > Settings > AI Purchase PDF.'
            ))

        endpoint = param.get_param('ai_purchase_pdf.openai_endpoint') or 'https://api.openai.com/v1/responses'
        model = param.get_param('ai_purchase_pdf.openai_model') or 'gpt-5.6-luna'
        timeout = max(self._get_param_int('ai_purchase_pdf.timeout', 120), 10)

        prompt = (
            'Read the attached supplier PDF and extract purchase/RFQ data. '
            'The document may be a supplier quotation, pro-forma invoice, purchase request, or purchase order detail. '
            'Return only values that are actually present or strongly supported by the document. Never invent a vendor, '
            'product, quantity, price, tax, currency, date, or payment term. Use ISO currency codes such as USD, EUR, BDT. '
            'Dates must use YYYY-MM-DD when identifiable. Quantities and unit prices must be numeric. '
            'For each item, keep the supplier description exactly enough to identify it. '
            'Set is_purchase_document=false if the PDF is not purchase-related. '
            'Confidence values must be between 0 and 1.'
        )

        payload = {
            'model': model,
            'store': False,
            'input': [
                {
                    'role': 'user',
                    'content': [
                        {
                            'type': 'input_file',
                            'filename': self.file_name or 'supplier.pdf',
                            'file_data': 'data:application/pdf;base64,%s' % file_b64,
                            'detail': 'high',
                        },
                        {'type': 'input_text', 'text': prompt},
                    ],
                }
            ],
            'text': {
                'format': {
                    'type': 'json_schema',
                    'name': 'purchase_document_extraction',
                    'strict': True,
                    'schema': self._extraction_schema(),
                }
            },
        }

        try:
            response = requests.post(
                endpoint,
                headers={
                    'Authorization': 'Bearer %s' % api_key,
                    'Content-Type': 'application/json',
                },
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise UserError(_('Unable to reach the AI provider: %s') % str(exc)) from exc

        if not response.ok:
            try:
                error_data = response.json()
                detail = error_data.get('error', {}).get('message') or json.dumps(error_data)[:1000]
            except Exception:
                detail = response.text[:1000]
            raise UserError(_('AI provider returned HTTP %s: %s') % (response.status_code, detail))

        try:
            data = response.json()
        except Exception as exc:
            raise UserError(_('AI provider returned an invalid JSON response.')) from exc

        if data.get('status') == 'incomplete':
            reason = (data.get('incomplete_details') or {}).get('reason') or _('unknown reason')
            raise UserError(_('AI processing returned an incomplete response: %s') % reason)

        text_chunks = []
        refusals = []
        if isinstance(data.get('output_text'), str):
            text_chunks.append(data['output_text'])
        for item in data.get('output', []) or []:
            if not isinstance(item, dict):
                continue
            for content in item.get('content', []) or []:
                if not isinstance(content, dict):
                    continue
                if content.get('type') == 'output_text' and content.get('text'):
                    text_chunks.append(content['text'])
                elif content.get('type') == 'refusal' and content.get('refusal'):
                    refusals.append(content['refusal'])

        if not text_chunks:
            if refusals:
                raise UserError(_('AI provider refused to process this PDF: %s') % ' '.join(refusals)[:1000])
            raise UserError(_('AI provider returned no structured output.'))

        raw_text = '\n'.join(text_chunks).strip()
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError as exc:
            # Defensive fallback for providers that wrap the JSON in markdown fences.
            cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw_text, flags=re.IGNORECASE | re.DOTALL).strip()
            try:
                return json.loads(cleaned)
            except Exception:
                raise UserError(_('AI output could not be parsed as JSON.')) from exc

    def _apply_extraction(self, data):
        self.ensure_one()
        if not data.get('is_purchase_document'):
            warning = data.get('warning') or _('The uploaded PDF does not appear to contain purchase/RFQ details.')
            raise UserError(warning)

        line_commands = [Command.clear()]
        for item in data.get('lines') or []:
            line_commands.append(Command.create({
                'description': item.get('description') or _('Unspecified item - human review required'),
                'vendor_sku': item.get('vendor_sku') or '',
                'quantity': float(item.get('quantity') or 0.0),
                'extracted_uom': item.get('uom') or '',
                'price_unit': float(item.get('unit_price') or 0.0),
                'price_extracted': item.get('unit_price') is not None,
                'tax_rate': float(item.get('tax_rate') or 0.0),
                'confidence': float(item.get('confidence') or 0.0),
            }))

        currency_code = (data.get('currency_code') or '').upper().strip()
        currency = self.env['res.currency'].search([
            ('name', '=', currency_code)
        ], limit=1) if currency_code else self.env['res.currency']

        payment_term = self.env['account.payment.term']
        payment_terms = (data.get('payment_terms') or '').strip()
        if payment_terms:
            payment_term = self.env['account.payment.term'].search([
                '|', ('company_id', '=', False), ('company_id', '=', self.company_id.id),
                ('name', 'ilike', payment_terms),
            ], limit=1)

        self.write({
            'extracted_document_type': data.get('document_type') or '',
            'extracted_vendor_name': data.get('vendor_name') or '',
            'extracted_vendor_tax_id': data.get('vendor_tax_id') or '',
            'extracted_vendor_email': data.get('vendor_email') or '',
            'vendor_reference': data.get('vendor_reference') or '',
            'document_date': self._safe_date(data.get('document_date')),
            'extracted_currency_code': currency_code,
            'currency_id': currency.id if currency else False,
            'extracted_payment_terms': payment_terms,
            'payment_term_id': payment_term.id if payment_term else False,
            'delivery_date': self._safe_date(data.get('delivery_date')),
            'extracted_notes': data.get('notes') or '',
            'overall_confidence': float(data.get('overall_confidence') or 0.0),
            'ai_raw_json': json.dumps(data, ensure_ascii=False, indent=2),
            'line_ids': line_commands,
            'error_message': False,
        })

    def _match_vendor(self):
        self.ensure_one()
        if self.partner_id:
            return self.partner_id, 1.0, _('Vendor selected manually or already matched.')

        Partner = self.env['res.partner'].with_company(self.company_id)
        base_domain = ['|', ('company_id', '=', False), ('company_id', '=', self.company_id.id)]

        vat = (self.extracted_vendor_tax_id or '').strip()
        if vat:
            partner = Partner.search(base_domain + [('vat', '=ilike', vat)], limit=1)
            if partner:
                return partner.commercial_partner_id, 1.0, _('Matched by vendor tax/VAT ID.')

        email = (self.extracted_vendor_email or '').strip()
        if email:
            partner = Partner.search(base_domain + [('email', '=ilike', email)], limit=1)
            if partner:
                return partner.commercial_partner_id, 1.0, _('Matched by vendor email.')

        name = (self.extracted_vendor_name or '').strip()
        if not name:
            return Partner, 0.0, _('AI did not extract a vendor name.')

        exact = Partner.search(base_domain + [('name', '=ilike', name)], limit=2)
        if len(exact) == 1:
            return exact.commercial_partner_id, 1.0, _('Matched by exact vendor name.')

        normalized = self._normalize_text(name)
        words = [w for w in normalized.split() if len(w) >= 3]
        token = max(words, key=len) if words else normalized
        candidates = Partner.search(base_domain + [('name', 'ilike', token)], limit=100)
        if not candidates:
            candidates = Partner.search(base_domain + [('supplier_rank', '>', 0)], limit=300)

        scored = sorted(
            ((self._similarity(name, p.name), p) for p in candidates),
            key=lambda x: x[0], reverse=True,
        )
        if not scored:
            return Partner, 0.0, _('No existing vendor candidate was found.')

        best_score, best = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        threshold = min(max(self._get_param_float('ai_purchase_pdf.match_threshold', 0.90), 0.0), 1.0)
        if best_score >= threshold and (best_score >= 0.98 or best_score - second_score >= 0.04):
            return best.commercial_partner_id, best_score, _('Matched by vendor name similarity %.0f%%.') % (best_score * 100)
        return Partner, best_score, _(
            'Vendor match is uncertain. Best candidate: %s (%.0f%%). Please select the vendor manually.'
        ) % (best.display_name, best_score * 100)

    def _product_from_supplierinfo(self, supplierinfo):
        self.ensure_one()
        if not supplierinfo:
            return self.env['product.product']
        if supplierinfo.product_id:
            return supplierinfo.product_id
        variants = supplierinfo.product_tmpl_id.product_variant_ids
        return variants if len(variants) == 1 else self.env['product.product']

    def _match_product(self, line):
        self.ensure_one()
        if line.product_id:
            return line.product_id, 1.0, _('Product selected manually or already matched.')

        Product = self.env['product.product'].with_company(self.company_id)
        product_domain = [
            ('purchase_ok', '=', True),
            '|', ('company_id', '=', False), ('company_id', '=', self.company_id.id),
        ]
        vendor = self.partner_id.commercial_partner_id if self.partner_id else self.env['res.partner']
        vendor_ids = vendor.ids

        sku = (line.vendor_sku or '').strip()
        if sku and vendor_ids:
            seller = self.env['product.supplierinfo'].search([
                ('partner_id', 'in', vendor_ids),
                ('product_code', '=ilike', sku),
                '|', ('company_id', '=', False), ('company_id', '=', self.company_id.id),
            ], limit=2)
            if len(seller) == 1:
                product = self._product_from_supplierinfo(seller)
                if product:
                    return product, 1.0, _('Matched by vendor product code.')

        if sku:
            exact_code = Product.search(product_domain + [
                '|', ('default_code', '=ilike', sku), ('barcode', '=ilike', sku)
            ], limit=2)
            if len(exact_code) == 1:
                return exact_code, 1.0, _('Matched by internal reference/barcode.')

        description = (line.description or '').strip()
        if description and vendor_ids:
            seller = self.env['product.supplierinfo'].search([
                ('partner_id', 'in', vendor_ids),
                ('product_name', '=ilike', description),
                '|', ('company_id', '=', False), ('company_id', '=', self.company_id.id),
            ], limit=2)
            if len(seller) == 1:
                product = self._product_from_supplierinfo(seller)
                if product:
                    return product, 1.0, _('Matched by vendor product name.')

        if description:
            exact_name = Product.search(product_domain + [('name', '=ilike', description)], limit=2)
            if len(exact_name) == 1:
                return exact_name, 1.0, _('Matched by exact product name.')

        search_text = ' '.join(filter(None, [sku, description])).strip()
        normalized = self._normalize_text(search_text)
        words = [w for w in normalized.split() if len(w) >= 3]
        token = max(words, key=len) if words else normalized

        candidates = Product
        if token:
            candidates = Product.search(product_domain + [
                '|', '|',
                ('name', 'ilike', token),
                ('default_code', 'ilike', token),
                ('barcode', 'ilike', token),
            ], limit=120)
        if not candidates and vendor_ids and token:
            seller_candidates = self.env['product.supplierinfo'].search([
                ('partner_id', 'in', vendor_ids),
                '|', ('product_name', 'ilike', token), ('product_code', 'ilike', token),
            ], limit=120)
            candidates = seller_candidates.mapped('product_id') | seller_candidates.mapped('product_tmpl_id.product_variant_ids')
        if not candidates:
            candidates = Product.search(product_domain, limit=300)

        scored = []
        for product in candidates:
            scores = [
                self._similarity(search_text, product.display_name),
                self._similarity(description, product.name),
                self._similarity(sku, product.default_code),
                self._similarity(sku, product.barcode),
            ]
            if vendor_ids:
                seller_names = product.seller_ids.filtered(lambda s: s.partner_id.id in vendor_ids)
                for seller in seller_names:
                    scores.append(self._similarity(description, seller.product_name))
                    scores.append(self._similarity(sku, seller.product_code))
            scored.append((max(scores or [0.0]), product))

        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            return Product, 0.0, _('No existing product candidate was found.')

        best_score, best = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        threshold = min(max(self._get_param_float('ai_purchase_pdf.match_threshold', 0.90), 0.0), 1.0)
        if best_score >= threshold and (best_score >= 0.98 or best_score - second_score >= 0.04):
            return best, best_score, _('Matched by product similarity %.0f%%.') % (best_score * 100)
        return Product, best_score, _(
            'Product match is uncertain. Best candidate: %s (%.0f%%). Please select manually.'
        ) % (best.display_name, best_score * 100)

    def _resolve_uom(self, line):
        """Return (uom, is_safe_match). A supplied PDF UoM must match an allowed product UoM.

        When the PDF does not state a UoM, the product default is safe. When the user
        manually selected a UoM in review, that selection is also considered reviewed.
        """
        if not line.product_id:
            return self.env['uom.uom'], False
        if line.uom_id and line.human_reviewed:
            return line.uom_id, True
        product = line.product_id
        allowed = product.uom_id | product.uom_ids
        extracted = (line.extracted_uom or '').strip()
        if not extracted:
            return product.uom_id, True
        matched = allowed.filtered(lambda u: self._normalize_text(u.name) == self._normalize_text(extracted))
        if matched:
            return matched[:1], True
        matched = allowed.filtered(lambda u: extracted.lower() in (u.name or '').lower())
        if matched:
            return matched[:1], True
        return self.env['uom.uom'], False

    def _auto_match(self):
        self.ensure_one()
        summary = []
        vendor, vendor_score, vendor_note = self._match_vendor()
        if vendor:
            self.partner_id = vendor.id
        summary.append(_('Vendor: %s') % vendor_note)

        matched_count = 0
        review_count = 0
        for line in self.line_ids:
            product, score, note = self._match_product(line)
            vals = {
                'match_score': score,
                'match_note': note,
            }
            safe = bool(product)
            if product:
                vals['product_id'] = product.id
                uom, uom_safe = self._resolve_uom(line)
                if uom:
                    vals['uom_id'] = uom.id
                if not uom_safe:
                    safe = False
                    vals['match_note'] = '%s %s' % (note, _('PDF UoM could not be safely matched; select the RFQ UoM manually.'))
                if not line.price_extracted and not line.human_reviewed:
                    safe = False
                    vals['match_note'] = '%s %s' % (vals['match_note'], _('Unit price was not found in the PDF; enter/verify it manually.'))
                if line.confidence < 0.60 and not line.human_reviewed:
                    safe = False
                    vals['match_note'] = '%s %s' % (vals['match_note'], _('AI extraction confidence is low; review this line manually.'))

            if safe:
                vals['match_status'] = 'matched'
                matched_count += 1
            else:
                vals['match_status'] = 'review'
                review_count += 1
            line.write(vals)

        if not self.partner_id:
            review_count += 1
        if self.extracted_currency_code:
            summary.append(_('Currency: %s') % (_('matched to %s') % self.currency_id.display_name if self.currency_id else _('not matched - select currency manually')))
        if self.extracted_payment_terms:
            summary.append(_('Payment terms: %s') % (_('matched to %s') % self.payment_term_id.display_name if self.payment_term_id else _('not matched - select payment terms manually')))
        self.matching_summary = '\n'.join(summary + [
            _('Product lines matched: %s') % matched_count,
            _('Items requiring review: %s') % review_count,
        ])
        return not review_count and bool(self.line_ids)

    def _all_ready_for_rfq(self):
        self.ensure_one()
        if not self.partner_id or not self.line_ids:
            return False
        if self.extracted_currency_code and not self.currency_id:
            return False
        if self.extracted_payment_terms and not self.payment_term_id:
            return False
        for line in self.line_ids:
            if (
                not line.product_id
                or not line.uom_id
                or line.quantity <= 0
                or line.match_status != 'matched'
                or (not line.price_extracted and not line.human_reviewed)
            ):
                return False
        return True

    def action_process_pdf(self):
        self.ensure_one()
        if self.purchase_order_id:
            raise UserError(_('This document is already linked to an RFQ/PO.'))
        self.state = 'processing'
        self.error_message = False
        try:
            file_b64 = self._validate_pdf()
            mode = self.processing_mode or self.env['ir.config_parameter'].sudo().get_param(
                'ai_purchase_pdf.processing_mode', 'local_test'
            )
            if mode == 'openai':
                data = self._call_openai(file_b64)
            else:
                data = self._parse_pdf_locally(file_b64)
            self._apply_extraction(data)
            ready = self._auto_match()
            if ready and self._all_ready_for_rfq():
                return self.action_create_rfq_and_request()
            self.state = 'review'
            mode_label = _('OpenAI extraction') if mode == 'openai' else _('No API local extraction')
            self.message_post(body=_('%s completed. Some fields require human review.') % mode_label)
            return self._notify(
                _('Review Required'),
                _('%s is complete. Review/correct the vendor, products, quantities, UoM and prices before creating the draft RFQ.') % mode_label,
                'warning',
                True,
            )
        except Exception as exc:
            message = str(exc)
            self.write({'state': 'error', 'error_message': message})
            self.message_post(body=_('PDF processing failed: %s') % message)
            return self._notify(_('PDF Processing Failed'), message, 'danger', True)

    def action_retry_matching(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_('There is no extracted data to match.'))
        ready = self._auto_match()
        self.state = 'review'
        if ready and self._all_ready_for_rfq():
            return self._notify(_('Matching Complete'), _('Vendor and all product lines are matched. You can create the draft RFQ.'), 'success')
        return self._notify(_('Review Required'), _('Some fields still need manual review.'), 'warning')

    def _prepare_po_values(self):
        self.ensure_one()
        if not self._all_ready_for_rfq():
            raise UserError(_('Select a vendor and product for every line before creating the RFQ.'))

        order_lines = []
        for line in self.line_ids:
            uom = line.uom_id or line.product_id.uom_id
            planned = fields.Datetime.now()
            if self.delivery_date:
                planned = datetime.combine(self.delivery_date, time(12, 0, 0))
            order_lines.append(Command.create({
                'product_id': line.product_id.id,
                'name': line.description or line.product_id.display_name,
                'product_qty': line.quantity,
                'product_uom_id': uom.id,
                'price_unit': line.price_unit,
                'date_planned': planned,
            }))

        vals = {
            'partner_id': self.partner_id.id,
            'partner_ref': self.vendor_reference or False,
            'origin': self.name,
            'company_id': self.company_id.id,
            'ai_purchase_document_id': self.id,
            'ai_approval_state': 'none',
            'order_line': order_lines,
        }
        if self.currency_id:
            vals['currency_id'] = self.currency_id.id
        if self.payment_term_id:
            vals['payment_term_id'] = self.payment_term_id.id
        if self.document_date:
            vals['date_order'] = datetime.combine(self.document_date, time(12, 0, 0))
        return vals

    def _enforce_extracted_line_values(self, po):
        """Re-apply document values after Odoo's purchase-line default computations.

        Odoo computes vendor prices/descriptions/dates for new purchase lines. The supplier
        PDF is the source for this workflow, so the extracted values are explicitly restored
        on the freshly-created draft while standard Odoo taxes/fiscal positions remain in place.
        """
        self.ensure_one()
        po_lines = po.order_line.filtered(lambda line: not line.display_type)
        if len(po_lines) != len(self.line_ids):
            raise UserError(_('The generated RFQ line count does not match the extracted PDF lines.'))
        for source, po_line in zip(self.line_ids, po_lines):
            planned = po_line.date_planned
            if self.delivery_date:
                planned = datetime.combine(self.delivery_date, time(12, 0, 0))
            po_line.write({
                'name': source.description or source.product_id.display_name,
                'price_unit': source.price_unit,
                'technical_price_unit': source.price_unit,
                'date_planned': planned,
            })

    def _attach_source_pdf_to_po(self, po):
        self.ensure_one()
        existing = self.env['ir.attachment'].search([
            ('res_model', '=', 'purchase.order'),
            ('res_id', '=', po.id),
            ('name', '=', self.file_name),
        ], limit=1)
        if existing:
            return existing
        return self.env['ir.attachment'].create({
            'name': self.file_name or '%s.pdf' % self.name,
            'type': 'binary',
            'datas': self.file_data,
            'mimetype': 'application/pdf',
            'res_model': 'purchase.order',
            'res_id': po.id,
        })

    def action_create_rfq_and_request(self):
        self.ensure_one()
        if self.purchase_order_id:
            raise UserError(_('A draft RFQ has already been created for this document.'))
        if not self._all_ready_for_rfq():
            self.state = 'review'
            raise UserError(_('Please resolve all vendor/product matches first.'))

        po = self.env['purchase.order'].with_context(ai_purchase_workflow=True).create(self._prepare_po_values())
        self._enforce_extracted_line_values(po)
        self._attach_source_pdf_to_po(po)
        self.write({
            'purchase_order_id': po.id,
            'state': 'draft_created',
            'error_message': False,
        })
        self.message_post(body=_('Draft RFQ %s was created from the uploaded PDF.') % po.display_name)
        po.message_post(body=_('This RFQ was created by AI from source document %s. Human approval is required before confirmation.') % self.name)

        if self.approver_id:
            return self.action_request_approval()
        return self._notify(
            _('Draft RFQ Created'),
            _('The draft RFQ was created. Select an approver and click Request Approval.'),
            'warning',
            True,
        )

    def action_request_approval(self):
        self.ensure_one()
        po = self.purchase_order_id
        if not po:
            raise UserError(_('Create the draft RFQ before requesting approval.'))
        if po.state not in ('draft', 'sent'):
            raise UserError(_('Approval can only be requested while the RFQ is still in draft/RFQ state.'))
        if not self.approver_id:
            raise UserError(_('Please select an approver.'))

        po.with_context(ai_purchase_workflow=True).write({'ai_approval_state': 'pending'})
        self.write({
            'state': 'approval_requested',
            'approval_requested_by_id': self.env.user.id,
            'approval_requested_date': fields.Datetime.now(),
            'rejection_reason': False,
        })

        # Avoid stacking duplicate approval activities.
        todo_type = self.env.ref('mail.mail_activity_data_todo')
        duplicates = self.activity_ids.filtered(
            lambda a: a.activity_type_id == todo_type and a.user_id == self.approver_id
        )
        duplicates.unlink()
        self.activity_schedule(
            'mail.mail_activity_data_todo',
            user_id=self.approver_id.id,
            summary=_('Review AI-created RFQ %s') % po.display_name,
            note=_('Review the source PDF and draft RFQ. Approve & Confirm only if the vendor, products, quantities and prices are correct.'),
        )
        self.message_post(body=_('Approval requested from %s for RFQ %s.') % (self.approver_id.display_name, po.display_name))
        po.message_post(body=_('AI purchase approval requested from %s.') % self.approver_id.display_name)
        return self._notify(_('Approval Requested'), _('The approver has received an Odoo activity to review this RFQ.'), 'success')

    def _check_current_user_can_approve(self):
        self.ensure_one()
        if self.env.user == self.approver_id:
            return True
        if self.env.user.has_group('purchase.group_purchase_manager'):
            return True
        raise UserError(_('Only the assigned approver or a Purchase Administrator can approve/reject this RFQ.'))

    def _close_approval_activities(self, feedback):
        self.ensure_one()
        todo_type = self.env.ref('mail.mail_activity_data_todo')
        activities = self.activity_ids.filtered(lambda a: a.activity_type_id == todo_type)
        for activity in activities:
            activity.action_feedback(feedback=feedback)

    def action_approve_and_confirm(self):
        self.ensure_one()
        self._check_current_user_can_approve()
        po = self.purchase_order_id
        if not po:
            raise UserError(_('No RFQ is linked to this document.'))
        if self.state != 'approval_requested':
            raise UserError(_('This document is not waiting for approval.'))
        if po.state not in ('draft', 'sent'):
            raise UserError(_('The linked RFQ is no longer in a confirmable draft state.'))

        now = fields.Datetime.now()
        po.with_context(ai_purchase_workflow=True).write({
            'ai_approval_state': 'approved',
            'ai_approved_by_id': self.env.user.id,
            'ai_approved_date': now,
        })
        self.write({
            'approved_by_id': self.env.user.id,
            'approved_date': now,
        })
        self._close_approval_activities(_('AI-created RFQ approved by %s.') % self.env.user.display_name)

        # Standard Odoo confirmation remains authoritative. If the company has Odoo's own
        # two-step Purchase approval enabled, button_confirm() may put the order in 'to approve'.
        po.button_confirm()
        if po.state == 'to approve' and self.env.user.has_group('purchase.group_purchase_manager'):
            po.button_approve()

        if po.state == 'purchase':
            self.state = 'done'
            self.message_post(body=_('Approved and confirmed as Purchase Order %s by %s.') % (po.display_name, self.env.user.display_name))
            return self._notify(_('Purchase Order Confirmed'), _('%s is now a confirmed Purchase Order.') % po.display_name, 'success', True)

        self.state = 'approved'
        self.message_post(body=_('Human AI-workflow approval completed by %s. Odoo standard Purchase approval is still required for %s.') % (self.env.user.display_name, po.display_name))
        return self._notify(
            _('Odoo Approval Still Required'),
            _('Human review is complete, but this RFQ is above your company Purchase approval threshold and is now in Odoo\'s standard To Approve state.'),
            'warning',
            True,
        )

    def action_reject(self):
        self.ensure_one()
        self._check_current_user_can_approve()
        if self.state != 'approval_requested':
            raise UserError(_('This document is not waiting for approval.'))
        if not self.rejection_reason:
            raise UserError(_('Enter a rejection reason before rejecting.'))
        now = fields.Datetime.now()
        if self.purchase_order_id:
            self.purchase_order_id.with_context(ai_purchase_workflow=True).write({'ai_approval_state': 'rejected'})
            self.purchase_order_id.message_post(body=_('AI purchase approval rejected by %s. Reason: %s') % (self.env.user.display_name, self.rejection_reason))
        self.write({
            'state': 'rejected',
            'rejected_by_id': self.env.user.id,
            'rejected_date': now,
        })
        self._close_approval_activities(_('RFQ rejected by %s.') % self.env.user.display_name)
        self.message_post(body=_('RFQ rejected by %s. Reason: %s') % (self.env.user.display_name, self.rejection_reason))
        return self._notify(_('RFQ Rejected'), _('The RFQ remains unconfirmed and can be corrected before requesting approval again.'), 'warning')

    def action_open_purchase_order(self):
        self.ensure_one()
        if not self.purchase_order_id:
            raise UserError(_('No RFQ/PO is linked to this document.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('RFQ / Purchase Order'),
            'res_model': 'purchase.order',
            'res_id': self.purchase_order_id.id,
            'view_mode': 'form',
        }


class AiPurchaseDocumentLine(models.Model):
    _name = 'ai.purchase.document.line'
    _description = 'AI Purchase Document Line'
    _order = 'id'

    document_id = fields.Many2one(
        'ai.purchase.document',
        required=True,
        ondelete='cascade',
        index=True,
    )
    company_id = fields.Many2one(related='document_id.company_id', store=True, readonly=True)
    description = fields.Char(required=True)
    vendor_sku = fields.Char(string='Vendor SKU')
    quantity = fields.Float(required=True, digits='Product Unit')
    extracted_uom = fields.Char(string='PDF UoM')
    price_unit = fields.Float(string='Unit Price', required=True, digits='Product Price')
    price_extracted = fields.Boolean(string='Price Found in PDF', readonly=True)
    tax_rate = fields.Float(string='PDF Tax %')
    confidence = fields.Float(digits=(5, 4), readonly=True)
    human_reviewed = fields.Boolean(
        string='Human Reviewed',
        help='Mark this after manually verifying/correcting an uncertain extracted line.',
    )

    product_id = fields.Many2one(
        'product.product',
        string='Matched Product',
        domain="[('purchase_ok', '=', True)]",
    )
    uom_id = fields.Many2one('uom.uom', string='RFQ UoM')
    match_status = fields.Selection(
        [('matched', 'Matched'), ('review', 'Review Required')],
        default='review',
        readonly=True,
    )
    match_score = fields.Float(readonly=True, digits=(5, 4))
    match_note = fields.Char(readonly=True)

    @api.onchange('product_id')
    def _onchange_product_id_ai_purchase(self):
        for line in self:
            if line.product_id:
                line.uom_id = line.product_id.uom_id
                line.human_reviewed = True
                line.match_status = 'matched'
                line.match_score = 1.0
                line.match_note = _('Product selected manually.')
            else:
                line.uom_id = False
                line.human_reviewed = False
                line.match_status = 'review'

    @api.onchange('uom_id', 'price_unit')
    def _onchange_manual_line_review(self):
        for line in self:
            if line.product_id:
                line.human_reviewed = True
                line.price_extracted = True
                line.match_status = 'matched'
                line.match_note = _('Line manually reviewed/corrected.')
