#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os
sys.path.insert(0, os.getcwd())
sys.path.insert(0, './data/knowledge/qidian-knowledge')

# 直接导入脚本文件
import importlib.util
spec = importlib.util.spec_from_file_location("validate", "./data/knowledge/qidian-knowledge/scripts/validate.py")
validate_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validate_module)
Validator = validate_module.Validator

def test_basic():
    v = Validator()
    result = v.validate("test candidate")
    print("Result:", result)
    assert "verdict" in result
    assert result["verdict"] in ["注意", "人工复核", "信息不足"]

if __name__ == "__main__":
    test_basic()