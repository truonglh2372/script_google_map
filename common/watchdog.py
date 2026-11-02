#!/usr/bin/env python3
"""OS watchdog for a crawler (truonglh2372/an_uong_quanh_day#1388). Run every 15 min by Task Scheduler (Windows) or
launchd (Mac); see the README. Stdlib only; the same file lives in script_foody/common and script_google_map/common.

  python watchdog.py --crawler foody --state-dir C:/script_foody/foody_data --match "foody_fetch\\.py" \
      --restart "C:\\...\\aqd_foody.cmd" [--stop-file C:/script_foody/foody_data/STOP]

- A STOP file present -> the crawler was stopped on purpose: nothing is done.
- The crawler process (command line matching --match) is alive -> nothing is done.
- Otherwise it runs --restart (detached), waits up to --wait seconds for the process and posts to Slack:
  restarted (⚫→🟢) or could not restart (⚫, red mention). More than --max-restarts restarts in one day (UTC+7)
  -> a red "restarted too often" alert instead. Alerts use the same webhook, mention and anti-spam as slack_notify.
Exit code 0 when the crawler runs (or STOP), 1 when it could not be restarted.
"""
import argparse, json, os, re, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import slack_notify as sn  # noqa: E402


def list_processes():
    """[(pid, command line)] of this machine's processes; [] when it cannot be read."""
    try:
        if os.name == "nt":
            ps = ("Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                                 timeout=60).stdout
            rows = json.loads(out or "[]")
            rows = rows if isinstance(rows, list) else [rows]
            return [(int(r.get("ProcessId") or 0), r.get("CommandLine") or "") for r in rows]
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=30).stdout
        procs = []
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                procs.append((int(parts[0]), parts[1]))
        return procs
    except Exception:  # noqa: BLE001
        return []


def find(pattern, procs, me=None):
    rx = re.compile(pattern)
    me = os.getpid() if me is None else me
    return [pid for pid, cmd in procs if pid != me and rx.search(cmd or "") and "watchdog.py" not in cmd]


def start_detached(command):
    """Start the restart command without waiting for it; returns the Popen or raises."""
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(command, shell=True, **kw)


class Watchdog:
    def __init__(self, crawler, state_dir, match, restart, stop_files=(), wait=30, max_restarts=3, env=None,
                 now=time.time, sender=None, lister=list_processes, starter=start_detached, sleep=time.sleep):
        self.n = sn.Notifier(crawler, state_dir, env=env, now=now, sender=sender)
        self.match, self.restart, self.stop_files = match, restart, list(stop_files)
        self.wait, self.max_restarts = wait, max_restarts
        self.lister, self.starter, self.sleep, self.now = lister, starter, sleep, now
        self.state_file = os.path.join(state_dir, "watchdog_state.json")

    def _load(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self, st):
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f)
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    def run(self):
        """'stop' / 'alive' / 'restarted' / 'failed' / 'flapping'."""
        if any(os.path.exists(p) for p in self.stop_files):
            sn.log("watchdog: có file STOP -> không làm gì")
            return "stop"
        if find(self.match, self.lister()):
            return "alive"
        seen = self.now()
        last = self.n._load().get("last_report") or self.n._load().get("alive_at")
        day = sn.datetime.fromtimestamp(seen, sn.ICT).strftime("%Y-%m-%d")
        st = self._load()
        if st.get("day") != day:
            st = {"day": day, "restarts": 0}
        st["restarts"] = int(st.get("restarts") or 0) + 1
        self._save(st)
        rc, pids = None, []
        try:
            proc = self.starter(self.restart)
            for _ in range(max(1, int(self.wait // 5))):
                self.sleep(5)
                pids = find(self.match, self.lister())
                if pids:
                    break
                rc = proc.poll() if hasattr(proc, "poll") else None
        except Exception as e:  # noqa: BLE001
            rc = type(e).__name__
        title = self.n._title()
        foot = f"_Watchdog ({self.n.scheduler}, 15 phút/lần) · {sn.ddmm_hhmm(seen)} (UTC+7)_"
        cmd = f"`{sn._clean(self.restart, 100)}`"
        if not pids:
            if not self.n._update(lambda s: self.n._allow(s, "watchdog_failed")):
                return "failed"
            why = f"thất bại (exit {rc})" if rc not in (None, 0) else "không thấy process sau khi chạy"
            text = (f"⚫ {sn.mention(self.n.env)}{title} — watchdog: crawler đã dừng, KHÔNG khởi động lại được\n"
                    f"• Không thấy process `{self.match}` lúc {sn.hhmm(seen)}; chạy lại {cmd} {why}\n"
                    f"• Việc cần làm: *đăng nhập {self.n.host} kiểm tra*\n{foot}")
            self.n._send("watchdog", text)
            return "failed"
        if st["restarts"] > self.max_restarts:
            if self.n._update(lambda s: self.n._allow(s, "watchdog_flapping")):
                text = (f"🔴 {sn.mention(self.n.env)}{title} — watchdog: đã khởi động lại {st['restarts']} lần hôm nay\n"
                        f"• Lần này: chạy lại {cmd} → PID {pids[0]} đang chạy\n"
                        f"• Việc cần làm: *crawler chết liên tục, xem log trên {self.n.host}*\n{foot}")
                self.n._send("watchdog", text)
            return "flapping"
        text = (f"⚫→🟢 {title} — watchdog: process đã chết, đã khởi động lại\n"
                f"• Không thấy `{self.match}` lúc {sn.hhmm(seen)} (UTC+7)"
                + (f"; báo cáo cuối của crawler: {sn.hhmm(float(last))}" if last else "") + "\n"
                f"• Đã chạy lại {cmd} → PID {pids[0]} đang chạy\n"
                f"• Việc cần làm: không (nếu bị khởi động lại >{self.max_restarts} lần/ngày sẽ báo 🔴)\n{foot}")
        self.n._send("watchdog", text)
        return "restarted"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--crawler", required=True)
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--match", required=True, help="regex on the crawler's command line")
    ap.add_argument("--restart", required=True, help="command that starts the crawler again")
    ap.add_argument("--stop-file", action="append", default=[])
    ap.add_argument("--wait", type=int, default=30)
    ap.add_argument("--max-restarts", type=int, default=3)
    ap.add_argument("--log", help="append output to this file (pythonw / launchd have no console)")
    a = ap.parse_args(argv)
    if a.log:
        try:
            sys.stdout = open(a.log, "a", encoding="utf-8", buffering=1)
            sys.stderr = sys.stdout
        except OSError:
            pass
    sn.log(f"watchdog {a.crawler}: {sn.ddmm_hhmm(time.time())} kiểm tra")
    out = Watchdog(a.crawler, a.state_dir, a.match, a.restart, a.stop_file, a.wait, a.max_restarts).run()
    sn.log(f"watchdog {a.crawler}: {out}")
    return 1 if out == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
