"""Translates opaque Playwright browser-launch failures into an actionable
message, so on-call staff reading a WhatsApp admin alert don't have to
parse a raw Playwright stack trace to know what to do next."""


def friendly_browser_launch_error(exc: Exception) -> str:
    text = str(exc)
    if "Executable doesn't exist" in text or "playwright install" in text.lower():
        return (
            "Chromium browser is not installed on this server — the deploy did not "
            "run 'playwright install --with-deps chromium' at build time. Check that "
            "the Render service builds from the Dockerfile (see render.yaml, env: docker), "
            "then redeploy."
        )
    return text
