@tool
def list_directory(path: str = ".", show_hidden: bool = False) -> str:
    """List files and directories at the given path.
    Args:
        path: Directory path to list (default: current directory).
        show_hidden: Whether to include hidden files (default: false).
    """
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"Directory not found: {path}"
        if not p.is_dir():
            return f"Not a directory: {path}"
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        lines = [f"Contents of {p}:", ""]
        for entry in entries:
            if not show_hidden and entry.name.startswith("."):
                continue
            prefix = "[Folder] " if entry.is_dir() else "[File] "
            size = ""
            if entry.is_file():
                s = entry.stat().st_size
                if s < 1024:
                    size = f"  ({s}B)"
                elif s < 1_048_576:
                    size = f"  ({s / 1024:.1f}KB)"
                else:
                    size = f"  ({s / 1_048_576:.1f}MB)"
            lines.append(f"  {prefix}{entry.name}{size}")
        if len(lines) == 2:
            lines.append("  (empty)")
        return "\n".join(lines)
    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Error listing directory: {e}"