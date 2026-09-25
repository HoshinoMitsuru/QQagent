#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""敏感信息入库检查（配合 pre-commit hook 使用）。

词表：tools/sensitive-words.local.txt（被 .gitignore 排除，绝不入库），
每行一个敏感片段（子串匹配），# 开头为注释。

用法：
    python tools/check_sensitive.py          # 扫描暂存区新增行（pre-commit 默认）
    python tools/check_sensitive.py --all    # 扫描全部跟踪文本文件（定期全量体检）

命中 → 打印 文件:行号（敏感词掩码显示）→ 退出码 1 阻止提交。
词表缺失时只提示不阻塞（避免把词表本身逼回仓库）。

一次性安装 hook：
    git config core.hooksPath .githooks
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORDLIST = os.path.join(HERE, 'sensitive-words.local.txt')
SKIP_EXT = ('.png', '.ico', '.jpg', '.jpeg', '.gif', '.pyc', '.exe', '.mp3', '.webp')


def git(*args):
    return subprocess.run(['git'] + list(args), cwd=ROOT, capture_output=True,
                          text=True, encoding='utf-8', errors='replace').stdout


def load_words():
    if not os.path.exists(WORDLIST):
        print(f'[check-sensitive] 未找到本地词表 {WORDLIST}')
        print('  请复制 tools/sensitive-words.example.txt 为 sensitive-words.local.txt，')
        print('  填入真实敏感片段（昵称/群名/真名/QQ号等）。词表本身已被 .gitignore 排除。')
        return None
    return [ln.strip() for ln in open(WORDLIST, encoding='utf-8')
            if ln.strip() and not ln.strip().startswith('#')]


def check_line(line, words):
    hits = [w for w in words if w in line]
    if not hits:
        return None
    masked = line
    for w in hits:
        masked = masked.replace(w, '***')
    return masked.strip()[:160]


def scan_staged(words):
    rows = []
    cur, new_ln = None, 0
    for line in git('diff', '--cached', '-U0', '--no-color').splitlines():
        if line.startswith('+++ b/'):
            cur, new_ln = line[6:], 0
        elif line.startswith('@@'):
            m = re.search(r'\+(\d+)', line)
            new_ln = int(m.group(1)) if m else 0
        elif line.startswith('+') and cur and not line.startswith('+++'):
            hit = check_line(line[1:], words)
            if hit:
                rows.append((cur, new_ln, hit))
            new_ln += 1
    return rows


def scan_all(words):
    rows = []
    for f in git('ls-files').splitlines():
        if f.endswith(SKIP_EXT):
            continue
        p = os.path.join(ROOT, f)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding='utf-8', errors='ignore') as fh:
                for i, line in enumerate(fh, 1):
                    hit = check_line(line, words)
                    if hit:
                        rows.append((f, i, hit))
        except OSError:
            pass
    return rows


def main():
    words = load_words()
    if not words:
        return 0
    rows = scan_all(words) if '--all' in sys.argv else scan_staged(words)
    if rows:
        print(f'[check-sensitive] 命中 {len(rows)} 处疑似敏感信息，提交被阻止：')
        for f, ln, masked in rows:
            print(f'  {f}:{ln}  {masked}')
        print('处理：真实值移入 gitignored 本地文件（config.json / secrets.local.json），'
              '代码与文档用占位符；误报则从本地词表移除该词。')
        return 1
    print('[check-sensitive] 通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
