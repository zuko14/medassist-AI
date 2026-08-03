"""MocDoc DOM Selector Provider (Active Version Alias)."""

from app.integrations.callmedex.browser.selectors.mocdoc.v1 import MocDocSelectorProviderV1


class MocDocSelectorProvider(MocDocSelectorProviderV1):
    """Active selector provider pointing to version v1.0.0."""
    pass
