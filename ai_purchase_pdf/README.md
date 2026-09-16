# AI Purchase PDF to RFQ — Odoo 19

Version: **19.0.1.1.0**

This module supports two PDF-processing modes:

1. **No API Test Mode** — default. No OpenAI key, no external AI call, no API cost.
2. **OpenAI API** — optional later for intelligent extraction from varied supplier PDF layouts.

The purchase-control workflow is identical in both modes:

**Upload PDF → Extract data → Match existing vendor/products → Draft RFQ → Human Approval → Standard Odoo PO Confirmation**

## No API Test Mode

No API Test Mode uses the PDF library already available in the Odoo 19 Python stack to read embedded text, then applies deterministic rules to recognize common supplier quotation fields.

It can recognize common labels such as:

- Supplier / Vendor
- VAT / TIN / BIN
- Email
- Quotation / Reference
- Date
- Currency
- Payment Terms
- Delivery Date
- Product / Item / Description
- SKU / Item Code
- Quantity / Qty
- UoM / Unit
- Unit Price / Rate
- Tax / VAT rate

It also attempts simple tables with headers such as **Description | Qty | Unit Price**.

### Important limitation

No API Test Mode does **not** perform OCR. A PDF that is only a scanned image/photo will not contain readable embedded text. For testing, use a PDF exported from Word/Excel/Odoo/ERP or another source where the text can be selected/copied.

If fields are not recognized, the document goes to **Needs Review**. You can select the vendor, choose products, correct quantities/UoM/prices, and add/delete extracted lines manually before creating the RFQ.

## Configure No API Test Mode

Go to:

**Purchase → Configuration → Settings → AI Purchase PDF**

Set:

- **PDF Processing Mode:** `No API Test Mode`
- **Default Human Approver:** choose an employee/purchase manager
- **Automatic Match Threshold:** keep `0.90` for initial testing
- **Maximum PDF Size:** e.g. `20 MB`

Save Settings.

No OpenAI API key is needed.

## Test PDF example

For the easiest test, create a text-based PDF containing something similar to:

```text
Supplier: ABC Supplier Ltd.
Quotation No: QT-2026-0058
Date: 16/09/2026
Currency: BDT
Payment Terms: 30 Days
Delivery Date: 25/09/2026

Product: Dell Latitude 5450
SKU: DELL-5450
Quantity: 5
UoM: Units
Unit Price: 125000

Product: Logitech Mouse
SKU: LOG-M185
Quantity: 5
UoM: Units
Unit Price: 2500
```

For automatic master-data matching, create the corresponding vendor/products in Odoo first. The module does not create vendor/product master records automatically.

## User workflow

### 1. Upload
Go to **Purchase → Orders → AI Purchase PDFs** and create a record. Upload the PDF and select `No API Test Mode`.

### 2. Process PDF
Click **Process PDF**.

The module extracts embedded PDF text locally and tries to structure the purchase data.

### 3. Needs Review
If anything is uncertain, status becomes **Needs Review**.

Review:

- Matched Vendor
- Currency / Payment Terms
- Product on every line
- Quantity
- RFQ UoM
- Unit Price

You can add/delete lines in this stage if the local parser did not recognize the table correctly.

Click **Retry Matching** after corrections if needed.

### 4. Create Draft RFQ
Click **Create Draft RFQ**.

The original PDF is attached to the RFQ.

### 5. Request Human Approval
If an approver is configured, approval is requested automatically. Otherwise choose an approver and click **Request Approval**.

The module prevents normal Odoo confirmation until the human approval gate is completed.

### 6. Approve or Reject
The assigned approver (or Purchase Administrator) compares the source PDF and RFQ.

- **Reject:** RFQ remains unconfirmed and can be corrected.
- **Approve & Confirm:** records the approver/date and calls Odoo's standard purchase confirmation flow.

If standard Odoo two-step Purchase approval is enabled, the order can still move to **To Approve** according to normal Odoo rules.

## Switching to OpenAI later

Go to **Purchase → Configuration → Settings → AI Purchase PDF** and change:

**PDF Processing Mode → OpenAI API**

Then configure the API key/model/endpoint fields. The RFQ, approval, audit and confirmation workflow does not change.

## Upgrade from 19.0.1.0.1

Replace the addon folder with this version, push it to Odoo.sh, let the branch rebuild, then upgrade the module from Apps (or with `-u ai_purchase_pdf`).

Existing failed test records may be retried after selecting `No API Test Mode`; creating a fresh test record is also fine.
