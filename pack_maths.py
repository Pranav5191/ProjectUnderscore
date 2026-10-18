import os

# Only scan these specific subdirectories for math, quantitative, and trading code
include_subdirs = {
    os.path.normpath("src/ml"),
    os.path.normpath("src/scoring"),
    os.path.normpath("src/scripts"),
    os.path.normpath("src/trading"),
    os.path.normpath("src/utils")
}

exclude_dirs = {".git", "venv", "venv314", ".venv", "__pycache__", "logs", "backups", "data", ".vscode", ".idea"}
exclude_files = {"pack.py", "full_codebase.txt"}
valid_exts = {".py", ".sh", ".yaml", ".yml", ".toml"}
valid_names = {"Dockerfile", "docker-compose.yml"}

MAX_FILE_SIZE_BYTES = 200 * 1024  # Skip any file larger than 200 KB

with open("quant_code.txt", "w", encoding="utf-8") as out:
    for root, dirs, files in os.walk("."):
        dirs[:] = sorted([d for d in dirs if d not in exclude_dirs])
        
        # Check if the current root path starts with any of our allowed subdirectories
        norm_root = os.path.normpath(root)
        is_allowed = any(norm_root == inc or norm_root.startswith(inc + os.sep) for inc in include_subdirs)
        if not is_allowed:
            continue

        for f in sorted(files):
            if f in exclude_files:
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in valid_exts or f in valid_names:
                p = os.path.join(root, f)
                if os.path.getsize(p) > MAX_FILE_SIZE_BYTES:
                    continue
                out.write(f"\n{'='*60}\nFILE: {p}\n{'='*60}\n")
                with open(p, "r", encoding="utf-8", errors="ignore") as src:
                    out.write(src.read())

print("Done! Math & Quant codebase packed into quant_code.txt")