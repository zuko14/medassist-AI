"""Test storage upload directly."""
from app.database import supabase
from uuid import uuid4

# Try uploading a tiny test PDF bytes
test_bytes = b"%PDF-1.4 test content"
test_path = f"test/{uuid4()}_test.pdf"

print(f"\n=== TESTING STORAGE UPLOAD ===")
print(f"Path: {test_path}")
try:
    result = supabase.storage.from_("lab-reports").upload(
        test_path, test_bytes, {"content-type": "application/pdf"}
    )
    print(f"Upload result: {result}")
    print("SUCCESS - storage upload works!")

    # Clean up
    supabase.storage.from_("lab-reports").remove([test_path])
    print("Cleaned up test file.")
except Exception as e:
    print(f"UPLOAD FAILED: {type(e).__name__}: {e}")
