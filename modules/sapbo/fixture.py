"""
Synthetic SAP BusinessObjects extract bundle: the "CNR Finance" Universe and ten Web Intelligence reports.

It mirrors what the WS1 extractor captures from a real BO server (REST/SDK responses, flattened into the bundle
format documented in docs/sapbo/extract-bundle.md), over the `cnfinance` catalog that already exists in Unity
Catalog. The Universe points at an *Oracle* schema `CNR_FIN` whose tables have the same columns as
`cnfinance.silver` (plus one aggregate table that maps to `cnfinance.gold.revenue_monthly`), so the captured
report SQL can be transpiled and re-run on Databricks for reconciliation.

Traps planted on purpose, so the gap report has real findings (roadmap §4):
  * two measures called "Revenue" with different SQL (GL revenue vs billing revenue)              -> C6 duplicate
  * a cryptic, undocumented measure FB_AMT_ADJ                                                     -> LLM description
  * report R06 uses custom SQL with a filter the Universe doesn't have                            -> SQL conflict
  * an aggregate-aware measure (@Aggregate_Aware over AGG_REVENUE_MONTHLY / FREIGHT_BILLING)       -> partial
  * an alias table (DEST_STATION alias of STATIONS) and a derived table (DT_PERIOD_CALENDAR)
  * a theta join to AGING_BANDS, a table with no Unity Catalog counterpart                        -> needs human
  * a join with no cardinality set                                                                 -> derived
  * an AVG measure with a Sum projection                                                           -> conflict
  * Oracle-isms (NVL, DECODE, boolean flags stored as NUMBER(1))                                  -> transpile rules
  * a row restriction on cost center region                                                        -> unsupported
  * Webi variables: RelativeValue, RunningSum, Previous, Rank, Percentage, ForEach, If/Then/Else

`build()` returns the bundle as a dict; `write(dir)` writes it as files (samples/sapbo/cnr_finance/).
"""
from __future__ import annotations

import json
import pathlib

SCHEMA = "CNR_FIN"
UNIVERSE_ID = "UNV_CNR_FIN"

# Physical tables (Oracle side) -> columns with Ossie logical types (same columns as cnfinance.silver).
TABLES: dict[str, list[tuple[str, str]]] = {
    "FREIGHT_BILLING": [("REVENUE_LINE_ID", "Integer"), ("PERIOD_ID", "Integer"), ("FISCAL_YEAR", "Integer"),
                        ("CUSTOMER_ID", "String"), ("COMMODITY_CODE", "String"), ("ORIGIN_STATION_CODE", "String"),
                        ("DESTINATION_STATION_CODE", "String"), ("INVOICE_ID", "String"), ("CARLOADS", "Integer"),
                        ("NET_TONS", "Integer"), ("LOADED_MILES", "Integer"), ("LINEHAUL_REVENUE", "Decimal"),
                        ("FUEL_SURCHARGE_REVENUE", "Decimal"), ("ACCESSORIAL_REVENUE", "Decimal"),
                        ("LINE_REVENUE", "Decimal"), ("REVENUE_TON_MILES", "Integer"), ("HAUL_LENGTH_BAND", "String")],
    "AR_INVOICES": [("INVOICE_ID", "String"), ("CUSTOMER_ID", "String"), ("INVOICE_DATE", "Date"),
                    ("DUE_DATE", "Date"), ("INVOICE_AMOUNT", "Decimal"), ("AMOUNT_PAID", "Decimal"),
                    ("SNAPSHOT_DATE", "Date"), ("PERIOD_ID", "Integer"), ("FISCAL_YEAR", "Integer"),
                    ("OPEN_AMOUNT", "Decimal"), ("DAYS_TO_PAY", "Integer"), ("DAYS_PAST_DUE", "Integer"),
                    ("AGING_BUCKET", "String"), ("IS_PAID_LATE", "Boolean"), ("LAST_PAYMENT_DATE", "Date"),
                    ("IS_DISPUTED", "Boolean"), ("PAYMENT_STATUS", "String")],
    "AP_INVOICES": [("AP_DOCUMENT_ID", "String"), ("VENDOR_ID", "String"), ("VENDOR_INVOICE_NUMBER", "String"),
                    ("ACCOUNT_NUMBER", "Integer"), ("COST_CENTER_ID", "String"), ("INVOICE_DESCRIPTION", "String"),
                    ("INVOICE_DATE", "Date"), ("DUE_DATE", "Date"), ("INVOICE_AMOUNT", "Decimal"),
                    ("AMOUNT_PAID", "Decimal"), ("SNAPSHOT_DATE", "Date"), ("PERIOD_ID", "Integer"),
                    ("FISCAL_YEAR", "Integer"), ("OPEN_AMOUNT", "Decimal"), ("DAYS_TO_PAY", "Integer"),
                    ("DAYS_PAST_DUE", "Integer"), ("AGING_BUCKET", "String"), ("IS_PAID_LATE", "Boolean"),
                    ("PAYMENT_DATE", "Date"), ("IS_FUEL", "Boolean")],
    "GL_JOURNAL_LINES": [("JOURNAL_ID", "String"), ("LINE_NUMBER", "Integer"), ("POSTING_DATE", "Date"),
                         ("PERIOD_ID", "Integer"), ("FISCAL_YEAR", "Integer"), ("JOURNAL_SOURCE", "String"),
                         ("JOURNAL_DESCRIPTION", "String"), ("ACCOUNT_NUMBER", "Integer"),
                         ("COST_CENTER_ID", "String"), ("DEBIT_AMOUNT", "Decimal"), ("CREDIT_AMOUNT", "Decimal"),
                         ("NET_AMOUNT", "Decimal"), ("REVENUE_AMOUNT", "Decimal"), ("EXPENSE_AMOUNT", "Decimal")],
    "BUDGET_LINES": [("PERIOD_ID", "Integer"), ("FISCAL_YEAR", "Integer"), ("COST_CENTER_ID", "String"),
                     ("ACCOUNT_NUMBER", "Integer"), ("BUDGET_AMOUNT", "Decimal"), ("REVENUE_BUDGET", "Decimal"),
                     ("EXPENSE_BUDGET", "Decimal")],
    "FISCAL_PERIODS": [("PERIOD_ID", "Integer"), ("FISCAL_YEAR", "Integer"), ("FISCAL_QUARTER", "Integer"),
                       ("PERIOD_NUMBER", "Integer"), ("PERIOD_START", "Date"), ("PERIOD_END", "Date"),
                       ("IS_CLOSED", "Boolean"), ("QUARTER_LABEL", "String"), ("DAYS_IN_PERIOD", "Integer")],
    "COMMODITY_GROUPS": [("GROUP_CODE", "String"), ("GROUP_NAME", "String"), ("BUSINESS_SEGMENT", "String"),
                         ("REVENUE_ACCOUNT_NUMBER", "Integer")],
    "COMMODITIES": [("COMMODITY_CODE", "String"), ("COMMODITY_NAME", "String"), ("GROUP_CODE", "String"),
                    ("CAR_TYPE", "String"), ("IS_HAZARDOUS", "Boolean")],
    "STATIONS": [("STATION_CODE", "String"), ("STATION_NAME", "String"), ("PROVINCE_STATE", "String"),
                 ("COUNTRY", "String"), ("REGION", "String"), ("STATION_TYPE", "String"), ("LATITUDE", "Float"),
                 ("LONGITUDE", "Float")],
    "CUSTOMERS": [("CUSTOMER_ID", "String"), ("CUSTOMER_NAME", "String"), ("INDUSTRY", "String"),
                  ("HQ_CITY", "String"), ("HQ_PROVINCE_STATE", "String"), ("HQ_COUNTRY", "String"),
                  ("CREDIT_LIMIT", "Decimal"), ("PAYMENT_TERMS_DAYS", "Integer"), ("CUSTOMER_SINCE", "Date"),
                  ("IS_KEY_ACCOUNT", "Boolean"), ("IS_ACTIVE", "Boolean")],
    "VENDORS": [("VENDOR_ID", "String"), ("VENDOR_NAME", "String"), ("VENDOR_CATEGORY", "String"),
                ("VENDOR_CITY", "String"), ("VENDOR_PROVINCE_STATE", "String"), ("VENDOR_COUNTRY", "String"),
                ("VENDOR_PAYMENT_TERMS_DAYS", "Integer")],
    "COST_CENTERS": [("COST_CENTER_ID", "String"), ("COST_CENTER_NAME", "String"), ("DEPARTMENT", "String"),
                     ("COST_CENTER_REGION", "String"), ("COST_CENTER_FUNCTION", "String"), ("HEADCOUNT", "Integer")],
    "GL_ACCOUNTS": [("ACCOUNT_NUMBER", "Integer"), ("ACCOUNT_NAME", "String"), ("ACCOUNT_TYPE", "String"),
                    ("FINANCIAL_STATEMENT", "String"), ("STATEMENT_LINE", "String"), ("NORMAL_BALANCE", "String"),
                    ("IS_ACTIVE_ACCOUNT", "Boolean")],
    # aggregate table used by @Aggregate_Aware (maps to cnfinance.gold.revenue_monthly)
    "AGG_REVENUE_MONTHLY": [("PERIOD_ID", "Integer"), ("FISCAL_YEAR", "Integer"), ("FREIGHT_REVENUES", "Decimal"),
                            ("TOTAL_CARLOADS", "Integer")],
    # lookup with a theta join; it has no Unity Catalog counterpart (trap)
    "AGING_BANDS": [("BAND_NAME", "String"), ("MIN_DAYS", "Integer"), ("MAX_DAYS", "Integer")],
}

ORACLE_TYPES = {"Integer": "NUMBER(10)", "Decimal": "NUMBER(18,2)", "String": "VARCHAR2(200)", "Date": "DATE",
                "Boolean": "NUMBER(1)", "Float": "NUMBER"}

# Join id -> (expression, cardinality as shown in IDT or None, contexts)
JOINS = [
    ("J01", "FREIGHT_BILLING.CUSTOMER_ID=CUSTOMERS.CUSTOMER_ID", "N-1", ["REVENUE"]),
    ("J02", "FREIGHT_BILLING.COMMODITY_CODE=COMMODITIES.COMMODITY_CODE", "N-1", ["REVENUE"]),
    ("J03", "COMMODITIES.GROUP_CODE=COMMODITY_GROUPS.GROUP_CODE", "N-1", ["REVENUE"]),
    ("J04", "FREIGHT_BILLING.ORIGIN_STATION_CODE=STATIONS.STATION_CODE", "N-1", ["REVENUE"]),
    ("J05", "FREIGHT_BILLING.DESTINATION_STATION_CODE=DEST_STATION.STATION_CODE", "N-1", ["REVENUE"]),
    ("J06", "FREIGHT_BILLING.PERIOD_ID=FISCAL_PERIODS.PERIOD_ID", "N-1", ["REVENUE"]),
    ("J07", "AR_INVOICES.CUSTOMER_ID=CUSTOMERS.CUSTOMER_ID", "N-1", ["RECEIVABLES"]),
    ("J08", "AR_INVOICES.PERIOD_ID=FISCAL_PERIODS.PERIOD_ID", "N-1", ["RECEIVABLES"]),
    ("J09", "AP_INVOICES.VENDOR_ID=VENDORS.VENDOR_ID", "N-1", ["PAYABLES"]),
    ("J10", "AP_INVOICES.COST_CENTER_ID=COST_CENTERS.COST_CENTER_ID", "N-1", ["PAYABLES"]),
    ("J11", "AP_INVOICES.PERIOD_ID=FISCAL_PERIODS.PERIOD_ID", "N-1", ["PAYABLES"]),
    ("J12", "AP_INVOICES.ACCOUNT_NUMBER=GL_ACCOUNTS.ACCOUNT_NUMBER", None, ["PAYABLES"]),   # no cardinality (trap)
    ("J13", "GL_JOURNAL_LINES.ACCOUNT_NUMBER=GL_ACCOUNTS.ACCOUNT_NUMBER", "N-1", ["LEDGER"]),
    ("J14", "GL_JOURNAL_LINES.COST_CENTER_ID=COST_CENTERS.COST_CENTER_ID", "N-1", ["LEDGER"]),
    ("J15", "GL_JOURNAL_LINES.PERIOD_ID=FISCAL_PERIODS.PERIOD_ID", "N-1", ["LEDGER"]),
    ("J16", "BUDGET_LINES.ACCOUNT_NUMBER=GL_ACCOUNTS.ACCOUNT_NUMBER", "N-1", ["BUDGET"]),
    ("J17", "BUDGET_LINES.COST_CENTER_ID=COST_CENTERS.COST_CENTER_ID", "N-1", ["BUDGET"]),
    ("J18", "BUDGET_LINES.PERIOD_ID=FISCAL_PERIODS.PERIOD_ID", "N-1", ["BUDGET"]),
    ("J19", "AGG_REVENUE_MONTHLY.PERIOD_ID=FISCAL_PERIODS.PERIOD_ID", "N-1", ["REVENUE"]),
    ("J20", "DT_PERIOD_CALENDAR.PERIOD_ID=FISCAL_PERIODS.PERIOD_ID", "1-1", ["REVENUE", "LEDGER", "BUDGET"]),
    ("J21", "AR_INVOICES.DAYS_PAST_DUE BETWEEN AGING_BANDS.MIN_DAYS AND AGING_BANDS.MAX_DAYS", "N-1",
     ["RECEIVABLES"]),                                                                      # theta join (trap)
]

CONTEXTS = {
    "REVENUE": "Freight billing (revenue sub-ledger) with customer, commodity, lane and period.",
    "RECEIVABLES": "Accounts receivable invoices with customer and period.",
    "PAYABLES": "Accounts payable invoices with vendor, cost center, account and period.",
    "LEDGER": "General ledger journal lines with account, cost center and period.",
    "BUDGET": "Budget lines with account, cost center and period.",
}

# Business layer items: (id, kind, folder, name, select, where, projection, datatype, description, extra)
_ = None
ITEMS = [
    # --- Time
    ("O_FY", "Dimension", "Time", "Fiscal Year", "FISCAL_PERIODS.FISCAL_YEAR", _, "None", "Numeric",
     "Fiscal year (January to December).", {}),
    ("O_FQ", "Dimension", "Time", "Fiscal Quarter", "FISCAL_PERIODS.QUARTER_LABEL", _, "None", "String",
     "Fiscal quarter label, e.g. 2025-Q2.", {}),
    ("O_FP", "Dimension", "Time", "Fiscal Period", "FISCAL_PERIODS.PERIOD_NUMBER", _, "None", "Numeric",
     "Fiscal period (month) number, 1 to 12.", {}),
    ("O_PSTART", "Dimension", "Time", "Period Start Date", "FISCAL_PERIODS.PERIOD_START", _, "None", "Date",
     "First calendar day of the fiscal period.", {}),
    ("O_YP", "Dimension", "Time", "Year-Period", "DT_PERIOD_CALENDAR.YEAR_PERIOD", _, "None", "Numeric",
     "", {}),                                                                        # from a derived table
    ("O_CLOSED", "Dimension", "Time", "Period Closed", "DECODE(FISCAL_PERIODS.IS_CLOSED,1,'Closed','Open')", _,
     "None", "String", "Whether the fiscal period is closed for posting.", {}),
    # --- Customer
    ("O_CUST", "Dimension", "Customer", "Customer Name", "CUSTOMERS.CUSTOMER_NAME", _, "None", "String",
     "Legal name of the shipper billed for the freight.", {}),
    ("O_CUSTID", "Attribute", "Customer", "Customer Id", "CUSTOMERS.CUSTOMER_ID", _, "None", "String",
     "Customer account number.", {"parent": "O_CUST"}),
    ("O_IND", "Dimension", "Customer", "Industry", "CUSTOMERS.INDUSTRY", _, "None", "String",
     "Customer's industry.", {}),
    ("O_KEYACC", "Dimension", "Customer", "Key Account", "DECODE(CUSTOMERS.IS_KEY_ACCOUNT,1,'Yes','No')", _,
     "None", "String", "Yes when the customer is managed as a key account.", {}),
    ("O_CCTRY", "Dimension", "Customer", "Customer Country", "CUSTOMERS.HQ_COUNTRY", _, "None", "String",
     "Country of the customer's head office.", {}),
    # --- Commodity
    ("O_COMM", "Dimension", "Commodity", "Commodity", "COMMODITIES.COMMODITY_NAME", _, "None", "String",
     "Commodity shipped.", {}),
    ("O_CGRP", "Dimension", "Commodity", "Commodity Group", "COMMODITY_GROUPS.GROUP_NAME", _, "None", "String",
     "Commodity group (business unit), e.g. Grain and Fertilizers.", {}),
    ("O_SEG", "Dimension", "Commodity", "Business Segment", "COMMODITY_GROUPS.BUSINESS_SEGMENT", _, "None",
     "String", "Business segment the commodity group reports into.", {}),
    # --- Geography
    ("O_OREG", "Dimension", "Geography", "Origin Region", "STATIONS.REGION", _, "None", "String",
     "Operating region of the origin station.", {}),
    ("O_OPROV", "Dimension", "Geography", "Origin Province", "STATIONS.PROVINCE_STATE", _, "None", "String",
     "Province or state of the origin station.", {}),
    ("O_DREG", "Dimension", "Geography", "Destination Region", "DEST_STATION.REGION", _, "None", "String",
     "Operating region of the destination station.", {}),
    ("O_HAUL", "Dimension", "Geography", "Haul Length Band", "NVL(FREIGHT_BILLING.HAUL_LENGTH_BAND,'Unknown')", _,
     "None", "String", "Short, medium or long haul.", {}),
    # --- Organisation
    ("O_CC", "Dimension", "Organisation", "Cost Center", "COST_CENTERS.COST_CENTER_NAME", _, "None", "String",
     "Cost center name.", {}),
    ("O_DEPT", "Dimension", "Organisation", "Department", "COST_CENTERS.DEPARTMENT", _, "None", "String",
     "Department that owns the cost center.", {}),
    ("O_CCREG", "Dimension", "Organisation", "Cost Center Region", "COST_CENTERS.COST_CENTER_REGION", _, "None",
     "String", "Region of the cost center.", {}),
    # --- Accounts
    ("O_ACCT", "Dimension", "Accounts", "Account Name", "GL_ACCOUNTS.ACCOUNT_NAME", _, "None", "String",
     "General ledger account name.", {}),
    ("O_ACCTYPE", "Dimension", "Accounts", "Account Type", "GL_ACCOUNTS.ACCOUNT_TYPE", _, "None", "String",
     "Revenue, Expense, Asset, Liability or Equity.", {}),
    ("O_STLINE", "Dimension", "Accounts", "Statement Line", "GL_ACCOUNTS.STATEMENT_LINE", _, "None", "String",
     "Financial statement line the account rolls up to.", {}),
    # --- Vendor
    ("O_VEND", "Dimension", "Vendor", "Vendor Name", "VENDORS.VENDOR_NAME", _, "None", "String",
     "Supplier name.", {}),
    ("O_VCAT", "Dimension", "Vendor", "Vendor Category", "VENDORS.VENDOR_CATEGORY", _, "None", "String",
     "Supplier category.", {}),
    # --- Receivables / payables dimensions
    ("O_AGING", "Dimension", "Receivables", "Aging Bucket",
     "CASE WHEN AR_INVOICES.DAYS_PAST_DUE <= 0 THEN 'Current' WHEN AR_INVOICES.DAYS_PAST_DUE <= 30 THEN '1-30' "
     "WHEN AR_INVOICES.DAYS_PAST_DUE <= 60 THEN '31-60' WHEN AR_INVOICES.DAYS_PAST_DUE <= 90 THEN '61-90' "
     "ELSE '90+' END", _, "None", "String", "Days-past-due band of the open receivable.", {}),
    ("O_AGBAND", "Dimension", "Receivables", "Aging Band (lookup)", "AGING_BANDS.BAND_NAME", _, "None", "String",
     "Aging band from the AGING_BANDS lookup table.", {}),
    ("O_PAYST", "Dimension", "Receivables", "Payment Status", "AR_INVOICES.PAYMENT_STATUS", _, "None", "String",
     "Paid, partially paid or open.", {}),
    ("O_APAGING", "Dimension", "Payables", "AP Aging Bucket", "AP_INVOICES.AGING_BUCKET", _, "None", "String",
     "Days-past-due band of the open payable.", {}),
    # --- Measures: revenue (billing)
    ("M_FREV", "Measure", "Revenue", "Freight Revenue",
     "@Aggregate_Aware(SUM(AGG_REVENUE_MONTHLY.FREIGHT_REVENUES), SUM(FREIGHT_BILLING.LINE_REVENUE))", _, "Sum",
     "Numeric", "Freight revenue: line-haul + fuel surcharge + accessorial, in C$.", {"format": "#,##0"}),
    ("M_REV_BILL", "Measure", "Revenue", "Revenue", "SUM(FREIGHT_BILLING.LINEHAUL_REVENUE)", _, "Sum", "Numeric",
     "Line-haul revenue from billing.", {"format": "#,##0"}),                         # same name as M_REV_GL (trap)
    ("M_FSC", "Measure", "Revenue", "Fuel Surcharge Revenue", "SUM(FREIGHT_BILLING.FUEL_SURCHARGE_REVENUE)", _,
     "Sum", "Numeric", "Fuel surcharge billed to customers.", {"format": "#,##0"}),
    ("M_FBADJ", "Measure", "Revenue", "FB_AMT_ADJ",
     "SUM(FREIGHT_BILLING.LINEHAUL_REVENUE + NVL(FREIGHT_BILLING.ACCESSORIAL_REVENUE,0))", _, "Sum", "Numeric",
     "", {"format": "#,##0"}),                                                       # cryptic, undocumented (trap)
    ("M_CARS", "Measure", "Volume", "Carloads", "SUM(FREIGHT_BILLING.CARLOADS)", _, "Sum", "Numeric",
     "Number of carloads moved.", {"format": "#,##0"}),
    ("M_RTM", "Measure", "Volume", "Revenue Ton Miles", "SUM(FREIGHT_BILLING.REVENUE_TON_MILES)", _, "Sum",
     "Numeric", "Revenue ton-miles (RTM).", {"format": "#,##0"}),
    ("M_RPC", "Measure", "Volume", "Revenue per Carload",
     "SUM(FREIGHT_BILLING.LINE_REVENUE)/NULLIF(SUM(FREIGHT_BILLING.CARLOADS),0)", _, "Delegated", "Numeric",
     "Freight revenue divided by carloads.", {"format": "#,##0.00"}),
    ("M_ACTCUST", "Measure", "Revenue", "Active Customers", "COUNT(DISTINCT FREIGHT_BILLING.CUSTOMER_ID)", _,
     "Delegated", "Numeric", "Customers with at least one billing line.", {}),
    # --- Measures: ledger
    ("M_REV_GL", "Measure", "Ledger", "Revenue", "SUM(GL_JOURNAL_LINES.REVENUE_AMOUNT)", _, "Sum", "Numeric",
     "Total revenues posted to the general ledger.", {"format": "#,##0"}),
    ("M_OPEX", "Measure", "Ledger", "Operating Expenses", "SUM(GL_JOURNAL_LINES.EXPENSE_AMOUNT)", _, "Sum",
     "Numeric", "Operating expenses posted to the general ledger.", {"format": "#,##0"}),
    ("M_LAB", "Measure", "Ledger", "Labour Expense", "SUM(GL_JOURNAL_LINES.EXPENSE_AMOUNT)",
     "GL_JOURNAL_LINES.ACCOUNT_NUMBER BETWEEN 5000 AND 5099", "Sum", "Numeric",
     "Labour and fringe benefits (accounts 5000-5099).", {"format": "#,##0"}),
    ("M_FUELX", "Measure", "Ledger", "Fuel Expense", "SUM(GL_JOURNAL_LINES.EXPENSE_AMOUNT)",
     "GL_JOURNAL_LINES.ACCOUNT_NUMBER BETWEEN 5200 AND 5299", "Sum", "Numeric",
     "Fuel expense (accounts 5200-5299).", {"format": "#,##0"}),
    ("M_OR", "Measure", "Ledger", "Operating Ratio",
     "SUM(GL_JOURNAL_LINES.EXPENSE_AMOUNT)/NULLIF(SUM(GL_JOURNAL_LINES.REVENUE_AMOUNT),0)", _, "Delegated",
     "Numeric", "Operating expenses as a share of revenues (lower is better).", {"format": "0.0%"}),
    # --- Measures: budget
    ("M_RBUD", "Measure", "Budget", "Revenue Budget", "SUM(BUDGET_LINES.REVENUE_BUDGET)", _, "Sum", "Numeric",
     "Budgeted revenue.", {"format": "#,##0"}),
    ("M_XBUD", "Measure", "Budget", "Expense Budget", "SUM(BUDGET_LINES.EXPENSE_BUDGET)", _, "Sum", "Numeric",
     "Budgeted operating expense.", {"format": "#,##0"}),
    # --- Measures: receivables / payables
    ("M_ARINV", "Measure", "Receivables", "AR Invoiced Amount", "SUM(AR_INVOICES.INVOICE_AMOUNT)", _, "Sum",
     "Numeric", "Amount invoiced to customers.", {"format": "#,##0"}),
    ("M_AROPEN", "Measure", "Receivables", "AR Open Amount", "SUM(AR_INVOICES.OPEN_AMOUNT)", _, "Sum", "Numeric",
     "Open (unpaid) receivable balance at the snapshot date.", {"format": "#,##0"}),
    ("M_AROVD", "Measure", "Receivables", "AR Overdue Amount", "SUM(AR_INVOICES.OPEN_AMOUNT)",
     "AR_INVOICES.DAYS_PAST_DUE > 0", "Sum", "Numeric", "Open receivables past their due date.",
     {"format": "#,##0"}),
    ("M_DTP", "Measure", "Receivables", "Avg Days to Pay", "AVG(AR_INVOICES.DAYS_TO_PAY)", _, "Sum", "Numeric",
     "Average days customers take to pay.", {"format": "0.0"}),                      # AVG with Sum projection (trap)
    ("M_APINV", "Measure", "Payables", "AP Invoiced Amount", "SUM(AP_INVOICES.INVOICE_AMOUNT)", _, "Sum",
     "Numeric", "Amount invoiced by suppliers.", {"format": "#,##0"}),
    ("M_APOPEN", "Measure", "Payables", "AP Open Amount", "SUM(AP_INVOICES.OPEN_AMOUNT)", _, "Sum", "Numeric",
     "Open (unpaid) payable balance.", {"format": "#,##0"}),
    # --- Measure on a dimension table
    ("M_HC", "Measure", "Organisation", "Headcount", "SUM(COST_CENTERS.HEADCOUNT)", _, "Sum", "Numeric",
     "Full-time equivalents in the cost center.", {}),
    # --- Predefined filters
    ("F_CLOSED", "Filter", "Filters", "Closed Periods", _, "FISCAL_PERIODS.IS_CLOSED = 1", _, _,
     "Only fiscal periods closed for posting.", {}),
    ("F_OPENINV", "Filter", "Filters", "Open Invoices", _, "AR_INVOICES.OPEN_AMOUNT > 0", _, _,
     "Only receivables with an open balance.", {}),
    ("F_KEYACC", "Filter", "Filters", "Key Accounts", _, "CUSTOMERS.IS_KEY_ACCOUNT = 1", _, _,
     "Only key-account customers.", {}),
    ("F_FY", "Filter", "Filters", "Fiscal Year Prompt", _,
     "FISCAL_PERIODS.FISCAL_YEAR = @Prompt('Enter Fiscal Year:','N','Time\\Fiscal Year',mono,free,persistent)",
     _, _, "Prompts for one fiscal year.", {}),
]

PROMPTS = [
    {"id": "P_FY", "name": "Enter Fiscal Year:", "dataType": "Numeric", "multi": False, "optional": False,
     "default": [2025], "boundObject": "O_FY"},
    {"id": "P_REG", "name": "Select Region(s):", "dataType": "String", "multi": True, "optional": True,
     "default": [], "boundObject": "O_OREG"},
]

HIERARCHIES = [
    {"id": "H_FISCAL", "name": "Fiscal Calendar", "levels": ["O_FY", "O_FQ", "O_FP"]},
    {"id": "H_COMMODITY", "name": "Commodity", "levels": ["O_SEG", "O_CGRP", "O_COMM"]},
]

SECURITY = [
    {"id": "S_WEST", "type": "rowRestriction", "name": "West finance team", "table": "COST_CENTERS",
     "where": "COST_CENTERS.COST_CENTER_REGION = 'West'", "groups": ["grp_finance_west"]},
]

DERIVED_TABLES = {
    "DT_PERIOD_CALENDAR": ("SELECT PERIOD_ID, FISCAL_YEAR, FISCAL_YEAR*100 + PERIOD_NUMBER AS YEAR_PERIOD "
                           "FROM CNR_FIN.FISCAL_PERIODS", [("PERIOD_ID", "Integer"), ("FISCAL_YEAR", "Integer"),
                                                           ("YEAR_PERIOD", "Integer")]),
}
ALIASES = {"DEST_STATION": "STATIONS"}

# Unity Catalog mapping of the Oracle tables (Track S: 1:1 onto cnfinance.silver; Track C: built by the user)
UC_MAPPING = {t: f"cnfinance.silver.{t.lower()}" for t in TABLES if t not in ("AGG_REVENUE_MONTHLY", "AGING_BANDS")}
UC_MAPPING["AGG_REVENUE_MONTHLY"] = "cnfinance.gold.revenue_monthly"
UC_MAPPING["AGING_BANDS"] = None            # no counterpart (trap)

# Primary keys of the Unity Catalog tables (Track S reads them from cnfinance; the app reads information_schema)
UC_PK = {"FREIGHT_BILLING": ["REVENUE_LINE_ID"], "AR_INVOICES": ["INVOICE_ID"], "AP_INVOICES": ["AP_DOCUMENT_ID"],
         "GL_JOURNAL_LINES": ["JOURNAL_ID", "LINE_NUMBER"], "BUDGET_LINES": ["PERIOD_ID", "COST_CENTER_ID", "ACCOUNT_NUMBER"],
         "FISCAL_PERIODS": ["PERIOD_ID"], "COMMODITY_GROUPS": ["GROUP_CODE"], "COMMODITIES": ["COMMODITY_CODE"],
         "STATIONS": ["STATION_CODE"], "CUSTOMERS": ["CUSTOMER_ID"], "VENDORS": ["VENDOR_ID"],
         "COST_CENTERS": ["COST_CENTER_ID"], "GL_ACCOUNTS": ["ACCOUNT_NUMBER"], "AGG_REVENUE_MONTHLY": ["PERIOD_ID"]}


# ----------------------------------------------------------------------------------------------------------- #
# A small BO-style SQL generator: it writes what Web Intelligence would send for a query, so the "captured"
# report SQL is consistent with the Universe. One SELECT per context touched (multi-fact = several statements).
# ----------------------------------------------------------------------------------------------------------- #
_ITEM = {i[0]: i for i in ITEMS}
_MEASURE_CONTEXT = {"FREIGHT_BILLING": "REVENUE", "AGG_REVENUE_MONTHLY": "REVENUE", "AR_INVOICES": "RECEIVABLES",
                    "AP_INVOICES": "PAYABLES", "GL_JOURNAL_LINES": "LEDGER", "BUDGET_LINES": "BUDGET"}


def _tables_in(sql: str) -> list[str]:
    import re
    names = list(TABLES) + list(ALIASES) + list(DERIVED_TABLES)
    return [t for t in names if re.search(rf"\b{t}\.", sql or "")]


def _measure_select(item, use_aggregate: bool) -> str:
    sel = item[4]
    if sel.startswith("@Aggregate_Aware("):
        inner = sel[len("@Aggregate_Aware("):-1]
        first, second = [s.strip() for s in inner.split(", ", 1)]
        sel = first if use_aggregate else second
    if item[5]:                                            # measure WHERE -> BO moves it into a CASE
        agg, arg = sel.split("(", 1)
        sel = f"{agg}(CASE WHEN {item[5]} THEN {arg[:-1]} END)"
    return sel


def _join_path(tables: set[str], context: str | None) -> list[tuple[str, str]]:
    """Joins of the context that connect the tables, adding the intermediate tables BO would add
    (shortest path from the first fact table to each other table). `tables` is extended in place."""
    edges = [(jid, expr, _tables_in(expr)) for jid, expr, _card, ctxs in JOINS
             if jid != "J21" and (not context or context in ctxs)]
    facts = [t for t in sorted(tables) if t in _MEASURE_CONTEXT]
    used: dict[str, str] = {}
    if facts:
        root = facts[0]
        for target in sorted(tables - {root}):
            frontier, seen = [(root, [])], {root}
            while frontier:
                node, path = frontier.pop(0)
                if node == target:
                    for jid, expr in path:
                        used[jid] = expr
                    break
                for jid, expr, ts in edges:
                    if node in ts:
                        other = next((t for t in ts if t != node), node)
                        if other not in seen:
                            seen.add(other)
                            frontier.append((other, path + [(jid, expr)]))
        for expr in used.values():
            tables |= set(_tables_in(expr))
    else:
        for jid, expr, ts in edges:
            if set(ts) <= tables:
                used[jid] = expr
    return sorted(used.items())


def generate_sql(objects: list[str], conditions: list[str] | None = None, use_aggregate: bool = False) -> list[str]:
    """BO-style SQL for a query: implicit joins in WHERE, one statement per measure context."""
    dims = [o for o in objects if _ITEM[o][1] in ("Dimension", "Attribute")]
    meas = [o for o in objects if _ITEM[o][1] == "Measure"]
    by_ctx: dict[str, list[str]] = {}
    for m in meas:
        sel = _measure_select(_ITEM[m], use_aggregate)
        fact = next((t for t in _tables_in(sel) if t in _MEASURE_CONTEXT), None)
        ctx = _MEASURE_CONTEXT.get(fact, "ORG")
        by_ctx.setdefault(ctx, []).append(m)
    if not by_ctx:
        by_ctx = {None: []}
    statements = []
    for ctx, ms in by_ctx.items():
        cols = [_ITEM[d][4] for d in dims] + [_measure_select(_ITEM[m], use_aggregate) for m in ms]
        conds = list(conditions or [])
        tables = set()
        for c in cols + conds:
            tables |= set(_tables_in(c))
        if ctx == "REVENUE" and use_aggregate:
            tables.discard("FREIGHT_BILLING")
        joins = _join_path(tables, ctx)
        frm = []
        for t in sorted(tables):
            if t in ALIASES:
                frm.append(f"{SCHEMA}.{ALIASES[t]}  {t}")
            elif t in DERIVED_TABLES:
                frm.append(f"( {DERIVED_TABLES[t][0]} )  {t}")
            else:
                frm.append(f"{SCHEMA}.{t}  {t}")
        where = [f"( {e}  )" for _, e in joins] + [f"( {c} )" for c in conds]
        sql = "SELECT\n  " + ",\n  ".join(cols) + "\nFROM\n  " + ",\n  ".join(frm)
        if where:
            sql += "\nWHERE\n  " + "\n  AND  ".join(where)
        if ms and dims:
            sql += "\nGROUP BY\n  " + ",\n  ".join(_ITEM[d][4] for d in dims)
        statements.append(sql)
    return statements


def _cond(obj: str, op: str, values=None, prompt: str | None = None) -> dict:
    c = {"object": obj, "operator": op}
    if prompt:
        c["prompt"] = prompt
    else:
        c["values"] = values or []
    return c


def _sql_cond(c: dict, prompt_values: dict) -> str:
    sel = _ITEM[c["object"]][4]
    vals = prompt_values.get(c["prompt"], []) if c.get("prompt") else c["values"]
    lit = ", ".join(str(v) if isinstance(v, (int, float)) else f"'{v}'" for v in vals)
    if c["operator"] == "Equal":
        return f"{sel} = {lit}"
    if c["operator"] == "InList":
        return f"{sel} IN ({lit})"
    if c["operator"] == "GreaterThan":
        return f"{sel} > {lit}"
    raise ValueError(c["operator"])


# Report definitions: (id, name, folder, usage, data providers, variables, blocks, constructs)
# data provider: (dp id, name, result objects, conditions, predefined filters, use_aggregate, custom_sql)
PROMPT_VALUES = {"P_FY": [2025], "P_REG": []}

REPORTS = [
    ("R01", "Freight Revenue by Commodity Group", "Finance/Revenue", (412, 37),
     [("DP0", "Revenue", ["O_FY", "O_FP", "O_SEG", "O_CGRP", "M_FREV", "M_CARS"], [], [], False, None)],
     [("V_SEGSHORT", "Segment Code", "Dimension",
       '=If([Business Segment]="Bulk") Then "BLK" ElseIf([Business Segment]="Merchandise") Then "MER" Else "INT"')],
     [{"id": "B1", "type": "Crosstab", "axes": ["Fiscal Year", "Fiscal Period", "Commodity Group"],
       "measures": ["Freight Revenue", "Carloads"]}],
     ["C1", "C5", "C3"]),
    ("R02", "Operating Ratio Trend", "Finance/Ledger", (288, 22),
     [("DP0", "Ledger", ["O_FY", "O_FP", "M_OPEX", "M_REV_GL", "M_OR"], [], ["F_CLOSED"], False, None),
      ("DP1", "Billing", ["O_FY", "O_FP", "M_FREV"], [], ["F_CLOSED"], False, None)],
     [("V_ORPCT", "Operating Ratio %", "Measure", "=[Operating Expenses]/[Revenue]*100"),
      ("V_GLVSBILL", "GL vs Billing Gap", "Measure", "=[Revenue]-[Freight Revenue]")],
     [{"id": "B1", "type": "Table", "axes": ["Fiscal Year", "Fiscal Period"],
       "measures": ["Operating Expenses", "Revenue", "Operating Ratio", "Operating Ratio %"]}],
     ["C2", "C4", "C3", "C7"]),
    ("R03", "Revenue YTD vs Prior Year by Region", "Finance/Revenue", (356, 41),
     [("DP0", "Revenue", ["O_FY", "O_FP", "O_PSTART", "O_OREG", "M_FREV"], [], [], False, None)],
     [("V_PY", "Revenue PY", "Measure", "=RelativeValue([Freight Revenue];([Fiscal Year]);-1)"),
      ("V_YTD", "Revenue YTD", "Measure", "=RunningSum([Freight Revenue];([Fiscal Year]))"),
      ("V_YOY", "YoY %", "Measure", "=([Freight Revenue]-[Revenue PY])/[Revenue PY]")],
     [{"id": "B1", "type": "Table", "axes": ["Fiscal Year", "Fiscal Period", "Origin Region"],
       "measures": ["Freight Revenue", "Revenue PY", "Revenue YTD", "YoY %"]}],
     ["C3", "C1", "C5"]),
    ("R04", "Budget vs Actual by Cost Center", "Finance/Budget", (301, 29),
     [("DP0", "Actual", ["O_FY", "O_CC", "O_DEPT", "M_REV_GL", "M_OPEX"], [_cond("O_FY", "Equal", prompt="P_FY")],
       [], False, None),
      ("DP1", "Budget", ["O_FY", "O_CC", "O_DEPT", "M_RBUD", "M_XBUD"], [_cond("O_FY", "Equal", prompt="P_FY")],
       [], False, None)],
     [("V_RVAR", "Revenue Variance", "Measure", "=[Revenue]-[Revenue Budget]"),
      ("V_RVARP", "Revenue Variance %", "Measure", "=[Revenue Variance]/[Revenue Budget]"),
      ("V_XVAR", "Expense Variance", "Measure", "=[Operating Expenses]-[Expense Budget]")],
     [{"id": "B1", "type": "Table", "axes": ["Cost Center", "Department"],
       "measures": ["Revenue", "Revenue Budget", "Revenue Variance", "Revenue Variance %", "Operating Expenses",
                    "Expense Budget", "Expense Variance"]}],
     ["C4", "C2", "C7"]),
    ("R05", "AR Aging Buckets", "Finance/Receivables", (264, 18),
     [("DP0", "Receivables", ["O_AGING", "O_CUST", "M_AROPEN", "M_AROVD", "M_ARINV", "M_DTP"], [],
       ["F_OPENINV"], False, None)],
     [("V_OVDPCT", "Overdue %", "Measure", "=[AR Overdue Amount]/[AR Open Amount]"),
      ("V_DSO", "DSO (approx)", "Measure", "=[AR Open Amount]/[AR Invoiced Amount]*365")],
     [{"id": "B1", "type": "Crosstab", "axes": ["Aging Bucket"],
       "measures": ["AR Open Amount", "AR Overdue Amount", "Overdue %", "Avg Days to Pay"]}],
     ["C5", "C7", "C2"]),
    ("R06", "AP Aging and Vendor Spend", "Finance/Payables", (198, 12),
     [("DP0", "Payables", ["O_VEND", "O_VCAT", "O_APAGING", "M_APINV", "M_APOPEN"], [], [], False,
       "custom")],
     [],
     [{"id": "B1", "type": "Table", "axes": ["Vendor Name", "Vendor Category", "AP Aging Bucket"],
       "measures": ["AP Invoiced Amount", "AP Open Amount"], "sorts": [{"by": "AP Open Amount", "dir": "desc"}]}],
     ["C1", "C5", "C7"]),
    ("R07", "Top 20 Customers by Revenue", "Finance/Revenue", (377, 45),
     [("DP0", "Revenue", ["O_FY", "O_CUST", "O_IND", "M_FREV", "M_CARS"], [_cond("O_FY", "Equal", prompt="P_FY")],
       [], False, None)],
     [("V_RANK", "Customer Rank", "Measure", "=Rank([Freight Revenue];([Customer Name]))"),
      ("V_SHARE", "Share of Total", "Measure", "=Percentage([Freight Revenue])")],
     [{"id": "B1", "type": "Table", "axes": ["Customer Name", "Industry"],
       "measures": ["Freight Revenue", "Share of Total", "Customer Rank"],
       "ranking": {"top": 20, "by": "Freight Revenue"}}],
     ["C8", "C2", "C7"]),
    ("R08", "Fuel Surcharge Recovery", "Finance/Revenue", (152, 9),
     [("DP0", "Billing", ["O_FY", "O_FP", "M_FSC", "M_FREV", "M_FBADJ"], [], [], False, None),
      ("DP1", "Ledger", ["O_FY", "O_FP", "M_FUELX"], [], [], False, None)],
     [("V_REC", "Fuel Recovery Rate", "Measure", "=[Fuel Surcharge Revenue]/[Fuel Expense]"),
      ("V_NET", "Net Revenue", "Measure", "=[Freight Revenue]-[Fuel Surcharge Revenue]")],
     [{"id": "B1", "type": "Table", "axes": ["Fiscal Year", "Fiscal Period"],
       "measures": ["Fuel Surcharge Revenue", "Fuel Expense", "Fuel Recovery Rate", "Net Revenue", "FB_AMT_ADJ"]}],
     ["C2", "C4", "C6"]),
    ("R09", "Labour Cost per FTE by Cost Center", "Finance/Ledger", (121, 8),
     [("DP0", "Ledger", ["O_FY", "O_CC", "O_CCREG", "M_LAB"], [_cond("O_FY", "Equal", prompt="P_FY")], [], False,
       None),
      ("DP1", "Organisation", ["O_CC", "O_CCREG", "M_HC"], [], [], False, None)],
     [("V_LPF", "Labour Cost per FTE", "Measure", "=[Labour Expense]/[Headcount]")],
     [{"id": "B1", "type": "Table", "axes": ["Cost Center", "Cost Center Region"],
       "measures": ["Labour Expense", "Headcount", "Labour Cost per FTE"]}],
     ["C2", "C8", "C4"]),
    ("R10", "KPI Summary (CFO pack)", "Finance/Executive", (502, 61),
     [("DP0", "Revenue", ["O_FY", "M_FREV"], [], [], True, None),
      ("DP1", "Ledger", ["O_FY", "M_REV_GL", "M_OPEX", "M_OR"], [], [], False, None),
      ("DP2", "Monthly", ["O_FY", "O_FP", "O_OREG", "M_FREV", "M_REV_BILL", "M_FSC"], [], [], False, None)],
     [("V_NET10", "Net Revenue", "Measure", "=[Revenue]-[Fuel Surcharge Revenue]"),
      ("V_R3M", "Revenue Rolling 3M", "Measure",
       "=[Freight Revenue]+Previous([Freight Revenue])+Previous([Freight Revenue];2)"),
      ("V_MAXREG", "Best Region Revenue", "Measure", "=Max([Freight Revenue] ForEach ([Origin Region]))")],
     [{"id": "B1", "type": "Table", "axes": ["Fiscal Year"],
       "measures": ["Freight Revenue", "Revenue", "Operating Expenses", "Operating Ratio"]},
      {"id": "B2", "type": "Chart", "axes": ["Fiscal Year", "Fiscal Period"],
       "measures": ["Freight Revenue", "Revenue Rolling 3M", "Net Revenue"]}],
     ["C1", "C2", "C3", "C6", "C8"]),
]

CUSTOM_SQL_R06 = ("SELECT\n  VENDORS.VENDOR_NAME,\n  VENDORS.VENDOR_CATEGORY,\n  AP_INVOICES.AGING_BUCKET,\n"
                  "  SUM(AP_INVOICES.INVOICE_AMOUNT),\n  SUM(AP_INVOICES.OPEN_AMOUNT)\nFROM\n"
                  "  CNR_FIN.AP_INVOICES  AP_INVOICES,\n  CNR_FIN.VENDORS  VENDORS\nWHERE\n"
                  "  ( AP_INVOICES.VENDOR_ID=VENDORS.VENDOR_ID  )\n  AND  ( VENDORS.VENDOR_CATEGORY <> 'Intercompany' )\n"
                  "GROUP BY\n  VENDORS.VENDOR_NAME,\n  VENDORS.VENDOR_CATEGORY,\n  AP_INVOICES.AGING_BUCKET")


def _universe() -> dict:
    tables = []
    for t, cols in TABLES.items():
        tables.append({"id": f"T_{t}", "name": t, "type": "table", "qualifier": SCHEMA,
                       "columns": [{"name": c, "dataType": ORACLE_TYPES[k]} for c, k in cols]})
    for a, base in ALIASES.items():
        tables.append({"id": f"T_{a}", "name": a, "type": "alias", "aliasOf": base, "qualifier": SCHEMA,
                       "columns": [{"name": c, "dataType": ORACLE_TYPES[k]} for c, k in TABLES[base]]})
    for d, (sql, cols) in DERIVED_TABLES.items():
        tables.append({"id": f"T_{d}", "name": d, "type": "derived", "sql": sql,
                       "columns": [{"name": c, "dataType": ORACLE_TYPES[k]} for c, k in cols]})
    items = []
    for iid, kind, folder, name, sel, where, proj, dtype, desc, extra in ITEMS:
        it = {"id": iid, "type": kind, "path": f"CNR Finance\\{folder}", "name": name, "description": desc,
              "state": "Active", "hidden": False}
        if sel is not None:
            it["select"] = sel
        if where is not None:
            it["where"] = where
        if proj is not None:
            it["projection"] = proj
        if dtype is not None:
            it["dataType"] = dtype
        if extra.get("parent"):
            it["parent"] = extra["parent"]
        if extra.get("format"):
            it["format"] = {"numberFormat": extra["format"]}
        if kind in ("Dimension", "Attribute"):
            it["lov"] = {"name": f"LOV {name}", "automaticRefresh": True}
        items.append(it)
    return {
        "id": UNIVERSE_ID, "cuid": "AZcnrFin0000Universe01", "name": "CNR Finance", "type": "unx",
        "description": "Finance universe for revenue, ledger, budget, receivables and payables reporting.",
        "connection": {"name": "CNR_FIN_ORACLE", "dialect": "oracle", "qualifier": SCHEMA},
        "dataFoundation": {
            "tables": tables,
            "joins": [{"id": j, "expression": e, "cardinality": c, "outer": False, "shortcut": False}
                      for j, e, c, _ in JOINS],
            "contexts": [{"id": f"C_{n}", "name": n, "description": d,
                          "joins": [j for j, _e, _c, cs in JOINS if n in cs]} for n, d in CONTEXTS.items()],
        },
        "businessLayer": {"items": items, "parameters": PROMPTS, "hierarchies": HIERARCHIES,
                          "securityProfiles": SECURITY},
    }


def _report(rid, name, folder, usage, dps, variables, blocks, constructs) -> dict:
    data_providers = []
    for dp_id, dp_name, objs, conds, filters, use_agg, custom in dps:
        sql_conds = [_sql_cond(c, PROMPT_VALUES) for c in conds] + [_ITEM[f][5] for f in filters]
        statements = [CUSTOM_SQL_R06] if custom else generate_sql(objs, sql_conds, use_agg)
        cond_tree = [dict(c) for c in conds] + [{"predefinedFilter": f} for f in filters]
        data_providers.append({
            "id": dp_id, "name": dp_name, "dataSourceId": UNIVERSE_ID, "dataSourceType": "unx",
            "querySpecification": {"resultObjects": objs,
                                   "filters": {"operator": "And", "conditions": cond_tree} if cond_tree else None},
            "customSql": bool(custom), "promptValues": {k: v for k, v in PROMPT_VALUES.items()
                                                        if any(c.get("prompt") == k for c in conds)},
            "queryPlan": {"statements": statements},
        })
    return {
        "id": rid, "cuid": f"AZcnr{rid}000000000000", "name": f"{rid} {name}", "folder": f"Public Folders/{folder}",
        "usage": {"refreshes_90d": usage[0], "users_90d": usage[1]},
        "constructs_intended": constructs,
        "dataProviders": data_providers,
        "variables": [{"id": v, "name": n, "qualification": q, "formula": f} for v, n, q, f in variables],
        "reports": [{"id": "RPT1", "name": "Report 1", "blocks": blocks}],
    }


def build() -> dict:
    """The full extract bundle as a dict: manifest, universe, reports, table mapping."""
    universe = _universe()
    reports = [_report(*r) for r in REPORTS]
    manifest = {
        "bundle_version": "0.1", "source_system": {"type": "sap_bo", "version": "4.3 SP4 (synthetic)"},
        "extracted_at": "2026-11-27T00:00:00Z", "extractor": "synthetic fixture (modules/sapbo/fixture.py)",
        "universe": universe["id"], "reports": [r["id"] for r in reports],
        "extraction_methods": {"universe.businessLayer": "rest:/biprws/sl/v1 (simulated)",
                               "universe.dataFoundation": "sdk:semantic-layer-java (simulated)",
                               "reports": "rest:/biprws/raylight/v1 (simulated)"},
        "counts": {"tables": len(universe["dataFoundation"]["tables"]),
                   "joins": len(universe["dataFoundation"]["joins"]),
                   "contexts": len(universe["dataFoundation"]["contexts"]),
                   "items": len(universe["businessLayer"]["items"]), "reports": len(reports)},
    }
    mapping = {"tables": []}
    for t in list(TABLES) + list(ALIASES):
        base = ALIASES.get(t, t)
        uc = UC_MAPPING.get(base)
        row = {"bo_table": t, "uc_table": uc}
        if uc:   # what the app reads from information_schema (types, primary key)
            row["uc_columns"] = {c.lower(): k for c, k in TABLES[base]}
            row["uc_primary_key"] = [c.lower() for c in UC_PK.get(base, [])]
        mapping["tables"].append(row)
    return {"manifest": manifest, "universe": universe, "reports": reports, "mapping": mapping}


def write(directory: str | pathlib.Path) -> pathlib.Path:
    """Write the bundle as files: manifest.json, universe.json, mapping.json, reports/<id>.json."""
    d = pathlib.Path(directory)
    (d / "reports").mkdir(parents=True, exist_ok=True)
    b = build()
    for key in ("manifest", "universe", "mapping"):
        (d / f"{key}.json").write_text(json.dumps(b[key], indent=2), encoding="utf-8")
    for r in b["reports"]:
        (d / "reports" / f"{r['id']}.json").write_text(json.dumps(r, indent=2), encoding="utf-8")
    return d


if __name__ == "__main__":  # python -m modules.sapbo.fixture samples/sapbo/cnr_finance
    import sys
    print(write(sys.argv[1] if len(sys.argv) > 1 else "samples/sapbo/cnr_finance"))
