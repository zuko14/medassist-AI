"""
MocDoc DOM Selectors — Single Source of Truth.

If MocDoc changes their UI, update ONLY this file.
Every selector was extracted from live MocDoc HTML source code (July 2026).
"""

# ═══════════════════════════════════════════════════════════════════════════════
# LOGIN PAGE — https://mocdoc.com/user/loginform
# Source: inspected HTML, confirmed via screenshot
# ═══════════════════════════════════════════════════════════════════════════════
LOGIN_URL_PATH = "/user/loginform"
USERNAME_FIELD = "#secret-user-name"       # jQuery: $("#secret-user-name").focus()
PASSWORD_FIELD = "#pwd"                    # jQuery: $("#pwd").val()
LOGIN_BUTTON = "#patient-login"            # jQuery: $("#patient-login").click(...)
# NOTE: The page JS handles AES-256 encryption of the password automatically.
# Playwright just fills the fields and clicks the button. The page's own event
# handler on #patient-login encrypts and submits.

# Post-login redirect target
DASHBOARD_URL_PATH = "/frontoffice/home"

# ═══════════════════════════════════════════════════════════════════════════════
# MAINTENANCE MODAL — appears occasionally after login
# ═══════════════════════════════════════════════════════════════════════════════
MAINTENANCE_MODAL_OK = "button:has-text('OK')"
MAINTENANCE_MODAL_UNDERSTAND = "button:has-text('I Understand')"

# ═══════════════════════════════════════════════════════════════════════════════
# LAB REPORTS PAGE
# URL pattern: /investigation/listbydate/order/{clinic_slug}?cat=Laboratory
# ═══════════════════════════════════════════════════════════════════════════════
LAB_REPORTS_URL_TEMPLATE = "/investigation/listbydate/order/{clinic_slug}?cat=Laboratory"

# Tab navigation — "Pending Print" shows approved, ready-to-download reports
PENDING_PRINT_TAB = "a:has-text('Pending Print'), li:has-text('Pending Print') a"

# Page title text that confirms we're on the right tab
PENDING_PRINT_HEADING = "Pending Print Order"

# Entries-per-page dropdown (DataTables)
SHOW_ENTRIES_SELECT = "select[name$='_length'], .dataTables_length select"

# ═══════════════════════════════════════════════════════════════════════════════
# REPORT TABLE (DataTables-powered)
# ═══════════════════════════════════════════════════════════════════════════════
REPORT_TABLE_BODY = "#example tbody, table.dataTable tbody"
REPORT_ROWS = "#example tbody tr, table.dataTable tbody tr"

# Table header cells — used to locate the "Provider" column by name rather
# than by a hardcoded index, so an added/reordered MocDoc column cannot make
# the connector read the wrong cell.
REPORT_TABLE = "#example, table.dataTable"
REPORT_HEADER_CELLS = "thead th"
PROVIDER_COLUMN_HEADER = "provider"
# Column order as of Sep 2026: Patient | Referred By | Provider | Type | Date |
# Status | actions. Used only if the header lookup finds nothing.
PROVIDER_COLUMN_FALLBACK_INDEX = 2

# The "View" button on each row (green button, right-most column)
VIEW_BUTTON = "button:has-text('View'), a:has-text('View'), .btn:has-text('View')"
HIDE_BUTTON = "button:has-text('Hide'), a:has-text('Hide'), .btn:has-text('Hide')"

# Status badge
STATUS_APPROVED = "Approved"

# ═══════════════════════════════════════════════════════════════════════════════
# EXPANDED ROW — appears after clicking "View" on a patient row
# ═══════════════════════════════════════════════════════════════════════════════
# The "Download Result" icon in the expanded test row's toolbar
# Visible in screenshot as a download-arrow icon with tooltip "Download Result"
DOWNLOAD_RESULT_ICON = (
    "a[title='Download Result'], "
    "button[title='Download Result'], "
    "i[title='Download Result'], "
    "[title='Download Result']"
)

# ═══════════════════════════════════════════════════════════════════════════════
# DOWNLOAD MODAL — appears after clicking "Download Result"
# ═══════════════════════════════════════════════════════════════════════════════
# "Select" button (orange) — triggers the download
DOWNLOAD_SELECT_BUTTON = "#dwnld-sel"

# "Close" button — dismisses the modal
DOWNLOAD_CLOSE_BUTTON = "button:has-text('Close')"

# Status texts shown during and after download
DOWNLOAD_IN_PROGRESS_TEXT = "Download in progress"
DOWNLOAD_COMPLETED_TEXT = "Download Completed"

# Error text shown when bill is unpaid — download is blocked by MocDoc
DOWNLOAD_FAILED_BILL_PENDING = "Download Failed"
BILL_PENDING_KEYWORDS = [
    "Patient Due Pending",
    "Account Balance Exceed",
    "Download Failed",
]

# ═══════════════════════════════════════════════════════════════════════════════
# PAGINATION (DataTables)
# ═══════════════════════════════════════════════════════════════════════════════
NEXT_PAGE_BUTTON = ".dataTables_paginate .next:not(.disabled), .paginate_button.next:not(.disabled)"
PAGINATION_INFO = ".dataTables_info"
EMPTY_TABLE_TEXT = "No data available"
