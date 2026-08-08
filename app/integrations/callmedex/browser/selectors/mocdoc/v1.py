"""MocDoc DOM Selector Provider (Version v1.0.0 Grounded in Authoritative Reference Screenshots)."""

from typing import List

from app.integrations.callmedex.browser.selectors.base import BaseSelectorProvider


class MocDocSelectorProviderV1(BaseSelectorProvider):
    """MocDoc EMR UI Selector Definitions (v1.0.0).

    Derived strictly from authoritative UI screenshots in
    app/integrations/callmedex/browser/screenshots/reference/

    Screen Dimensions & Browser Metadata:
    - Display Resolution: 1920x1080 (100% Zoom, 100% Windows DPI Scaling)
    - Base URL: https://mocdoc.com/user/loginform
    """

    @property
    def version(self) -> str:
        return "v1.0.0"

    # ═══════════════════════════════════════════════════════════════════════
    # LOGIN PAGE
    # ═══════════════════════════════════════════════════════════════════════
    @property
    def login_username_input(self) -> str:
        return "input[placeholder='Email or User Id'], input#username, input[name='username']"

    @property
    def login_password_input(self) -> str:
        return "input[placeholder='Password'], input#password, input[name='password']"

    @property
    def login_submit_button(self) -> str:
        return "button:has-text('Login'), input[type='submit'][value='Login'], button.btn-login"

    # ═══════════════════════════════════════════════════════════════════════
    # DASHBOARD NAVIGATION & BARCODE SEARCH
    # ═══════════════════════════════════════════════════════════════════════
    @property
    def nav_investigation_tab(self) -> str:
        return "a:has-text('Investigation'), a[href*='investigation']"

    @property
    def nav_lab_order_link(self) -> str:
        return "a:has-text('Lab order'), a[href*='cat=Laboratory']"

    @property
    def search_barcode_input(self) -> str:
        return "input[type='search'], input[placeholder*='Search'], input#search_barcode"

    @property
    def search_submit_button(self) -> str:
        return "button:has-text('Search'), input[value='Search']"

    @property
    def patient_view_button(self) -> str:
        return "button:has-text('View'), a.btn-view-patient"

    # ═══════════════════════════════════════════════════════════════════════
    # LAB REPORTS PAGE — PENDING PRINT TAB
    # URL: /investigation/listbydate/order/{clinic_slug}?cat=Laboratory
    # ═══════════════════════════════════════════════════════════════════════
    @property
    def lab_reports_url_template(self) -> str:
        """URL path template for the lab reports page. Format with clinic_slug."""
        return "/investigation/listbydate/order/{clinic_slug}?cat=Laboratory"

    @property
    def pending_print_tab(self) -> str:
        """Selector for the 'Pending Print' tab link."""
        return "a:has-text('Pending Print'), li:has-text('Pending Print') a"

    @property
    def pending_print_tab_id(self) -> str:
        """Element ID for the Pending Print tab (used for JS click fallback)."""
        return "pendingprint"

    @property
    def dashboard_url_path(self) -> str:
        return "/frontoffice/home"

    # ═══════════════════════════════════════════════════════════════════════
    # REPORT TABLE (DataTables-powered)
    # ═══════════════════════════════════════════════════════════════════════
    @property
    def report_table_body(self) -> str:
        return "#example tbody, table.dataTable tbody"

    @property
    def report_rows(self) -> str:
        return "#example tbody tr, table.dataTable tbody tr"

    @property
    def show_entries_select(self) -> str:
        """Entries-per-page dropdown (DataTables)."""
        return "select[name$='_length'], .dataTables_length select"

    @property
    def empty_table_text(self) -> str:
        return "No data available"

    @property
    def view_button(self) -> str:
        """The 'View' button on each row (green button, right-most column)."""
        return "button:has-text('View'), a:has-text('View'), .btn:has-text('View')"

    @property
    def hide_button(self) -> str:
        """The 'Hide' button to collapse an expanded row."""
        return "button:has-text('Hide'), a:has-text('Hide'), .btn:has-text('Hide')"

    # ═══════════════════════════════════════════════════════════════════════
    # EXPANDED ROW — appears after clicking "View"
    # ═══════════════════════════════════════════════════════════════════════
    @property
    def report_download_trigger_icon(self) -> str:
        """Download Result icon in the expanded test row."""
        return (
            "a[title='Download Result'], "
            "button[title='Download Result'], "
            "i[title='Download Result'], "
            "[title='Download Result']"
        )

    @property
    def download_result_link_class(self) -> str:
        """CSS class on the <a> tag for Download Result (used for scoped clicks)."""
        return "a.downloadresult"

    # ═══════════════════════════════════════════════════════════════════════
    # DOWNLOAD MODAL
    # ═══════════════════════════════════════════════════════════════════════
    @property
    def download_modal_checkbox_mantoux(self) -> str:
        return "input[type='checkbox'][name*='test'], input[type='checkbox']:checked"

    @property
    def download_modal_select_button(self) -> str:
        return "button:has-text('Select'), input[value='Select']"

    @property
    def download_select_button_id(self) -> str:
        """The '#dwnld-sel' button ID in the download modal."""
        return "#dwnld-sel"

    @property
    def download_modal_close_button(self) -> str:
        return "button:has-text('Close')"

    @property
    def download_pdf_button(self) -> str:
        return "button:has-text('Select'), a[href*='download']"

    @property
    def download_in_progress_text(self) -> str:
        return "Download in progress"

    @property
    def download_completed_text(self) -> str:
        return "Download Completed"

    @property
    def download_failed_text(self) -> str:
        return "Download Failed"

    @property
    def bill_pending_keywords(self) -> List[str]:
        """Keywords that indicate download is blocked by unpaid bill."""
        return [
            "Patient Due Pending",
            "Account Balance Exceed",
            "Download Failed",
        ]

    # ═══════════════════════════════════════════════════════════════════════
    # PROFILE & LOGOUT
    # ═══════════════════════════════════════════════════════════════════════
    @property
    def profile_dropdown_menu(self) -> str:
        return "a.profile-icon, div.user-profile, a:has-text('Welcome')"

    @property
    def logout_button(self) -> str:
        return "a:has-text('Sign out'), a[href*='logout']"
