"""
test_pst.py — run directly to diagnose PST parsing
Usage: python test_pst.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

PST_PATH = r"Y:\Ai POC Projects\email-intelligence\emails\backup.pst"

print("=== Step 1: Check file exists ===")
print(f"Exists: {os.path.exists(PST_PATH)}")
print(f"Size:   {os.path.getsize(PST_PATH):,} bytes")

print("\n=== Step 2: Check pywin32 ===")
try:
    import win32com.client
    print("pywin32 import: OK")
except ImportError as e:
    print(f"pywin32 import FAILED: {e}")
    sys.exit(1)

print("\n=== Step 3: Launch Outlook COM ===")
try:
    outlook = win32com.client.Dispatch("Outlook.Application")
    print("Outlook.Application: OK")
except Exception as e:
    print(f"Outlook.Application FAILED: {e}")
    sys.exit(1)

print("\n=== Step 4: Get MAPI namespace ===")
try:
    namespace = outlook.GetNamespace("MAPI")
    print("GetNamespace(MAPI): OK")
except Exception as e:
    print(f"GetNamespace FAILED: {e}")
    sys.exit(1)

print("\n=== Step 5: Add PST store ===")
try:
    namespace.AddStoreEx(PST_PATH, 3)
    print("AddStoreEx: OK")
except Exception as e:
    print(f"AddStoreEx FAILED: {e}")
    sys.exit(1)

print("\n=== Step 6: List stores ===")
try:
    for i, store in enumerate(namespace.Stores):
        try:
            print(f"  Store {i}: {store.DisplayName} | {store.FilePath}")
        except Exception as e:
            print(f"  Store {i}: error reading — {e}")
except Exception as e:
    print(f"Stores enumeration FAILED: {e}")

print("\n=== Step 7: Count emails in first folder ===")
try:
    from pathlib import Path
    abs_path = str(Path(PST_PATH).resolve())
    pst_store = None
    for store in namespace.Stores:
        try:
            if store.FilePath and Path(store.FilePath).resolve() == Path(abs_path).resolve():
                pst_store = store
                break
        except Exception:
            continue

    if pst_store is None:
        print("Could not match PST store by path — trying first store")
        pst_store = namespace.Stores.Item(1)

    root = pst_store.GetRootFolder()
    print(f"Root folder: {root.Name}")
    print(f"Subfolders:  {root.Folders.Count}")
    total = 0
    for i in range(1, root.Folders.Count + 1):
        folder = root.Folders.Item(i)
        count = folder.Items.Count
        print(f"  {folder.Name}: {count} items")
        total += count
    print(f"\nTotal items (top level folders only): {total}")
except Exception as e:
    print(f"FAILED: {e}")

print("\nDone.")
