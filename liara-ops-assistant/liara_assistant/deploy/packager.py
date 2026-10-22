"""Project analysis and source packaging (mirrors the official Liara CLI)."""
from __future__ import annotations

import fnmatch
import json
import os
import re
import tarfile
import tempfile
import zipfile
from pathlib import Path

MAX_SOURCE_SIZE = 256 * 1024 * 1024  # 256 MiB, same limit as the CLI

PLATFORMS = ["node", "laravel", "php", "python", "django", "flask", "dotnet", "react",
             "angular", "vue", "static", "docker", "next", "go"]

# Ports fixed by the platform (CLI getPort) and defaults for the rest (CLI getDefaultPort)
FIXED_PORTS = {"static": 80, "python": 80, "react": 80, "vue": 80, "angular": 80, "laravel": 80,
               "django": 80, "flask": 80, "php": 80, "next": 3000}
DEFAULT_PORTS = {"dotnet": 80, "go": 8080}

ALWAYS_IGNORE = [".git", ".git/**", "node_modules", "node_modules/**", "__pycache__", "**/__pycache__",
                 ".venv", ".venv/**", ".env", ".env.*", "*.pyc", ".DS_Store"]


class PackagingError(Exception):
    pass


def detect_platform(path: Path) -> str:
    has = lambda name: (path / name).exists()  # noqa: E731
    pkg = _read_json(path / "package.json") if has("package.json") else None
    if has("Dockerfile") and pkg is None and not has("composer.json"):
        return "docker"
    if list(path.glob("*.csproj")):
        return "dotnet"
    if has("composer.json"):
        req = (_read_json(path / "composer.json") or {}).get("require", {})
        return "laravel" if "laravel/framework" in req else "php"
    if has("requirements.txt") or has("Pipfile"):
        txt = ((path / "requirements.txt").read_text(errors="ignore") if has("requirements.txt")
               else (path / "Pipfile").read_text(errors="ignore")).lower()
        if "django" in txt:
            return "django"
        if "flask" in txt:
            return "flask"
        return "python"
    if has("go.mod"):
        return "go"
    if pkg is not None:
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        if "next" in deps:
            return "next"
        if "@angular/core" in deps:
            return "angular"
        if "vue" in deps or "@vue/cli-service" in deps:
            return "vue"
        if "react-scripts" in deps or ("react" in deps and "vite" in deps and not pkg.get("main")):
            return "react"
        return "node"
    if has("Dockerfile"):
        return "docker"
    if has("index.html"):
        return "static"
    raise PackagingError("نوع پروژه تشخیص داده نشد (package.json / requirements.txt / composer.json / "
                         "go.mod / Dockerfile / index.html پیدا نشد).")


def detect_port(path: Path, platform: str) -> tuple[int, str]:
    """Return (port, how it was found)."""
    cfg = read_liara_json(path)
    if cfg.get("port"):
        return int(cfg["port"]), "liara.json"
    if platform in FIXED_PORTS:
        return FIXED_PORTS[platform], "پیش‌فرض پلتفرم"
    if platform in ("node", "docker"):
        for f in ["index.js", "server.js", "app.js", "src/index.js", "src/server.js", "main.js", "Dockerfile"]:
            fp = path / f
            if fp.exists():
                txt = fp.read_text(errors="ignore")
                m = re.search(r"PORT\s*\|\|\s*(\d{2,5})", txt) or re.search(r"listen\(\s*(\d{2,5})", txt) \
                    or re.search(r"EXPOSE\s+(\d{2,5})", txt)
                if m:
                    return int(m.group(1)), f"از کد ({f})"
    return DEFAULT_PORTS.get(platform, 3000), "پیش‌فرض"


def read_liara_json(path: Path) -> dict:
    return _read_json(path / "liara.json") or {}


def analyze(path: str | Path) -> dict:
    p = Path(path).expanduser().resolve()
    if not p.exists() or not p.is_dir():
        raise PackagingError(f"مسیر پروژه پیدا نشد: {p}")
    platform = read_liara_json(p).get("platform") or detect_platform(p)
    port, port_source = detect_port(p, platform)
    files = list(_iter_files(p, _ignore_patterns(p)))
    size = sum((p / f).stat().st_size for f in files)
    cfg = read_liara_json(p)
    pkg = _read_json(p / "package.json") or {}
    deps = list((pkg.get("dependencies") or {}).keys())
    db_hints = [d for d in deps if d in ("pg", "mysql2", "mongoose", "mongodb", "prisma", "sequelize", "redis",
                                         "ioredis", "typeorm")]
    if (p / "requirements.txt").exists():
        req = (p / "requirements.txt").read_text(errors="ignore").lower()
        db_hints += [d for d in ("psycopg", "mysqlclient", "pymongo", "redis", "sqlalchemy") if d in req]
    env_example = sorted(_env_keys(p))
    return {
        "path": str(p),
        "platform": platform,
        "port": port,
        "port_source": port_source,
        "files": len(files),
        "size_mb": round(size / 1024 / 1024, 2),
        "has_liara_json": bool(cfg),
        "liara_json": cfg,
        "health_check": cfg.get("healthCheck"),
        "database_hints": sorted(set(db_hints)),
        "env_keys_expected": env_example,
        "has_dotenv_file": (p / ".env").exists(),
    }


def make_archive(path: str | Path) -> Path:
    p = Path(path).expanduser().resolve()
    patterns = _ignore_patterns(p)
    files = list(_iter_files(p, patterns))
    if not files:
        raise PackagingError("همه فایل‌ها ignore شده‌اند؛ چیزی برای آپلود نیست.")
    fd, out = tempfile.mkstemp(suffix=".tar.gz", prefix="liara-src-")
    os.close(fd)
    with tarfile.open(out, "w:gz") as tar:
        for rel in files:
            tar.add(p / rel, arcname=rel)
    size = Path(out).stat().st_size
    if size > MAX_SOURCE_SIZE:
        Path(out).unlink(missing_ok=True)
        raise PackagingError(f"حجم سورس فشرده ({size // 1024 // 1024}MB) از سقف ۲۵۶ مگابایت لیارا بیشتر است.")
    return Path(out)


def extract_zip(zip_path: str | Path) -> Path:
    dest = Path(tempfile.mkdtemp(prefix="liara-upload-"))
    with zipfile.ZipFile(zip_path) as z:
        for member in z.namelist():  # zip-slip guard
            target = (dest / member).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise PackagingError("مسیر ناامن در فایل zip")
        z.extractall(dest)
    entries = [e for e in dest.iterdir() if not e.name.startswith("__MACOSX")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest


# ---------------------------------------------------------------- helpers
def _read_json(fp: Path) -> dict | None:
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ignore_patterns(p: Path) -> list[str]:
    patterns = list(ALWAYS_IGNORE)
    src = p / ".liaraignore" if (p / ".liaraignore").exists() else p / ".gitignore"
    if src.exists():
        for line in src.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            line = line.lstrip("/").rstrip("/")
            patterns += [line, f"{line}/**", f"**/{line}", f"**/{line}/**"]
    return patterns


def _ignored(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pat) for pat in patterns)


def _iter_files(root: Path, patterns: list[str]):
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = [d for d in dirnames if not _ignored(os.path.join(rel_dir, d), patterns)]
        for f in filenames:
            rel = os.path.join(rel_dir, f)
            if not _ignored(rel, patterns):
                yield rel


def _env_keys(p: Path) -> set[str]:
    keys: set[str] = set()
    for name in (".env.example", ".env.sample", ".env.template"):
        fp = p / name
        if fp.exists():
            for line in fp.read_text(errors="ignore").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    keys.add(line.split("=", 1)[0].strip())
    return keys
