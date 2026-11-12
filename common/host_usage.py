"""What is using the disk and memory on this machine - pure Python, works on Windows, macOS and Linux.

Used by disk_cleanup.py when the disk is low but the crawler folders had nothing left to delete (the disk_low
alert must then say WHAT is taking the space). No `du`, no psutil: an os.scandir walk with a time budget and an
entry budget per folder (partial totals are shown as ">=" so a huge C:\\Windows never hangs the job), the
Windows page/hibernate/swap files, and the processes using the most memory (tasklist on Windows, ps elsewhere).
Read-only: nothing here deletes or changes anything. The same file lives in script_google_map/common and
script_foody/common.

Knobs (env): AQD_SPACE_SCAN_SECONDS (90) total time for all folders, AQD_SPACE_SCAN_MAX_ENTRIES (2000000)
entries per folder, AQD_SPACE_TOP (5) rows per folder, AQD_SPACE_PROCS (5) memory rows.
"""
import csv, io, ntpath, os, posixpath, subprocess, sys, time

IS_WIN = os.name == "nt"
SYSTEM_FILES = ("pagefile.sys", "hiberfil.sys", "swapfile.sys")
_REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: junctions / symlinks / OneDrive placeholders - never followed


def _pm(is_win):
    """Path rules of the target OS (ntpath on Windows), so the Windows path logic is testable on macOS/Linux."""
    return ntpath if is_win else posixpath


def _env_num(env, name, default):
    try:
        return float((env or {}).get(name, "") or default)
    except ValueError:
        return default


def human(n):
    """1536 -> '1,5 KB' (Vietnamese decimal comma, same style as slack_notify.gb)."""
    n = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            break
        n /= 1024
    if unit == "GB" and n >= 1024:
        n, unit = n / 1024, "TB"
    return (f"{n:.0f}" if unit == "B" else f"{n:.1f}").replace(".", ",") + " " + unit


def _skip_entry(entry):
    """Never follow symlinks or Windows reparse points (junctions like 'Application Data' loop forever)."""
    try:
        if entry.is_symlink():
            return True
        is_junction = getattr(entry, "is_junction", None)  # Python 3.12+
        if is_junction and is_junction():
            return True
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attrs & _REPARSE)
    except OSError:
        return True


def dir_size(path, deadline, max_entries, scandir=os.scandir, clock=time.monotonic):
    """-> (bytes, complete). Iterative os.scandir walk; stops at the deadline or after max_entries entries
    (complete=False -> the total is a lower bound). Unreadable folders are skipped (counted as incomplete)."""
    total, seen, complete, stack = 0, 0, True, [path]
    while stack:
        d = stack.pop()
        try:
            it = scandir(d)
        except OSError:
            complete = False
            continue
        with it:
            for e in it:
                seen += 1
                if seen > max_entries or clock() > deadline:
                    return total, False
                if _skip_entry(e):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    else:
                        total += e.stat(follow_symlinks=False).st_size
                except OSError:
                    complete = False
    return total, complete


def children_sizes(path, budget_s, max_entries, scandir=os.scandir, clock=time.monotonic):
    """[(name, bytes, complete)] for every child of path, biggest first. Files count with their own size;
    each sub-folder gets a fair share of what is left of budget_s."""
    try:
        with scandir(path) as it:
            entries = [e for e in it if not _skip_entry(e)]
    except OSError:
        return []
    out, dirs = [], []
    for e in entries:
        try:
            if e.is_dir(follow_symlinks=False):
                dirs.append(e)
            else:
                out.append((e.name, e.stat(follow_symlinks=False).st_size, True))
        except OSError:
            continue
    end = clock() + max(0.0, budget_s)
    for i, e in enumerate(dirs):
        share = max(0.2, (end - clock()) / max(1, len(dirs) - i))
        size, ok = dir_size(e.path, clock() + share, max_entries, scandir=scandir, clock=clock)
        out.append((e.name, size, ok))
    return sorted(out, key=lambda r: r[1], reverse=True)


def system_files(drive_root, stat=os.stat, names=SYSTEM_FILES, is_win=IS_WIN):
    """[(name, bytes)] of pagefile.sys / hiberfil.sys / swapfile.sys present at the drive root (Windows)."""
    out = []
    for n in names:
        try:
            out.append((n, stat(_pm(is_win).join(drive_root, n)).st_size))
        except OSError:
            continue
    return out


def _run(args, timeout=30):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, errors="replace")
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def parse_tasklist(text):
    """`tasklist /FO CSV /NH` -> [(image, kb)]. The memory column is locale-formatted ('1,234 K' / '1.234 K')."""
    out = []
    for row in csv.reader(io.StringIO(text or "")):
        if len(row) < 5:
            continue
        digits = "".join(ch for ch in row[4] if ch.isdigit())
        if digits:
            out.append((row[0], int(digits)))
    return out


def parse_ps(text):
    """`ps -axo rss=,comm=` -> [(name, kb)]."""
    out = []
    for line in (text or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            out.append((os.path.basename(parts[1]), int(parts[0])))
    return out


def top_memory(limit=5, runner=_run, is_win=IS_WIN):
    """[(name, count, bytes)] grouped by image name (chrome.exe has dozens of processes), biggest first."""
    rows = parse_tasklist(runner(["tasklist", "/FO", "CSV", "/NH"])) if is_win else parse_ps(runner(["ps", "-axo", "rss=,comm="]))
    agg = {}
    for name, kb in rows:
        c, b = agg.get(name, (0, 0))
        agg[name] = (c + 1, b + kb * 1024)
    return sorted(((n, c, b) for n, (c, b) in agg.items()), key=lambda r: r[2], reverse=True)[:limit]


def list_commands(runner=_run, is_win=IS_WIN):
    """[(pid, command line)] of this machine's processes, or None when the list cannot be read (callers that
    must be sure a program is NOT running treat None as 'maybe running')."""
    import json
    try:
        if is_win:
            ps = "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
            out = runner(["powershell", "-NoProfile", "-Command", ps], timeout=60)
            if not out.strip():
                return None
            rows = json.loads(out)
            rows = rows if isinstance(rows, list) else [rows]
            return [(int(r.get("ProcessId") or 0), r.get("CommandLine") or "") for r in rows]
        out = runner(["ps", "-axo", "pid=,command="], timeout=30)
        if not out.strip():
            return None
        procs = []
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                procs.append((int(parts[0]), parts[1]))
        return procs
    except (ValueError, TypeError, AttributeError):
        return None


def scan_targets(root, env=None, is_win=IS_WIN):
    """Folders whose children are listed, most useful first. Windows: the drive root of the crawler (C:\\),
    %USERPROFILE%, %LOCALAPPDATA%, the crawler root. Elsewhere: the home folder and the crawler root."""
    env = os.environ if env is None else env
    pm = _pm(is_win)
    root = pm.abspath(root) if is_win == IS_WIN else pm.normpath(root)
    out = []
    if is_win:
        drive = pm.splitdrive(root)[0] or "C:"
        out.append(drive + "\\")
        for k in ("USERPROFILE", "LOCALAPPDATA"):
            if env.get(k):
                out.append(env[k])
    else:
        out.append(os.path.expanduser("~"))
    out.append(root)
    seen, uniq = set(), []
    for p in out:
        key = pm.normcase(pm.normpath(p))
        if key not in seen:
            seen.add(key); uniq.append(p)
    return uniq


def _fmt_rows(rows, top):
    return ", ".join(f"{n} {'>=' if not ok else ''}{human(b)}" for n, b, ok in rows[:top] if b > 0)


def report(root, env=None, is_win=IS_WIN, scandir=os.scandir, stat=os.stat, runner=_run, clock=time.monotonic):
    """One line for Slack: system files, biggest children of each target folder, top memory processes."""
    env = os.environ if env is None else env
    budget = _env_num(env, "AQD_SPACE_SCAN_SECONDS", 90)
    max_entries = int(_env_num(env, "AQD_SPACE_SCAN_MAX_ENTRIES", 2000000))
    top = int(_env_num(env, "AQD_SPACE_TOP", 5))
    nproc = int(_env_num(env, "AQD_SPACE_PROCS", 5))
    parts = []
    if is_win:
        drive = (ntpath.splitdrive(root)[0] or "C:") + "\\"
        sf = system_files(drive, stat=stat, is_win=True)
        if sf:
            parts.append("hệ thống: " + ", ".join(f"{n} {human(b)}" for n, b in sf))
    targets = scan_targets(root, env, is_win)
    end = clock() + budget
    for i, t in enumerate(targets):
        share = max(0.5, (end - clock()) / max(1, len(targets) - i))
        rows = children_sizes(t, share, max_entries, scandir=scandir, clock=clock)
        txt = _fmt_rows(rows, top)
        if txt:
            parts.append(f"{t}: {txt}")
    try:
        mem = top_memory(nproc, runner=runner, is_win=is_win)
    except Exception:  # noqa: BLE001 - a report line never breaks the cleanup
        mem = []
    if mem:
        parts.append("RAM: " + ", ".join(f"{n}{f' x{c}' if c > 1 else ''} {human(b)}" for n, c, b in mem))
    return " · ".join(parts) or "không đo được"


if __name__ == "__main__":
    for s in (sys.stdout,):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    print(report(sys.argv[1] if len(sys.argv) > 1 else os.getcwd()))
