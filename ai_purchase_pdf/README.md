# AI Purchase PDF to RFQ — Odoo 19

This addon implements a controlled AI-assisted purchasing workflow for **Odoo 19**:

**Upload supplier PDF → AI extracts purchase data → match existing Odoo vendor/products → create Draft RFQ → request a human approval → Approve & Confirm → standard Odoo Purchase Order.**

The AI never independently authorizes the purchase. The addon blocks the standard Odoo `Confirm Order` / `Approve Order` path until the human approval step has been completed.

## What the addon does

- Upload a supplier quotation, pro-forma invoice, purchase request, or other purchase-related **PDF**.
- Send the PDF to the configured **OpenAI Responses API** using PDF/vision input.
- Request strict structured JSON for:
  - vendor name / tax ID / email
  - vendor reference
  - document date
  - currency
  - payment terms
  - delivery date
  - item description and vendor SKU
  - quantity and UoM
  - unit price
  - tax rate shown in the PDF
  - extraction confidence
- Match only **existing** Odoo vendors and products.
- Use vendor VAT/email/name and vendor product codes/names/internal references for deterministic matching.
- Stop in **Needs Review** when a vendor, product, UoM, price, currency, or payment term cannot be matched safely.
- Create a normal Odoo `purchase.order` in RFQ/Draft state after required data is ready.
- Copy the original supplier PDF to the generated RFQ/PO attachments.
- Create an Odoo To-Do activity for the selected approver.
- Require approval by the assigned approver or a Purchase Administrator.
- On approval, call Odoo's standard `purchase.order.button_confirm()` flow.
- Respect Odoo's standard two-step Purchase approval rules. If Odoo itself requires manager approval, the PO remains in **To Approve** until that standard step is completed.
- Record requested-by, approved-by, rejected-by, timestamps, and Chatter messages.

## Important safety controls

1. **No vendor/product creation by AI.** Uncertain master-data matches require a person.
2. **Protected approval fields.** The workflow fields on `purchase.order` cannot be changed manually through normal ORM create/write calls.
3. **Confirmation lock.** Both `button_confirm()` and `button_approve()` are blocked for AI-created RFQs until human approval is recorded.
4. **Original document retained.** The PDF is attached to the draft RFQ for side-by-side review.
5. **No automatic financial commitment.** AI creates a draft only. A person performs the approval.
6. **Multi-company record rules.** Users see AI purchase documents only for allowed companies.

## Requirements

- Odoo 19
- Purchase app installed
- Python `requests` package (normally already present in an Odoo environment)
- Outbound HTTPS access from the Odoo server to the configured AI endpoint
- An OpenAI API key with access to the configured model

Default model: `gpt-5.6-luna`

Default endpoint: `https://api.openai.com/v1/responses`

## Installation

1. Copy the folder `ai_purchase_pdf` into a custom addons directory, for example:

   ```bash
   /opt/odoo19/custom_addons/ai_purchase_pdf
   ```

2. Make sure the parent directory is included in `addons_path`, for example:

   ```ini
   addons_path = /opt/odoo19/odoo/addons,/opt/odoo19/custom_addons
   ```

3. Restart Odoo.

4. Upgrade the Apps list from Apps (developer mode if required).

5. Search for **AI Purchase PDF to RFQ** and install it.

CLI installation example:

```bash
./odoo-bin -c /opt/odoo19/odoo.conf -d YOUR_DATABASE -i ai_purchase_pdf --stop-after-init
```

To upgrade after replacing the module:

```bash
./odoo-bin -c /opt/odoo19/odoo.conf -d YOUR_DATABASE -u ai_purchase_pdf --stop-after-init
```

## Configuration

Open:

**Purchase → Configuration → Settings → AI Purchase PDF**

Configure:

- **OpenAI API Key** — required.
- **OpenAI Model** — default `gpt-5.6-luna`.
- **OpenAI Responses Endpoint** — normally keep the default.
- **Default Purchase Approver** — recommended.
- **Automatic Match Threshold** — default `0.90` (90%).
- **Maximum PDF Size** — default 20 MB.
- **AI Timeout** — default 120 seconds.

The approver should be an internal user with Purchase access so they can open and review the RFQ.

## Workflow — simple version

### Step 1 — Upload

Go to:

**Purchase → Orders → AI Purchase PDFs → New**

Upload the supplier PDF and choose an approver. Save the record.

### Step 2 — Process with AI

Click **Process PDF with AI**.

The addon sends the PDF to OpenAI and requests structured purchase data. For PDFs, the API can use both extracted text and page images, which also helps with scanned/visual PDFs.

### Step 3 — Match Odoo data

The addon looks for an existing vendor and existing products.

If every required match is safe, it creates the draft RFQ automatically.

If anything is uncertain, the record becomes **Needs Review**. A Purchase user selects the correct vendor/product/UoM or corrects the line value and clicks **Retry Matching**. Once ready, click **Create Draft RFQ**.

### Step 4 — Draft RFQ

Odoo creates a normal RFQ with:

- matched vendor
- vendor reference
- currency/payment terms when matched
- products
- quantities
- UoM
- supplier-document unit prices
- expected delivery date when available
- source document reference

The original supplier PDF is attached to the RFQ.

> PDF tax rate is displayed in the extraction review. The generated RFQ continues to use Odoo's normal product/fiscal-position tax logic, so the approver should compare taxes against the source PDF before approving.

### Step 5 — Approval request

If an approver is configured, the addon automatically sets the workflow to **Approval Requested** and creates an Odoo To-Do activity for that employee.

If no approver was selected, choose one and click **Request Approval**.

### Step 6 — Human review

The approver checks:

- original PDF
- vendor
- RFQ line products
- quantities
- UoM
- prices
- taxes
- currency
- payment terms
- delivery date

They can correct normal RFQ fields while the RFQ is still a draft.

### Step 7A — Reject

Enter a **Rejection Reason** and click **Reject**.

The RFQ remains unconfirmed. The purchasing employee can correct the draft RFQ and request approval again.

### Step 7B — Approve & Confirm

Click **Approve & Confirm**.

The addon records the employee and timestamp, unlocks the AI approval gate, and calls Odoo's standard `button_confirm()`.

- If Odoo standard Purchase approval allows it, the RFQ becomes a **Purchase Order**.
- If your company uses Odoo's two-step Purchase approval and the standard threshold requires a manager, it becomes **To Approve**. The normal Odoo Purchase Manager approval still applies.

## Workflow diagram

```text
Supplier PDF
     │
     ▼
AI Purchase PDF record
     │
     ▼
OpenAI PDF extraction
     │
     ▼
Existing Odoo Vendor/Product Matching
     │
     ├── uncertain ──► Needs Review ──► employee resolves match
     │                                      │
     └──────────────────────────────────────┘
                         │
                         ▼
                   Draft Odoo RFQ
                         │
                         ▼
                  Approval Activity
                         │
                 ┌───────┴────────┐
                 │                │
              Reject      Approve & Confirm
                 │                │
                 ▼                ▼
          Draft remains     Odoo button_confirm()
                                  │
                         ┌────────┴────────┐
                         │                 │
                    Purchase Order   Odoo To Approve
                                      (if standard
                                      two-step rule)
```

## Access rights

Users need the standard **Purchase User** group to use AI Purchase PDFs.

The assigned approver should also have Purchase access. Purchase Administrators can approve/reject as an administrative override.

## Data/privacy note

When **Process PDF with AI** is clicked, the uploaded supplier PDF is sent to the configured OpenAI API endpoint. Do not use this workflow for documents that your organization's policy does not permit you to send to that provider. API usage/costs are billed by the configured provider account.

## Test checklist before production

Use a staging database first and test at least:

- native-text PDF
- scanned PDF
- known vendor and known products
- unknown vendor
- unknown product
- ambiguous product names
- vendor product code matching
- multiple currencies
- payment terms
- different UoMs
- zero/missing prices
- rejection and re-request
- standard Purchase two-step approval enabled/disabled
- Purchase User vs Purchase Administrator permissions
- multi-company access
- receipt creation after final confirmation (when Inventory/Purchase Stock is installed)

## Technical notes

Main models:

- `ai.purchase.document`
- `ai.purchase.document.line`
- inherited `purchase.order`
- inherited `res.config.settings`

Main Purchase workflow protection:

- `purchase.order.button_confirm()` override
- `purchase.order.button_approve()` override
- protected AI workflow fields on Purchase Order

The addon deliberately uses Odoo ORM and the standard Purchase confirmation methods rather than directly writing `state='purchase'`.
