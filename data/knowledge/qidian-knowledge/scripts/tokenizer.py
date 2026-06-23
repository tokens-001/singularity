#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中文混合分词器
=============
- 中文连续段: 2/3/4-gram
- ASCII段: 按空格/符号切分 + snake_case/CamelCase 拆分
- 编号识别: P001, D008, 007, v2.0 等保持整体
- 同义词扩展: 从 data/synonyms.csv 加载
"""

import re
import csv
from pathlib import Path
from collections import defaultdict


class ChineseTokenizer:
    """中文 ngram + ASCII 混合分词"""

    def __init__(self, synonyms_path: str = None):
        self.synonym_map = defaultdict(set)  # 归一目标 → {同义词}
        self.reverse_synonym = {}  # 任意词 → 归一目标
        if synonyms_path:
            self._load_synonyms(synonyms_path)

    def _load_synonyms(self, path: str):
        """加载同义词表: 归一目标,同义词1|同义词2|..."""
        filepath = Path(path)
        if not filepath.exists():
            return
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row or row[0].startswith("#"):
                    continue
                if len(row) >= 2:
                    target = row[0].strip()
                    synonyms = [s.strip() for s in row[1].split("|")]
                    for syn in synonyms:
                        self.synonym_map[target].add(syn)
                        self.reverse_synonym[syn] = target
                    self.synonym_map[target].add(target)
                    self.reverse_synonym[target] = target

    def tokenize(self, text: str) -> list:
        """主分词入口"""
        if not text:
            return []

        tokens = []
        # 分块: 中文连续段 / ASCII段 / 标点
        segments = re.split(r"([一-鿿㐀-䶿]+)", str(text))

        for seg in segments:
            if not seg:
                continue
            if re.match(r"[一-鿿㐀-䶿]", seg):
                tokens.extend(self._chinese_ngram(seg))
            else:
                tokens.extend(self._ascii_tokens(seg))

        # 去重保留顺序
        seen = set()
        result = []
        for t in tokens:
            t_lower = t.lower()
            if t_lower not in seen and len(t_lower) >= 2:
                seen.add(t_lower)
                result.append(t_lower)
                # 同义词扩展
                if t_lower in self.reverse_synonym:
                    canonical = self.reverse_synonym[t_lower]
                    if canonical not in seen:
                        seen.add(canonical)
                        result.append(canonical)
                # 检查归一目标本身
                if t_lower in self.synonym_map:
                    for syn in self.synonym_map[t_lower]:
                        if syn not in seen:
                            seen.add(syn)
                            result.append(syn)

        return result

    def _chinese_ngram(self, text: str) -> list:
        """中文 2/3/4-gram"""
        tokens = []
        # 去掉标点只留汉字
        pure = re.sub(r"[^一-鿿㐀-䶿]", "", text)
        n = len(pure)
        for i in range(n):
            for k in [2, 3, 4]:  # 2-gram 优先，3/4-gram 补语义
                if i + k <= n:
                    tokens.append(pure[i : i + k])
        return tokens

    # 编号/版本号/标识符正则
    ID_PATTERN = re.compile(
        r"^(?:[A-Z]\d{2,4}(?:[-_]\d+)?|"  # P001, D008, P001-1
        r"\d{3,4}|"  # 007, 1024
        r"v\d+\.\d+(?:\.\d+)?|"  # v2.1.175
        r"[A-Z][a-z]+(?:[A-Z][a-z]+)+)$"  # CamelCase
    )

    def _ascii_tokens(self, text: str) -> list:
        """ASCII 段分词: split + snake_case/CamelCase 拆分 + 编号识别"""
        tokens = []

        # 先按非字母数字切
        parts = re.split(r"[^\w]", text)
        parts = [p for p in parts if p]

        for part in parts:
            # 编号/标识符整体保留
            if self.ID_PATTERN.match(part):
                tokens.append(part)
                continue

            # snake_case 拆分
            if "_" in part and not part.startswith("_"):
                for sub in part.split("_"):
                    if sub and len(sub) >= 2:
                        tokens.append(sub)
                tokens.append(part)  # 完整 token 也保留
                continue

            # CamelCase 拆分
            camel_parts = re.findall(r"[A-Z][a-z]+|[A-Z]+(?=[A-Z]|$)|[a-z]+", part)
            if len(camel_parts) > 1:
                tokens.extend([p.lower() for p in camel_parts if len(p) >= 2])
                tokens.append(part.lower())  # 完整 token 保留

            # 短词也保留（ASCII 2 字符即可）
            if len(part) >= 2:
                tokens.append(part.lower())

        return tokens


def tokenize_for_index(text: str, tokenizer: ChineseTokenizer = None) -> str:
    """索引用: 返回空格分隔的 token 串（BM25 fit 用）"""
    if tokenizer is None:
        tokenizer = ChineseTokenizer()
    return " ".join(tokenizer.tokenize(text))
