import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("WA_APP_SECRET", "testsecret"); os.environ.setdefault("WA_VERIFY_TOKEN", "vtoken")
os.environ.setdefault("IDENTITY_SECRET", "idsecret"); os.environ["DRY_RUN"] = "1"; os.environ["FLORENCE"] = "0"     # suite exercises the Tesseract path; Florence tests opt in (test_florence.py)
