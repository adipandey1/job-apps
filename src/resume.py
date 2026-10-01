from pathlib import Path

RESUME_EXTENSIONS = {".pdf", ".doc", ".docx"}


def _resume_score(path: Path) -> int:
    name = path.name.lower()
    score = 0
    if "final" in name:
        score += 100
    if "resume" in name:
        score += 50
    if "cv" in name:
        score += 40
    if path.suffix.lower() in {".pdf"}:
        score += 10
    if "aditya" in name or "pandey" in name:
        score += 5
    return score


def _is_resume_candidate(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix.lower() not in RESUME_EXTENSIONS:
        return False
    name = path.name.lower()
    return any(token in name for token in ("resume", "cv", "final"))


def resolve_resume_path(search_root: str | Path | None = None) -> Path | None:
    if search_root is not None:
        root = Path(search_root).expanduser()
        if root.is_file() and _is_resume_candidate(root):
            return root
        if root.is_dir():
            candidates = [p for p in root.iterdir() if _is_resume_candidate(p)]
            if candidates:
                return max(candidates, key=_resume_score)

    roots = []
    cwd = Path.cwd()
    roots.extend([cwd, cwd.parent])
    for root in roots:
        if not root.exists():
            continue
        candidates = [p for p in root.iterdir() if _is_resume_candidate(p)]
        if candidates:
            return max(candidates, key=_resume_score)

    return None
