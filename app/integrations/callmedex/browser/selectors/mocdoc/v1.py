"""MocDoc DOM Selector Provider (Version v1.0.0 Grounded in Authoritative Reference Screenshots)."""

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

    # Step 1-4: Login Page Selectors
    @property
    def login_username_input(self) -> str:
        return "input[placeholder='Email or User Id'], input#username, input[name='username']"

    @property
    def login_password_input(self) -> str:
        return "input[placeholder='Password'], input#password, input[name='password']"

    @property
    def login_submit_button(self) -> str:
        return "button:has-text('Login'), input[type='submit'][value='Login'], button.btn-login"

    # Step 5-7: Dashboard Navigation & Barcode Search Selectors
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

    # Step 8-9: Report View & Download Modal Selectors
    @property
    def report_download_trigger_icon(self) -> str:
        return "a[href*='labresult/download'], button[title*='Download Result']"

    @property
    def download_modal_checkbox_mantoux(self) -> str:
        return "input[type='checkbox'][name*='test'], input[type='checkbox']:checked"

    @property
    def download_modal_select_button(self) -> str:
        return "button:has-text('Select'), input[value='Select']"

    @property
    def download_modal_close_button(self) -> str:
        return "button:has-text('Close')"

    @property
    def download_pdf_button(self) -> str:
        return "button:has-text('Select'), a[href*='download']"

    # Step 10: Profile Dropdown & Logout Selectors
    @property
    def profile_dropdown_menu(self) -> str:
        return "a.profile-icon, div.user-profile, a:has-text('Welcome')"

    @property
    def logout_button(self) -> str:
        return "a:has-text('Sign out'), a[href*='logout']"
