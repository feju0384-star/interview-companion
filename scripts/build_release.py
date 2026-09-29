"""Build public source and introduction-page archives from an explicit source manifest.

No third-party dependencies, credentials, local settings or runtime files are read.
Outputs stay in .local/releases; this script never uploads or publishes anything.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "release-files.txt"
OUTPUT = ROOT / ".local" / "releases"


def source_files():
    files = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        relative = PurePosixPath(name)
        if (relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name
                or any(part in {".venv", ".local", "__pycache__", ".git"} for part in relative.parts)):
            raise ValueError(f"Unsafe release path: {name}")
        path = ROOT / name
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(ROOT) or path.is_symlink() or not path.is_file():
            raise ValueError(f"Release file must be a regular project file: {name}")
        if name in files:
            raise ValueError(f"Duplicate release path: {name}")
        files.append(name)
    if not files or "LICENSE" not in files:
        raise ValueError("Release manifest must include LICENSE")
    return files


def build():
    files = source_files()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    source = OUTPUT / "tingda-source.zip"
    with ZipFile(source, "w", ZIP_DEFLATED) as archive:
        for name in files:
            archive.write(ROOT / name, "tingda/" + name)

    # This independent directory can be hosted as static files. It has no API or
    # desktop control code. Make the free source directly downloadable in this copy.
    site = OUTPUT / "site"
    site.mkdir(exist_ok=True)
    page = (ROOT / "web" / "services.html").read_text(encoding="utf-8")
    page, replacements = re.subn(
        r'<a id="source-link"\s[^>]*>[^<]*</a>',
        '<a id="source-link" class="button secondary" href="./tingda-source.zip" download>免费下载 MIT 源码 ↓</a>',
        page,
    )
    if replacements != 1:
        raise ValueError("Source download link changed; update the site export mapping")
    page = page.replace("源码已在 GitHub 开放。下载 ZIP 后解压，双击“安装并启动听答.cmd”。", "下载源码后解压，双击“安装并启动听答.cmd”。模型账户与调用费用自备。")
    (site / "index.html").write_text(page, encoding="utf-8")
    # Remove the retired booking script from earlier local exports too.
    (site / "services.js").unlink(missing_ok=True)
    site_files = ["index.html", "services.css", "icon.svg", "LICENSE.txt", "tingda-source.zip"]
    for name in ("services.css", "icon.svg"):
        (site / name).write_bytes((ROOT / "web" / name).read_bytes())
    (site / "LICENSE.txt").write_bytes((ROOT / "LICENSE").read_bytes())
    (site / "tingda-source.zip").write_bytes(source.read_bytes())
    public_zip = OUTPUT / "tingda-service-site.zip"
    with ZipFile(public_zip, "w", ZIP_DEFLATED) as archive:
        for name in site_files:
            archive.write(site / name, name)
    hashes = []
    for artifact in (source, public_zip):
        hashes.append(f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}")
    (OUTPUT / "SHA256SUMS.txt").write_text("\n".join(hashes) + "\n", encoding="utf-8")
    (OUTPUT / "README.txt").write_text(
        "听答发布材料（仅本地生成，未上传）\n\n"
        "tingda-source.zip：MIT 源码，解压后在 tingda 目录运行安装脚本。\n"
        "tingda-service-site.zip：公开介绍页，可将全部内容解压到静态托管目录；包含免费源码下载。\n"
        "site/index.html：同一展示站的本地预览。\n"
        "SHA256SUMS.txt：校验值。\n\n"
        "请只托管展示站，不要把 8765 本机控制服务暴露到公网。\n"
        "有问题可以通过介绍页中的邮箱联系作者。\n"
        "已有包是本次构建的快照；修改代码或文案后须重新构建。\n",
        encoding="utf-8",
    )
    print(f"Source: {source} ({len(files)} files)")
    print(f"Static site: {public_zip}")
    print(f"Preview: {site / 'index.html'}")


if __name__ == "__main__":
    build()
