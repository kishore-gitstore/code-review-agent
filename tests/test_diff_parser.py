"""
tests/test_diff_parser.py — Unit tests for the diff parser utility
"""

from app.utils.diff_parser import parse_diff, get_changed_files, get_path_map

SAMPLE_DIFF = """diff --git a/app/main.py b/app/main.py
index abc..def 100644
--- a/app/main.py
+++ b/app/main.py
@@ -1,5 +1,7 @@
 import os
+import sys
 
 def main():
-    print("hello")
+    print("hello world")
+    return 0
diff --git a/README.md b/README.md
index 111..222 100644
--- a/README.md
+++ b/README.md
@@ -1,3 +1,4 @@
 # My Project
+Updated readme
"""


def test_parse_diff_file_count():
    files = parse_diff(SAMPLE_DIFF)
    assert len(files) == 2


def test_parse_diff_paths():
    files = parse_diff(SAMPLE_DIFF)
    paths = [f.path for f in files]
    assert "app/main.py" in paths
    assert "README.md" in paths


def test_parse_diff_added_lines():
    files = parse_diff(SAMPLE_DIFF)
    main_file = next(f for f in files if f.path == "app/main.py")
    assert main_file.added_lines == 3
    assert main_file.removed_lines == 1


def test_parse_diff_truncation():
    big_diff = "\n".join(
        [f"diff --git a/big.py b/big.py"] +
        [f"+line {i}" for i in range(500)]
    )
    files = parse_diff(big_diff, max_lines=100)
    assert len(files[0].hunks) <= 101   # 100 lines + truncation notice


def test_get_changed_files():
    files = get_changed_files(SAMPLE_DIFF)
    assert "app/main.py" in files
    assert "README.md" in files


def test_get_path_map_same_path():
    path_map = get_path_map(SAMPLE_DIFF)
    assert path_map["app/main.py"] == "app/main.py"
    assert path_map["README.md"] == "README.md"


RENAME_DIFF = """diff --git a/old_name.py b/new_name.py
similarity index 100%
rename from old_name.py
rename to new_name.py
"""


def test_get_path_map_rename():
    path_map = get_path_map(RENAME_DIFF)
    assert path_map["new_name.py"] == "old_name.py"
