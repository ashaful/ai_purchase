# AI Purchase PDF - Native Odoo AI (Odoo 19) — v19.0.2.1.0

Technical module name: `ai_purchase_native`

## Final workflow

1. Configure the Gemini key in Odoo: **Settings → AI → Providers → Use your own Google Gemini account**.
2. Open **Purchase → Orders → Upload Purchase PDF**.
3. Upload a supplier quotation / purchase-related PDF and choose the employee who will approve it.
4. Click **Create Draft RFQ with Odoo AI**.
5. The module uses Odoo's own AI `LLMApiService` with the configured Google provider to read the complete PDF.
6. Odoo immediately creates a standard draft `purchase.order` (RFQ), attaches the original PDF, and opens the RFQ.
7. Odoo matches existing vendors/products where possible.
   - If the supplier does not exist, the module creates a new Vendor automatically and uses it on the RFQ.
   - If a product does not exist, the module creates a new Goods product automatically, adds the supplier relation/vendor code/vendor price, and uses it on the RFQ.
   - If Gemini cannot identify any supplier name at all, processing stops instead of creating an unsafe anonymous vendor.
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
- Missing products are created automatically from the Gemini-extracted description/code, marked as purchasable Goods, and linked to the extracted vendor through Odoo supplier information.
- Missing vendors are created automatically when Gemini provides a supplier name.
- Taxes are left to Odoo's normal product/fiscal-position logic and must be checked by the reviewer.

## Dependencies

- Odoo 19 Enterprise
- Purchase
- Mail
- Odoo AI (`ai_app`)
- Google Gemini API key configured in Odoo AI settings

## Recommended installation on Odoo.sh

This is a clean replacement for the earlier local-parser prototype. Uninstall the old `ai_purchase_pdf` prototype if you no longer need it, then add this `ai_purchase_native` folder to the repository and install **AI Purchase PDF - Native Odoo AI** from Apps.


## Automatic master-data creation (v19.0.2.1.0)

When Gemini extracts a vendor or product that is not already in Odoo:

- **Vendor:** creates `res.partner` with supplier rank, name, email/phone when available, and attempts VAT/TIN safely.
- **Product:** creates a `product.template` / `product.product` as a purchasable Goods product, keeps the PDF purchase description, uses a unique extracted product code/barcode when safe, and adds `product.supplierinfo` for the vendor with vendor code/name and PDF price.
- The draft RFQ then uses those newly created records immediately.
- The human approval step remains mandatory before purchase confirmation.
