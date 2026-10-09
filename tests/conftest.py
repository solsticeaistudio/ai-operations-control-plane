import os, tempfile
os.environ["AIOPS_STORAGE"]="sqlite"
os.environ["AIOPS_SQLITE_PATH"]=tempfile.mktemp(prefix="aiops_test_",suffix=".db")
os.environ["AIOPS_DEMO_MODE"]="true"
