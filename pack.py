import os

exclude_dirs = {".git", "venv", "venv314", ".venv", "__pycache__", "logs", "backups", "data", ".vscode", ".idea"}
exclude_files = {"pack.py", "full_codebase.txt"}
valid_exts = {".py", ".sh", ".yaml", ".yml", ".toml"}
valid_names = {"Dockerfile", "docker-compose.yml"}

MAX_FILE_SIZE_BYTES = 200 * 1024  # Skip any file larger than 200 KB

with open("full_codebase.txt", "w", encoding="utf-8") as out:
    for root, dirs, files in os.walk("."):
        dirs[:] = sorted([d for d in dirs if d not in exclude_dirs])
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

print("Done! Upload full_codebase.txt")