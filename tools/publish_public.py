"""把当前私有库 HEAD 发布成公开镜像（**单提交**、脱敏快照，不携带私有历史）。

背景（2026-11-08 用户定的流程）：日常只在私有库 `origin` 上提交/推送；确认没问题后，
再把一份**脱敏快照**发到公开库。公开库永远只有一条快照提交，历史不会泄露本机信息。

流程：
1. 要求工作区干净（有未提交改动就中止）；
2. ``git archive HEAD`` 导出到临时目录（天然不含 .git / .env / models / data）；
3. 发布前自检（失败即中止）：不得有 ``.env*``；文档里不得出现本机用户名、微信路径、
   私人邮箱/QQ；
4. 在临时目录建单提交仓库，作者固定为中性身份；
5. 强推 ``main`` 到公开镜像库（每次覆盖）。

用法::

    python tools/publish_public.py             # 真正发布
    python tools/publish_public.py --dry-run   # 只做 1–4 步，不推送

公开库：https://github.com/yyyydssj/computer-voice-assistant-public
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

PUBLIC_URL = "https://github.com/yyyydssj/computer-voice-assistant-public.git"
NEUTRAL_NAME = "yyyydssj"
NEUTRAL_EMAIL = "yyyydssj@users.noreply.github.com"
DOC_SUFFIXES = {".md", ".json", ".txt", ".yml"}

#: 公开前必须为零的个人/本机痕迹（在文档里扫；代码里的 ``G:\\study`` 是功能白名单，保留）
PERSONAL_PATTERNS = {
    "Windows 用户名": re.compile(r"29190"),
    "微信 id": re.compile(r"wxid_[A-Za-z0-9_]{5,}"),
    "私人邮箱/QQ": re.compile(r"2919067522|@qq\.com"),
    "C 盘用户目录（具体用户）": re.compile(r"C:[\\/]Users[\\/](?!<)"),
    "study 盘绝对路径": re.compile(r"G:[\\/]study"),
}


def run(*args: str, cwd: Path | str | None = None, check: bool = True) -> str:
    result = subprocess.run(
        list(args), cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and result.returncode != 0:
        raise SystemExit(f"命令失败：{' '.join(args)}\n{result.stderr[:600]}")
    return result.stdout


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    root = Path(run("git", "rev-parse", "--show-toplevel").strip())
    if run("git", "status", "--porcelain").strip():
        raise SystemExit("工作区不干净：先提交/推送私有库，再发布公开镜像")
    head = run("git", "rev-parse", "--short", "HEAD").strip()

    with tempfile.TemporaryDirectory(prefix="cva-public-") as workdir:
        work = Path(workdir)
        tar_path = work / "snapshot.tar"
        run("git", "archive", "--format=tar", "HEAD", "-o", str(tar_path))
        with tarfile.open(tar_path) as archive:
            try:
                archive.extractall(work / "tree", filter="data")
            except TypeError:  # Python < 3.12 没有 filter 参数
                archive.extractall(work / "tree")
        tree = work / "tree"
        tar_path.unlink()

        # ---- 发布前自检（fail closed）----
        env_files = [p for p in tree.rglob(".env*") if p.is_file()]
        if env_files:
            raise SystemExit(f"快照里出现 .env：{env_files[:3]}")
        findings: list[str] = []
        for path in tree.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in DOC_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for label, pattern in PERSONAL_PATTERNS.items():
                if pattern.search(text):
                    findings.append(f"{label}: {path.relative_to(tree)}")
        if findings:
            raise SystemExit("快照自检失败（先脱敏再发布）：\n" + "\n".join(findings[:20]))

        file_count = sum(1 for p in tree.rglob("*") if p.is_file())
        print(f"快照：{file_count} 个文件（私有库 HEAD={head}）")
        if dry_run:
            print("--dry-run：自检通过，未推送")
            return 0

        # ---- 单提交仓库 + 强推 ----
        run("git", "init", "-q", cwd=tree)
        run("git", "add", "-A", cwd=tree)
        run(
            "git", "-c", f"user.name={NEUTRAL_NAME}", "-c", f"user.email={NEUTRAL_EMAIL}",
            "commit", "-q", "-m", f"公开镜像快照：电脑语音助手（脱敏后的代码与文档，私有库 {head}）",
            cwd=tree,
        )
        run("git", "branch", "-M", "main", cwd=tree)
        run("git", "push", "-f", PUBLIC_URL, "main", cwd=tree)
        public_head = run("git", "rev-parse", "--short", "HEAD", cwd=tree).strip()
        print(f"已发布：{PUBLIC_URL.replace('.git', '')}  commit={public_head}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
