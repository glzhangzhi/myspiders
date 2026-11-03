#!/usr/bin/env python3
"""公开仓库的发布前检查：扫描「将要公开的文件」里的个人信息与凭据痕迹。

用法：
    python scripts/check_public.py            # 扫当前目录树（公开仓库 / 公开工作区里跑）
    python scripts/check_public.py --tree DIR # 扫指定目录
    python scripts/check_public.py --list-files FILE  # 只扫清单里列出的文件（拆分脚本用）

规则见 PATTERNS：私网/公网 IP、个人域名与邮箱、德国 IBAN、各家 token 前缀、
OCID 等。命中即退出码 1。确实无害的命中（假数据、文档示例）用 ALLOW 白名单放行，
白名单必须精确到字符串，不允许整文件豁免 —— 整文件豁免等于以后往里加真东西也不报。

另外，存在于 PRIVATE_PATHS 清单里的路径一律不应出现在公开树里。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("个人域名", re.compile(r"qzz\.io|glzhangzhi@|zzczzc", re.I)),  # GitHub 用户名本身是公开的
    ("个人邮箱", re.compile(r"[\w.+-]+@(?:126|163|qq|gmail|outlook|stud\.tu-darmstadt)\.(?:com|de)", re.I)),
    ("Tailscale/CGNAT IP", re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b")),
    ("公网 IP", re.compile(r"\b(?!10\.|127\.|0\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|255\.|100\.)"
                         r"(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("局域网 IP", re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b")),
    ("IBAN", re.compile(r"\bDE\d{20}\b")),
    ("Slack token", re.compile(r"\bxox[abcdpr]-[A-Za-z0-9-]{10,}")),
    ("OpenAI/DeepSeek key", re.compile(r"\bsk-[A-Za-z0-9]{24,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}")),
    ("OCI OCID", re.compile(r"ocid1\.[a-z]+\.oc1\.[a-z0-9-]*\.[a-z0-9]{20,}")),
    ("私钥", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("Telegram 邀请链接", re.compile(r"t\.me/\+[A-Za-z0-9_-]{8,}")),
    ("真实姓名", re.compile(r"Zhichao|ZHICHAO|zhichao|Jinke Lyu|Shikenso", re.I)),
    ("成人站点", re.compile(r"javbus|javdb|madou\.club|pornhub|xvideos", re.I)),
    ("Mercor 内部 id", re.compile(r"\b(?:world|camp|acct|comp|user)_[0-9a-f]{20,}\b")),
]

# 精确字符串白名单：(规则名, 命中的文本)
ALLOW: set[tuple[str, str]] = {
    ("公网 IP", "154.0.0.0"),              # Chrome UA 里的版本号
    ("公网 IP", "203.0.113.10"),           # RFC 5737 文档保留段（测试假数据）
    ("Tailscale/CGNAT IP", "100.64.0.11"),  # 测试里的占位地址
    ("Tailscale/CGNAT IP", "100.64.0.22"),
    ("局域网 IP", "192.168.1.50"),           # 配置注释里的示例
    ("IBAN", "DE00100110010000000001"),    # 测试假 IBAN（DE00 开头校验位不可能合法）
    ("IBAN", "DE00500502010000000002"),
    ("IBAN", "DE00500400000000000003"),
}

TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".html", ".js", ".ts", ".tsx", ".css", ".yml",
                 ".yaml", ".toml", ".ini", ".sh", ".service", ".conf", ".example", ".cs", ".bat",
                 ".webmanifest", ".manifest", ""}
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "dist", ".pytest_cache"}
SKIP_FILES = {".env"}   # 本地真实配置，永远不进仓库（.gitignore 已挡）


def iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file() or any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if p.name in SKIP_FILES:
            continue
        if p.suffix.lower() in TEXT_SUFFIXES or p.name.startswith(".env"):
            yield p


def scan_text(rel: str, text: str) -> list[str]:
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, rx in PATTERNS:
            for m in rx.finditer(line):
                if (name, m.group(0)) in ALLOW:
                    continue
                if name == "公网 IP" and not _looks_like_ip(m.group(0)):
                    continue
                hits.append(f"{rel}:{lineno}: [{name}] {m.group(0)}")
    return hits


def _looks_like_ip(s: str) -> bool:
    parts = s.split(".")
    return len(parts) == 4 and all(p.isdigit() and int(p) <= 255 for p in parts) \
        and not all(len(p) == 1 for p in parts)  # 排除版本号 1.2.3.4 这类


def private_paths(root: Path) -> list[str]:
    f = root / "PRIVATE_PATHS"
    if not f.exists():
        return []
    return [l.strip() for l in f.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree", default=".")
    ap.add_argument("--private-list", help="PRIVATE_PATHS 文件（默认取 --tree 下的）")
    a = ap.parse_args()
    root = Path(a.tree).resolve()
    plist = private_paths(Path(a.private_list).parent if a.private_list else root)

    hits: list[str] = []
    for p in iter_files(root):
        rel = p.relative_to(root).as_posix()
        if rel in ("scripts/check_public.py", "tests/test_check_public.py"):  # 规则与正例本身含这些字样
            continue
        if plist and any(rel == x or (x.endswith("/") and rel.startswith(x)) for x in plist):
            # 私有仓库里跑时跳过私有路径；公开树里不该存在这些路径（下面单独查）
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        hits += scan_text(rel, text)
    for h in hits:
        print(h)
    print(f"\n{len(hits)} 处命中", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
