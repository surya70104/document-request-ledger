"""Default requirement-list templates (version 1) for the six engagement types.

Each item:
  document     name of what is asked for. Tokens {period_start}, {period_end}, {fy_label} are filled
               from the engagement's period.
  label        wording the client reads (optional; falls back to `description`)
  description  internal note for the team
  condition    expression over the entity profile (see conditions.py); omitted = always asked
  significance Material (to the opinion) or Routine
  priority     1 (highest) .. 5
  period       'engagement' (full period), 'as_at' (period end only) or 'none'
"""


def item(document, significance="Routine", priority=3, condition=None, label=None, description=None,
         period="engagement"):
    return {
        "document": document,
        "significance": significance,
        "priority": priority,
        "condition": condition,
        "label": label,
        "description": description,
        "period": period,
    }


M, R = "Material", "Routine"

TEMPLATES = {
    "Statutory audit": {
        "title": "Statutory audit - standard requirement list",
        "items": [
            item("Trial balance as at {period_end}", M, 1, period="as_at",
                 label="Final trial balance in Excel, after all year-end adjustments."),
            item("Bank statements - all current accounts", M, 1,
                 label="Monthly statements for every bank account, {period_start} to {period_end}."),
            item("Bank balance confirmations as at {period_end}", M, 2, period="as_at",
                 label="Bank certificates / confirmation letters for the closing balance of each account."),
            item("Fixed asset register", M, 2, "has_fixed_assets",
                 label="Asset-wise register with additions, disposals and depreciation for the year."),
            item("Stock register and physical count sheets", M, 1, "has_inventory",
                 label="Closing stock listing with quantities and rates, and the count sheets."),
            item("Loan agreements, sanction letters and lender statements", M, 2, "has_borrowings",
                 label="Sanction letters and the lender's year-end statement for each loan."),
            item("Related party list and transactions", M, 2, "has_related_parties",
                 label="Names of related parties and all transactions with them during the year."),
            item("Form 15CA / 15CB and foreign remittance details", R, 3, "has_foreign_remittance",
                 label="Copies of Form 15CA / 15CB and a list of payments sent abroad."),
            item("Payroll register, PF / ESI challans", R, 3, "has_employees",
                 label="Month-wise salary register and PF / ESI payment challans."),
            item("GST returns (GSTR-1, GSTR-3B) and reconciliation", R, 3, "is_gst_registered",
                 label="Filed GST returns for the year and the reconciliation with the books."),
            item("TDS returns and challans", R, 3,
                 label="Quarterly TDS returns and challans for the year."),
            item("Board and committee minutes {fy_label}", R, 3,
                 label="Signed minutes of every board and committee meeting held in the year."),
            item("Secretarial compliance and LODR filings", R, 3, "is_listed",
                 label="Stock-exchange filings and the secretarial compliance report."),
            item("Prior-year signed financial statements", R, 2, period="none",
                 label="Last year's signed balance sheet, profit and loss account and notes."),
        ],
    },
    "Tax audit": {
        "title": "Tax audit (s.44AB, Form 3CD) - standard requirement list",
        "items": [
            item("Trial balance as at {period_end}", M, 1, period="as_at"),
            item("Profit and loss account and balance sheet {fy_label}", M, 1),
            item("Fixed asset register with depreciation as per Income-tax Act", M, 2, "has_fixed_assets",
                 label="Asset-wise block of assets with depreciation worked out as per the Income-tax Act."),
            item("TDS / TCS returns, challans and reconciliation", M, 2,
                 label="All TDS and TCS returns and challans, with a reconciliation to the books."),
            item("GST turnover reconciliation with books", R, 3, "is_gst_registered"),
            item("Stock valuation method and closing stock details", M, 2, "has_inventory"),
            item("Loan confirmations and interest workings", R, 3, "has_borrowings"),
            item("Payments to related parties (s.40A(2)(b))", M, 2, "has_related_parties",
                 label="Payments made to directors, partners, relatives and concerns they are interested in."),
            item("Form 15CA / 15CB and TDS on foreign payments", R, 3, "has_foreign_remittance"),
            item("Cash payments and expenses above the s.40A(3) limit", R, 3,
                 label="List of cash payments and expenses above the permitted limit."),
            item("Statutory dues paid after the due date (s.43B)", R, 3,
                 label="Statutory dues unpaid at year end and the date they were paid."),
            item("Turnover bifurcation for Form 3CD clause 44 (GST registered)", R, 3,
                 "is_gst_registered and turnover_inr > 50000000"),
        ],
    },
    "GST annual return": {
        "title": "GST annual return (GSTR-9 / 9C) - standard requirement list",
        "items": [
            item("Sales register {fy_label}", M, 1, label="Month-wise sales register with GST split."),
            item("Purchase register {fy_label}", M, 1, label="Month-wise purchase register with ITC claimed."),
            item("GSTR-1 and GSTR-3B for all months", M, 1,
                 label="Filed copies of GSTR-1 and GSTR-3B for April to March."),
            item("GSTR-2A / 2B reconciliation with books", M, 2,
                 label="Reconciliation of ITC in the books with GSTR-2A / 2B."),
            item("ITC reversals and ineligible credit workings", R, 3),
            item("HSN-wise summary of outward and inward supplies", R, 3),
            item("E-way bill and transport records", R, 4, "has_inventory"),
            item("Export invoices, shipping bills and LUT", R, 3, "has_exports"),
            item("Audited financial statements {fy_label}", M, 2, "turnover_inr > 50000000",
                 label="Audited accounts, needed for the GSTR-9C reconciliation statement."),
            item("Books-to-GSTR-9 turnover reconciliation (GSTR-9C)", M, 2, "turnover_inr > 50000000"),
            item("Notices, demand orders and amendments during the year", R, 4),
        ],
    },
    "Bank audit": {
        "title": "Bank / stock audit for lenders - standard requirement list",
        "items": [
            item("Sanction letter and loan terms", M, 1, "has_borrowings",
                 label="Latest sanction letter with all terms and conditions."),
            item("Drawing power statements", M, 1, "has_borrowings"),
            item("Stock statements submitted to the bank", M, 1, "has_inventory and has_borrowings"),
            item("Stock register and physical verification report", M, 1, "has_inventory"),
            item("Book debts ageing and debtors list", M, 2),
            item("Creditors ageing and list", R, 3),
            item("Bank statements - all accounts", M, 1),
            item("Fixed asset register", R, 3, "has_fixed_assets"),
            item("Term loan utilisation certificate and supporting invoices", M, 2, "has_borrowings"),
            item("Insurance policies covering stock and assets", R, 3, "has_inventory or has_fixed_assets"),
            item("Latest audited financial statements", M, 2, period="none"),
            item("Previous stock audit / concurrent audit report", R, 4, period="none"),
        ],
    },
    "Internal audit": {
        "title": "Internal audit - standard requirement list",
        "items": [
            item("Process narratives and standard operating procedures", M, 1, period="none"),
            item("Organisation chart and delegation of authority", R, 2, period="none"),
            item("Previous internal audit reports and action-taken status", R, 2, period="none"),
            item("Vendor master and purchase-to-pay sample population", M, 2),
            item("Customer master and order-to-cash sample population", M, 2),
            item("Inventory management policy and count procedures", M, 2, "has_inventory", period="none"),
            item("Payroll master, attendance and leave records", R, 3, "has_employees"),
            item("Fixed asset policy and capitalisation approvals", R, 3, "has_fixed_assets"),
            item("Treasury and borrowing policy, covenant compliance", M, 2, "has_borrowings"),
            item("Foreign exchange and remittance policy", R, 3, "has_foreign_remittance", period="none"),
            item("Related-party transaction policy and approvals", M, 2, "has_related_parties"),
            item("IT general controls: access, change and backup records", R, 3),
            item("Statutory compliance calendar and filing evidence", R, 3),
        ],
    },
    "Certification work": {
        "title": "Certification work - standard requirement list",
        "items": [
            item("Request letter stating purpose, recipient and format of the certificate", M, 1, period="none",
                 label="The letter or email from the recipient that asks for the certificate, with its format."),
            item("Books of account extract for the certified period", M, 1,
                 label="Ledger extract supporting the figures to be certified."),
            item("Trial balance as at {period_end}", M, 1, period="as_at"),
            item("Bank statements for the certified period", M, 2),
            item("Board resolution authorising the certificate", R, 2, period="none"),
            item("Constitutional documents (MOA / AOA / partnership deed)", R, 3, period="none"),
            item("Loan sanction letters and covenant definitions", M, 2, "has_borrowings", period="none"),
            item("GST returns supporting turnover certification", R, 3, "is_gst_registered"),
            item("Foreign inward remittance certificates (FIRC / FIRA)", R, 3,
                 "has_exports or has_foreign_remittance"),
            item("Draft certificate in the recipient's prescribed format", M, 2, period="none"),
        ],
    },
}
