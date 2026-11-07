"""Network watchdog (system/strawman-netwatch.sh) escalation, without touching
the network: the router check is a fake that replays a script of up/down, and
every action runs in dry-run mode. Runs on the Mac or the Pi.
Run: python3 test_netwatch.py"""
import os, subprocess, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "system", "strawman-netwatch.sh")


def run(pattern, uptime=7200, last_reboot=None):
    """pattern: one char per check, '.' = router answers, 'x' = it doesn't.
    Returns the watchdog's log lines (startup line dropped)."""
    with tempfile.TemporaryDirectory() as d:
        seq = os.path.join(d, "seq")
        open(seq, "w").write(pattern)
        # Pops the first char; succeeds when it was '.'
        check = os.path.join(d, "check")
        open(check, "w").write(f'#!/bin/sh\nc=$(head -c1 "{seq}"); tail -c +2 "{seq}" > "{seq}.n"; '
                               f'mv "{seq}.n" "{seq}"; [ "$c" = . ]\n')
        os.chmod(check, 0o755)
        open(os.path.join(d, "uptime"), "w").write(f"{uptime}.42 1000.00\n")
        state = os.path.join(d, "state")
        if last_reboot is not None:
            os.makedirs(state)
            open(os.path.join(state, "last-reboot"), "w").write(f"{int(time.time() - last_reboot)}\n")
        env = dict(os.environ, NETWATCH_INTERVAL="0", NETWATCH_DRY="1", NETWATCH_CHECK=check,
                   NETWATCH_MAX_CHECKS=str(len(pattern)), NETWATCH_STATE_DIR=state,
                   NETWATCH_UPTIME_FILE=os.path.join(d, "uptime"),
                   NETWATCH_RESET_AFTER="6", NETWATCH_RELOAD_AFTER="12", NETWATCH_REBOOT_AFTER="18")
        out = subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True, timeout=30)
        rebooted = os.path.exists(os.path.join(state, "last-reboot")) and last_reboot is None
        return out.stdout.splitlines()[1:], out.stderr, rebooted


failures = 0
def check(name, cond, lines, err=""):
    global failures
    failures += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        print("      " + "\n      ".join(lines + ([err] if err else [])))


lines, err, rb = run("." * 10 + "x" * 5 + "." * 3)
check("short blip: nothing done", lines == [] and not rb, lines, err)

lines, err, rb = run("x" * 6 + "...")
check("3 min down: Wi-Fi reset, then recovery logged",
      any("wifi off" in l for l in lines) and any("wifi on" in l for l in lines)
      and not any("modprobe" in l for l in lines) and "answering again" in lines[-1], lines, err)

lines, err, rb = run("x" * 12)
check("6 min down: driver reload", any("modprobe -r brcmfmac_wcc brcmfmac" in l for l in lines)
      and not any("reboot" in l for l in lines), lines, err)

lines, err, rb = run("x" * 18)
check("9 min down, up 2 h, never rebooted: reboots",
      any("would run: systemctl reboot" in l for l in lines) and rb, lines, err)

lines, err, rb = run("x" * 18, uptime=600)
check("9 min down but only up 10 min: no reboot",
      not any("systemctl reboot" in l for l in lines) and "only up 10 min" in lines[-1], lines, err)

lines, err, rb = run("x" * 18, last_reboot=1200)
check("rebooted 20 min ago: no reboot", not any("systemctl reboot" in l for l in lines)
      and "already rebooted 20 min ago" in lines[-1], lines, err)

lines, err, rb = run("x" * 30, last_reboot=1200)
resets = sum("wifi off" in l for l in lines)
check("not allowed to reboot: goes round the Wi-Fi resets again", resets == 2
      and sum("modprobe -r" in l for l in lines) == 2, lines, err)

lines, err, rb = run("x" * 8 + "." + "x" * 5)
check("recovery in between restarts the count", sum("wifi off" in l for l in lines) == 1
      and not any("modprobe" in l for l in lines), lines, err)

raise SystemExit(1 if failures else 0)
