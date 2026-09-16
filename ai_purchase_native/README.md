# AI Purchase PDF - Native Odoo AI (Odoo 19)

Technical module name: `ai_purchase_native`

## Final workflow

1. Configure the Gemini key in Odoo: **Settings → AI → Providers → Use your own Google Gemini account**.
2. Open **Purchase → Orders → Upload Purchase PDF**.
3. Upload a supplier quotation / purchase-related PDF and choose the employee who will approve it.
4. Click **Create Draft RFQ with Odoo AI**.
5. The module uses Odoo's own AI `LLMApiService` with the configured Google provider to read the complete PDF.
6. Odoo immediately creates a standard draft `purchase.order` (RFQ), attaches the original PDF, and opens the RFQ.
7. Odoo matches existing vendors/products where possible.
   - A new vendor may be created when a clear vendor name exists but no vendor matches.
   - An unmatched product uses one reusable placeholder product named `AI Product Match Required`; the employee replaces it on the draft RFQ.
8. Odoo creates an approval activity for the selected approver.
9. The approver reviews the draft RFQ and source PDF.
10. The normal Odoo **Confirm Order** button is blocked for an AI-created RFQ until human approval.
11. The approver clicks **Approve & Confirm**.
12. The module records approver + timestamp and calls Odoo's standard `purchase.order.button_confirm()`.
13. Odoo creates the confirmed Purchase Order, subject to any standard Odoo Purchase two-step approval policy already configured by the company.

## Design rules

- AI prepares the draft; AI never authorizes the purchase.
- No Gemini key is stored in this custom module.
- The module reads the Gemini key through Odoo's native AI provider configuration.
- The original PDF stays attached to the RFQ/PO.
- Uncertain products never become invented product master records. They use one explicit review placeholder and confirmation is blocked until a real product is selected.
- Taxes are left to Odoo's normal product/fiscal-position logic and must be checked by the reviewer.

## Dependencies

- Odoo 19 Enterprise
- Purchase
- Mail
- Odoo AI (`ai_app`)
- Google Gemini API key configured in Odoo AI settings

## Recommended installation on Odoo.sh

This is a clean replacement for the earlier local-parser prototype. Uninstall the old `ai_purchase_pdf` prototype if you no longer need it, then add this `ai_purchase_native` folder to the repository and install **AI Purchase PDF - Native Odoo AI** from Apps.
