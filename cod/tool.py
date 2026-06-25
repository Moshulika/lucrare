@tool
def list_directory(path: str = ".", show_hidden: bool = False) -> str:
    """List files and directories at the given path.
    Args: path: Directory path to list (default: current directory).
          show_hidden: Whether to include hidden files (default: false)."""
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists(): return f"Directory not found: {path}"
        if not p.is_dir(): return f"Not a directory: {path}"
        def fmt_size(e):
            s = e.stat().st_size
            return f"  ({s}B)" if s < 1024 else f"  ({s/1024:.1f}KB)" if s < 1_048_576 else f"  ({s/1_048_576:.1f}MB)"
        entries = [e for e in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
                   if show_hidden or not e.name.startswith(".")]
        rows = [f"  {'[Folder] ' if e.is_dir() else '[File] '}{e.name}{fmt_size(e) if e.is_file() else ''}" for e in entries]
        return "\n".join([f"Contents of {p}:", ""] + (rows or ["  (empty)"]))
    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Error listing directory: {e}"