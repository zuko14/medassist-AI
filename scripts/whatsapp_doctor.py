"""Pinpoint why Meta is rejecting WhatsApp Cloud API calls.

Meta answers a bad token/phone-id/template with HTTP 500 +
{"code":1,"type":"OAuthException","message":"An unknown error has occurred."}
on *every* endpoint, which reads like a transient outage. This walks the
credentials one hop at a time so the real failure names itself.

    python -m scripts.whatsapp_doctor                  # global settings
    python -m scripts.whatsapp_doctor --clinic <uuid>  # that clinic's config
    python -m scripts.whatsapp_doctor --send +9199...  # also send a live text
"""

import argparse
import asyncio
import sys

import httpx

from app.config import settings

BASE = f"https://graph.facebook.com/{settings.whatsapp_api_version}"


def _verdict(r: httpx.Response) -> str:
    err = {}
    try:
        err = r.json().get("error", {}) or {}
    except Exception:
        pass
    if not err:
        return "OK"
    return (
        f"code={err.get('code')} type={err.get('type')} "
        f"msg={err.get('message')!r} fbtrace_id={err.get('fbtrace_id')}"
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clinic", help="clinic id; uses its config.meta_* credentials")
    ap.add_argument("--waba", help="WhatsApp Business Account id, for the template check")
    ap.add_argument("--send", help="phone in E.164 to send a live test text to")
    args = ap.parse_args()

    cfg: dict = {}
    token, phone_id = settings.whatsapp_token, settings.whatsapp_phone_number_id
    if args.clinic:
        from app.services.tenant import get_clinic_by_id

        cfg = (await get_clinic_by_id(args.clinic) or {}).get("config", {}) or {}
        token = cfg.get("meta_access_token") or token
        phone_id = cfg.get("meta_phone_number_id") or phone_id

    if not token:
        print("FAIL  WHATSAPP_TOKEN is empty")
        return 1
    if not phone_id:
        # The token check below needs no phone id, and it is the step that most
        # often explains a blanket OAuthException — so keep going, just skip
        # everything downstream of it.
        print("WARN  WHATSAPP_PHONE_NUMBER_ID is empty; token check only.")

    print(f"api      {BASE}")
    print(f"phone_id {phone_id}")
    print(f"token    ...{token[-6:]} (len {len(token)})")

    auth = {"Authorization": f"Bearer {token}"}
    failed = False
    async with httpx.AsyncClient(timeout=20.0, headers=auth) as c:
        # 1. Is the token itself alive, and whose is it?
        r = await c.get(f"{BASE}/me")
        print(f"\n[1] token       HTTP {r.status_code}  {_verdict(r)}")
        if r.status_code != 200:
            print("    -> token expired/revoked. Regenerate the System User token "
                  "(Business Settings > System Users > Generate token, permissions "
                  "whatsapp_business_messaging + whatsapp_business_management) and "
                  "update WHATSAPP_TOKEN on Render.")
            return 1
        print(f"    identity: {r.json()}")
        if not phone_id:
            print()
            print("INCONCLUSIVE - set WHATSAPP_PHONE_NUMBER_ID (or pass --clinic) "
                  "to check the number, template and media upload.")
            return 1

        # 2. Does that token actually own this phone number id?
        r = await c.get(
            f"{BASE}/{phone_id}",
            params={"fields": "display_phone_number,verified_name,quality_rating,"
                              "code_verification_status,throughput"},
        )
        print(f"[2] phone_id    HTTP {r.status_code}  {_verdict(r)}")
        if r.status_code != 200:
            print("    -> the token cannot access this phone number id: they belong "
                  "to different apps/WABAs, or the number was migrated. Fix whichever "
                  "of the two is stale.")
            return 1
        print(f"    number: {r.json()}")

        # 2b. The decisive check: Meta's own reason for refusing.
        r = await c.get(f"{BASE}/{phone_id}", params={"fields": "health_status,name_status,account_mode,status,messaging_limit_tier"})
        h = (r.json() or {}).get("health_status", {})
        print(f"[2b] health     can_send_message={h.get('can_send_message')}")
        if h.get("can_send_message") == "BLOCKED":
            failed = True
        for e in h.get("entities", []):
            for err in (e.get("errors") or []):
                if err.get("error_code") in (138024, 138025):
                    continue  # SIP/calling noise, never blocks messaging
                if err.get("error_code") == 141010:
                    print(f"    {e.get('entity_type')} [{e.get('can_send_message')}] "
                          f"Tier-250 (Unverified business tier — active for up to 250 convos/day)")
                    continue
                if e.get("can_send_message") == "BLOCKED":
                    failed = True
                print(f"    {e.get('entity_type')} {e.get('id')} [{e.get('can_send_message')}] "
                      f"{err.get('error_code')}: {err.get('error_description')}")
                print(f"        fix: {err.get('possible_solution')}")

        # 3. Is the report template approved, and in which language?
        from app.services.lab_reports import template_name_for

        name = template_name_for({"config": cfg})
        waba_id = args.waba or cfg.get("meta_waba_id")
        if waba_id and name:
            r = await c.get(
                f"{BASE}/{waba_id}/message_templates",
                params={"name": name, "fields": "name,status,language,category,components"},
            )
            print(f"[3] template    HTTP {r.status_code}  {_verdict(r)}")
            templates = r.json().get("data") or []
            for t in templates:
                hdr = next((x for x in t.get("components", []) if x.get("type") == "HEADER"), {})
                print(f"    {t['name']} lang={t['language']} status={t['status']} "
                      f"category={t.get('category')} header={hdr.get('format')}")
                # A non-APPROVED template rejects every send. This must fail the
                # doctor: reporting PASS here is what let a PENDING template look
                # like a healthy account while no report reached a patient.
                if t["status"] != "APPROVED":
                    failed = True
                    print(f"    -> template status is {t['status']}, so Meta rejects every "
                          f"send of it. PENDING usually clears within minutes; if it is "
                          f"stuck for hours, resubmit it in WhatsApp Manager > Message "
                          f"templates, or escalate to Meta support.")
            if not templates:
                failed = True
                print(f"    -> no template named {name!r} on WABA {waba_id}.")
        else:
            print("[3] template    SKIPPED - pass --waba <id> (or set config.meta_waba_id) "
                  "to check that the report template is APPROVED. Unchecked, an "
                  "unapproved template silently fails every delivery.")

        # 4. Media upload is the step that was failing — prove it end to end.
        pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
        r = await c.post(
            f"{BASE}/{phone_id}/media",
            data={"messaging_product": "whatsapp"},
            files={"file": ("doctor.pdf", pdf, "application/pdf")},
        )
        print(f"[4] upload      HTTP {r.status_code}  {_verdict(r)}")
        if r.status_code == 200:
            print(f"    media_id {r.json().get('id')} — uploads are healthy; a failing "
                  f"report upload is then about that specific file (empty/HTML/oversized).")
        else:
            failed = True
            print("    -> /media rejects a known-good PDF, so the problem is the "
                  "credentials or app, not the report file.")

        # 5. Optional live send.
        if args.send:
            r = await c.post(
                f"{BASE}/{phone_id}/messages",
                json={"messaging_product": "whatsapp", "to": args.send,
                      "type": "text", "text": {"body": "whatsapp_doctor test"}},
            )
            print(f"[5] send text   HTTP {r.status_code}  {_verdict(r)}")
            failed = failed or r.status_code != 200

    print("\n" + ("FAIL - see arrows above" if failed else "PASS - Meta accepts these credentials and account is LIVE!"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
