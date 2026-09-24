"""Verification tool for Kriya AI <-> CallMedex Render live connection.

Tests both directions against live Render endpoints using the shared secrets.
Run with: python scripts/verify_callmedex_render_connection.py
"""

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
import uuid

import os
from dotenv import load_dotenv

load_dotenv()

# Production endpoints on Render
CALLMEDEX_BASE_URL = os.getenv("CALLMEDEX_BASE_URL", "https://callmedex-backend.onrender.com")
KRIYA_BASE_URL = os.getenv("MEDASSIST_URL", "https://medassist-ai-docker.onrender.com")

# The shared credentials (read from environment or .env)
SHARED_BEARER_TOKEN = (
    os.getenv("CALLMEDEX_BEARER_TOKEN")
    or os.getenv("MEDIASSIST_INBOUND_BEARER_TOKEN")
    or os.getenv("CALLMEDEX_OUTBOUND_BEARER_TOKEN")
    or ""
)
SHARED_HMAC_SECRET = (
    os.getenv("CALLMEDEX_HMAC_SIGNATURE_SECRET")
    or os.getenv("CALLMEDEX_HMAC_SECRET")
    or os.getenv("MEDIASSIST_HMAC_SECRET")
    or ""
)


def test_callmedex_health():
    print("\n" + "=" * 65)
    print("1. PROBING CALLMEDEX HEALTH & CONFIGURATION")
    print("   URL: " + f"{CALLMEDEX_BASE_URL}/api/health")
    print("=" * 65)
    url = f"{CALLMEDEX_BASE_URL}/api/health"
    req = urllib.request.Request(url, headers={"User-Agent": "KriyaAI-Diagnostic/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            print(f"[*] CallMedex HTTP Status: {resp.status}")
            print(f"[*] Environment:          {data.get('environment')}")
            print(f"[*] mediassist_configured: {data.get('mediassist_configured')}")
            if not data.get("mediassist_configured"):
                print("    [!] WARNING: 'mediassist_configured' is FALSE on CallMedex!")
                print("        This indicates MEDIASSIST_BEARER_TOKEN is not set on CallMedex Render.")
            else:
                print("    [+] MEDIASSIST_BEARER_TOKEN is recognized by CallMedex.")
    except Exception as e:
        print(f"[-] FAILED to reach CallMedex health endpoint: {e}")


def test_kriya_health():
    print("\n" + "=" * 65)
    print("2. PROBING KRIYA AI HEALTH & INTEGRATION ENDPOINT")
    print("   URL: " + f"{KRIYA_BASE_URL}/internal/integrations/callmedex/health")
    print("=" * 65)
    url = f"{KRIYA_BASE_URL}/internal/integrations/callmedex/health"
    req = urllib.request.Request(url, headers={"User-Agent": "KriyaAI-Diagnostic/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            print(f"[*] Kriya HTTP Status:     {resp.status}")
            print(f"[*] Status:               {data.get('status')}")
            print(f"[*] Integration API Active:{data.get('integration_api')}")
            print(f"[*] Queue Status:          {data.get('queue_status')}")
    except Exception as e:
        print(f"[-] FAILED to reach Kriya internal health endpoint: {e}")


def test_kriya_to_callmedex_auth():
    print("\n" + "=" * 65)
    print("3. TESTING KRIYA -> CALLMEDEX SIGNED AUTH (INBOUND TO CALLMEDEX)")
    print("   Endpoint: GET /api/v1/integrations/mediassist/patients/lookup")
    print("=" * 65)
    if not SHARED_BEARER_TOKEN or not SHARED_HMAC_SECRET:
        print("[-] SKIPPED: Set CALLMEDEX_BEARER_TOKEN and CALLMEDEX_HMAC_SIGNATURE_SECRET in .env to run signed tests.")
        return

    path = "/api/v1/integrations/mediassist/patients/lookup"
    query = "phone=%2B919876543210"
    url = f"{CALLMEDEX_BASE_URL}{path}?{query}"

    ts = str(int(time.time()))
    # CallMedex signature: sha256 of timestamp + "." + query + body
    message = f"{ts}.".encode() + query.encode() + b""
    sig = "sha256=" + hmac.new(SHARED_HMAC_SECRET.encode(), message, hashlib.sha256).hexdigest()

    headers = {
        "Authorization": f"Bearer {SHARED_BEARER_TOKEN}",
        "X-Timestamp": ts,
        "X-Signature": sig,
        "User-Agent": "KriyaAI-Diagnostic/1.0",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[+] SUCCESS: HTTP {resp.status}")
            print(f"[*] Response Body: {resp.read().decode()}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if e.code == 404 and "patient_not_found" in body:
            print(f"[+] AUTH PASSED! HTTP 404 (Expected for test phone number): {body}")
            print("    The shared Bearer token and HMAC signature secret are 100% WORKING on CallMedex.")
        elif e.code == 401:
            print(f"[-] AUTH REJECTED: HTTP 401 - {body}")
            if "Inbound MediAssist auth is not configured" in body:
                print("    [!] REASON: MEDIASSIST_INBOUND_BEARER_TOKEN is missing or empty on CallMedex Render!")
            elif "Inbound MediAssist HMAC secret is not configured" in body:
                print("    [!] REASON: MEDIASSIST_HMAC_SECRET is missing or empty on CallMedex Render!")
            elif "Invalid bearer token" in body:
                print("    [!] REASON: Bearer token mismatch between Kriya and CallMedex!")
            elif "Signature verification failed" in body:
                print("    [!] REASON: HMAC secret mismatch or formula drift between Kriya and CallMedex!")
        else:
            print(f"[-] HTTP {e.code}: {body}")
    except Exception as e:
        print(f"[-] Transport failure: {e}")


def test_callmedex_to_kriya_auth():
    print("\n" + "=" * 65)
    print("4. TESTING CALLMEDEX -> KRIYA SIGNED AUTH (INBOUND TO KRIYA)")
    print("   Endpoint: POST /internal/integrations/callmedex/process-report")
    print("=" * 65)
    if not SHARED_BEARER_TOKEN or not SHARED_HMAC_SECRET:
        print("[-] SKIPPED: Set CALLMEDEX_BEARER_TOKEN and CALLMEDEX_HMAC_SIGNATURE_SECRET in .env to run signed tests.")
        return

    url = f"{KRIYA_BASE_URL}/internal/integrations/callmedex/process-report"
    unique_nonce = str(uuid.uuid4())
    body = json.dumps({"test_nonce": unique_nonce}).encode("utf-8")
    ts = str(int(time.time()))

    # Kriya expects bare hex HMAC of raw body with CALLMEDEX_HMAC_SIGNATURE_SECRET
    sig = hmac.new(SHARED_HMAC_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()

    headers = {
        "Authorization": f"Bearer {SHARED_BEARER_TOKEN}",
        "X-Timestamp": ts,
        "X-Signature-256": sig,
        "Content-Type": "application/json",
        "User-Agent": "CallMedex-Diagnostic/1.0",
    }
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[+] SUCCESS: HTTP {resp.status} - {resp.read().decode()}")
    except urllib.error.HTTPError as e:
        body_resp = e.read().decode()
        if e.code == 422:
            print(f"[+] AUTH PASSED! HTTP 422 Unprocessable Entity (Expected schema validation):")
            print(f"    Both CALLMEDEX_BEARER_TOKEN and CALLMEDEX_HMAC_SIGNATURE_SECRET authenticated successfully!")
        elif e.code == 401:
            print(f"[-] AUTH REJECTED: HTTP 401 - {body_resp}")
            if "Invalid or missing authorization bearer token" in body_resp:
                print("    [!] REASON: CALLMEDEX_BEARER_TOKEN mismatch on Kriya Render!")
            elif "Invalid HMAC-SHA256 signature header" in body_resp:
                print("    [!] REASON: CALLMEDEX_HMAC_SIGNATURE_SECRET mismatch on Kriya Render.")
                print("        (Kriya currently only recognizes CALLMEDEX_HMAC_SIGNATURE_SECRET in production)")
        else:
            print(f"[-] HTTP {e.code}: {body_resp}")
    except Exception as e:
        print(f"[-] Transport failure: {e}")


if __name__ == "__main__":
    test_callmedex_health()
    test_kriya_health()
    test_kriya_to_callmedex_auth()
    test_callmedex_to_kriya_auth()
    print("\n" + "=" * 65 + "\n")
