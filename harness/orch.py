#!/usr/bin/env python3
"""Orchestrates local-LLM coding sessions (llama.cpp + Pi) on Fusion-Integrated, one step at a time.

    orch.py setup <impl>                     clone the clean base into impl-<impl> and build its venv
    orch.py step  <impl> <prompt.md> [--timeout S] [--tag T]   one Pi session, then gates + checkpoint commit
    orch.py fix   <impl> [--timeout S]       one Pi session built from the first failing gate of the last run
    orch.py gates <impl> [--full]            run the gates and print a summary

Every session leaves evidence in evidence/<impl>/<run-id>/: the exact prompt, Pi's JSON event
stream and session file, the llama-server log, speed metrics, gate results and the diff.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

A6 = Path.home() / "a6"
ORCH = A6 / "orch"
EVID = A6 / "evidence"
RESULTS = A6 / "results.jsonl"
PI_DIR = A6 / "pi-agent"
SERVER = Path.home() / "Desktop/17-Placement Cell/Assignment6_Pi_Bench/runtime/llama-b11560/llama-server"
# Everything a run touches stays on the internal disk: the T7 drive dropped mid-run on 26 Oct.
MODELS = A6 / "models"
LOCAL_PY = A6 / "python/cpython-3.12.15-macos-aarch64-none/bin/python3.12"
PORT = 18080
DISK_FLOOR_GB = 2.0
# ponytail: one shared server profile; per-impl fields only where the models differ.
SERVER_ARGS = ["-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--cache-ram", "0", "-ctxcp", "8"]
QWEN_SAMPLING = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0}
IMPLS = {
    "frognano": {"model": "FrogNano-4B-2609-Q4_K_M.gguf", "ctx": 32768,
                 "extra": ["--spec-type", "draft-mtp"], "sampling": QWEN_SAMPLING, "thinking": "medium"},
    "qwen35": {"model": "Qwen3.5-4B-Q4_K_M.gguf", "ctx": 32768,
               "extra": [], "sampling": QWEN_SAMPLING, "thinking": "medium"},  # this GGUF build ships no MTP layers
}
GATES = [
    ("django check", ".venv/bin/python manage.py check"),
    ("migrations in sync", ".venv/bin/python manage.py makemigrations --check --dry-run"),
    ("ruff", ".venv/bin/ruff check ."),
    ("import contracts", ".venv/bin/lint-imports"),
    ("no cross-module FK", ".venv/bin/python ops/checks/no_cross_module_fk.py"),
    ("contracts plural", ".venv/bin/python ops/checks/contracts_are_plural.py"),
    ("nav matches routes", ".venv/bin/python ops/checks/nav_matches_routes.py"),
    ("permission manifest", ".venv/bin/python manage.py permission_manifest --check"),
    ("openapi schema", ".venv/bin/python manage.py spectacular --fail-on-warn --file /tmp/orch-schema.yaml"
                       " >/dev/null && diff -q openapi/fusion-integrated.v1.yaml /tmp/orch-schema.yaml"),
    ("pytest", ".venv/bin/python -m pytest -q -x -p no:cacheprovider"),
]
CLIENT_GATES = [
    ("client typecheck", "cd client && npx tsc --noEmit"),
    ("client tests", "cd client && npm test --silent"),
    ("client build", "cd client && npm run build --silent"),
]
DEPLOY_GATE = ("deploy check", "DJANGO_SETTINGS_MODULE=config.settings.prod DB_PASSWORD=ci DJANGO_SECRET_KEY=ci-only-key-long-enough-to-satisfy-the-length-check"
               " DJANGO_ALLOWED_HOSTS=ci.invalid IAM_SERVICE_TOKEN=ci PLACEMENT_UPLOAD_ROOT=/tmp/ci-uploads"
               " REDIS_CACHE_URL=redis://127.0.0.1:6379/1 REDIS_BROKER_URL=redis://127.0.0.1:6380/0"
               " .venv/bin/python manage.py check --deploy --fail-level WARNING")


def repo(impl):
    return A6 / f"impl-{impl}"


def sh(cmd, cwd, timeout=900):
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.dev"}
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def disk_free_gb():
    st = os.statvfs("/System/Volumes/Data")
    return st.f_bavail * st.f_frsize / 1e9


def mem_free_pct():
    out = subprocess.run(["sysctl", "-n", "kern.memorystatus_level"], capture_output=True, text=True).stdout.strip()
    return int(out or 0)


def run_gates(impl, full=False):
    r = repo(impl)
    if os.environ.get("ORCH_FAST"):
        rc, out = sh("sh ~/a6/orch/quick-check.sh", r)
        return [{"name": "quick-check", "cmd": "sh ~/a6/orch/quick-check.sh", "ok": rc == 0, "rc": rc, "seconds": 0,
                 "tail": "\n".join(out.strip().splitlines()[-40:])}]
    gates = list(GATES)
    if full or (r / "client/src/modules/leave").exists():
        gates += CLIENT_GATES
    if full:
        gates.append(DEPLOY_GATE)
    results = []
    for name, cmd in gates:
        t0 = time.time()
        rc, out = sh(cmd, r)
        results.append({"name": name, "cmd": cmd, "ok": rc == 0, "rc": rc,
                        "seconds": round(time.time() - t0), "tail": "\n".join(out.strip().splitlines()[-40:])})
    return results


def write_pi_agent(impl):
    cfg = IMPLS[impl]
    PI_DIR.mkdir(exist_ok=True)
    model = {"id": impl, "name": impl, "reasoning": cfg["thinking"] != "off", "contextWindow": cfg["ctx"],
             "maxTokens": 8192, "input": ["text"],
             "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
             "compat": {"thinkingFormat": "qwen-chat-template"}, "samplingParams": cfg["sampling"]}
    (PI_DIR / "models.json").write_text(json.dumps({"providers": {"local": {
        "baseUrl": f"http://127.0.0.1:{PORT}/v1", "api": "openai-completions", "apiKey": "local",
        "models": [model]}}}, indent=2))
    (PI_DIR / "settings.json").write_text(json.dumps({"defaultProvider": "local", "defaultModel": impl, "quietStartup": True}))


def start_server(impl, log):
    cfg = IMPLS[impl]
    cmd = [str(SERVER), "-m", str(MODELS / cfg["model"]), "-c", str(cfg["ctx"]), "-ngl", "999", "--jinja",
           "--metrics", "--parallel", "1", "--host", "127.0.0.1", "--port", str(PORT), *SERVER_ARGS, *cfg["extra"]]
    log.write(" ".join(cmd) + "\n")
    log.flush()
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    t0 = time.time()
    while time.time() - t0 < 300:
        if proc.poll() is not None:
            raise RuntimeError("llama-server exited during load; see server.log")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
                if r.status == 200:
                    return proc, time.time() - t0
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("llama-server load timeout")


def scrape_metrics():
    try:
        txt = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/metrics", timeout=5).read().decode()
    except Exception:
        return {}
    f = {k: float(v) for k, v in re.findall(r"^llamacpp:(\w+) ([\d.e+-]+)$", txt, re.M)}
    pp_s, tg_s = f.get("prompt_seconds_total", 0), f.get("tokens_predicted_seconds_total", 0)
    return {"prompt_tokens": f.get("prompt_tokens_total", 0), "gen_tokens": f.get("tokens_predicted_total", 0),
            "pp_tps": round(f.get("prompt_tokens_total", 0) / pp_s, 1) if pp_s else 0,
            "tg_tps": round(f.get("tokens_predicted_total", 0) / tg_s, 1) if tg_s else 0}


def kill(proc):
    if proc and proc.poll() is None:
        try:
            os.killpg(proc.pid, 15)
            proc.wait(10)
        except Exception:
            os.killpg(proc.pid, 9)


def summarize_events(path):
    calls = errors = turns = compactions = model_errors = 0
    for line in path.read_text(errors="ignore").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = e.get("type", "")
        calls += t == "tool_execution_end"
        errors += t == "tool_execution_end" and bool(e.get("isError"))
        turns += t == "turn_end"
        compactions += t == "compaction_end"
        m = e.get("message") or {}
        model_errors += t == "message_end" and m.get("role") == "assistant" and m.get("stopReason") == "error"
    return {"tool_calls": calls, "tool_errors": errors, "turns": turns, "compactions": compactions,
            "model_errors": model_errors}


def preflight(impl):
    problems = [p for p in (SERVER, MODELS / IMPLS[impl]["model"], LOCAL_PY) if not p.exists()]
    if sh(".venv/bin/python -c 'import django'", repo(impl))[0] != 0:
        problems.append(repo(impl) / ".venv")
    if problems:
        raise SystemExit("preflight failed: " + ", ".join(map(str, problems)))


def session(impl, prompt_text, tag, timeout):
    r = repo(impl)
    preflight(impl)
    if disk_free_gb() < DISK_FLOOR_GB + 0.5:
        raise SystemExit(f"disk guard: only {disk_free_gb():.1f} GB free")
    run_id = f"{time.strftime('%m%d-%H%M%S')}_{tag}"
    rd = EVID / impl / run_id
    rd.mkdir(parents=True)
    (rd / "prompt.md").write_text(prompt_text)
    write_pi_agent(impl)
    server_log = open(rd / "server.log", "w")
    server, load_s = start_server(impl, server_log)
    pi = ["sandbox-exec", "-f", str(ORCH / "agent.sb"), shutil.which("pi"), "-p", "--mode", "json",
          "--provider", "local", "--model", impl, "--thinking", IMPLS[impl]["thinking"],
          "--session-dir", str(rd), "--no-context-files", "--no-extensions", "-e", str(ORCH / "cap-output.ts"), "--no-skills",
          "--no-prompt-templates", "--offline", "--append-system-prompt", str(ORCH / "guide.md"), prompt_text]
    env = {**os.environ, "PI_CODING_AGENT_DIR": str(PI_DIR), "PI_OFFLINE": "1", "PI_TELEMETRY": "0",
           "DJANGO_SETTINGS_MODULE": "config.settings.dev"}
    low, stop, abort, mem_abort = [100], threading.Event(), threading.Event(), threading.Event()

    res = open(rd / "resources.tsv", "w")
    res.write("t\tmem_free_pct\tswap_used_mb\tdisk_free_gb\ttop_rss\n")

    def sampler():
        n = starved = 0
        while not stop.wait(5):
            free = mem_free_pct()
            low[0] = min(low[0], free)
            # ponytail: 30 s under 5% free froze this 8 GB Mac twice; stop before that, not after.
            starved = starved + 1 if free < 5 else 0
            if starved >= 6:
                mem_abort.set()
                kill(agent)
                kill(server)
            if n % 6 == 0:
                swap = re.search(r"used = ([\d.]+)M", subprocess.run(["sysctl", "vm.swapusage"], capture_output=True, text=True).stdout)
                top = subprocess.run("ps -axo rss=,comm= | sort -rn | head -5", shell=True, capture_output=True, text=True).stdout
                top = "; ".join(f"{int(l.split()[0])//1024}MB {Path(l.split(None, 1)[1]).name}" for l in top.splitlines() if l.strip())
                res.write(f"{round(time.time() - t0)}\t{mem_free_pct()}\t{swap.group(1) if swap else ''}\t{disk_free_gb():.2f}\t{top}\n")
                res.flush()
            n += 1
            if disk_free_gb() < DISK_FLOOR_GB:
                abort.set()
                kill(agent)

    t0 = time.time()
    with open(rd / "events.jsonl", "w") as ev:
        agent = subprocess.Popen(pi, cwd=r, env=env, stdout=ev, stderr=subprocess.STDOUT, start_new_session=True)
        threading.Thread(target=sampler, daemon=True).start()
        try:
            agent.wait(timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            kill(agent)
            timed_out = True
    wall = time.time() - t0
    stop.set()
    metrics = scrape_metrics()
    kill(server)
    server_log.close()

    gates = run_gates(impl)
    sh("git add -A -N . && git diff HEAD > /tmp/orch.diff", r)
    shutil.copy("/tmp/orch.diff", rd / "diff.patch")
    sh(f"git add -A && git commit -q -m 'ELM {impl}: {tag}' --allow-empty", r)
    row = {"run": run_id, "impl": impl, "tag": tag, "wall_s": round(wall), "load_s": round(load_s),
           "timed_out": timed_out, "disk_abort": abort.is_set(), "mem_abort": mem_abort.is_set(), "agent_rc": agent.returncode,
           "min_mem_free_pct": low[0], **metrics, **summarize_events(rd / "events.jsonl"),
           "gates_ok": [g["name"] for g in gates if g["ok"]], "gates_failed": [g["name"] for g in gates if not g["ok"]]}
    (rd / "gates.json").write_text(json.dumps(gates, indent=2))
    (rd / "run.json").write_text(json.dumps(row, indent=2))
    with open(RESULTS, "a") as f:
        f.write(json.dumps(row) + "\n")
    print(json.dumps(row, indent=2))
    return row


FIX = """Session goal: fix a failing repository check in the leave module work.
The check "{name}" fails. Its command is: {cmd}
Last lines of its output:
```
{tail}
```
Find the cause and fix it with the smallest correct change. Never delete tests or weaken a check; regenerate generated files with make permissions or make schema instead of editing them.
Run the failing command again, then sh ~/a6/orch/quick-check.sh, until both pass. Read docs/09-leave/leave-blueprint.md only if you need it.
"""


def last_failure(impl):
    runs = sorted((EVID / impl).glob("*/gates.json"))
    for g in json.loads(runs[-1].read_text()):
        if not g["ok"]:
            return g
    return None


def setup(impl):
    r = repo(impl)
    if not r.exists():
        subprocess.run(["git", "clone", "-q", str(A6 / "base"), str(r)], check=True)
        subprocess.run(["git", "remote", "set-url", "origin", "https://github.com/FusionIIIT/Fusion-Integrated"], cwd=r, check=True)
    print(sh(f"uv venv -q --python {LOCAL_PY} .venv && uv pip install -q --python .venv/bin/python -e '.[dev]'", r, 1800)[1][-500:])
    print(sh("cd client && npm ci --no-audit --no-fund --silent", r, 1800)[1][-500:])
    print(sh("git log -1 --format='%h %s'", r)[1])


def main():
    a = sys.argv[1:]
    cmd, impl = a[0], a[1]
    opt = lambda k, d: a[a.index(k) + 1] if k in a else d
    if cmd == "setup":
        setup(impl)
    elif cmd == "step":
        p = Path(a[2])
        session(impl, p.read_text(), opt("--tag", p.stem), int(opt("--timeout", 2400)))
    elif cmd == "fix":
        g = last_failure(impl)
        if not g:
            print("no failing gate")
            return
        tail = "\n".join(g["tail"].splitlines()[-30:])
        session(impl, FIX.format(name=g["name"], cmd=g["cmd"], tail=tail), opt("--tag", "fix-" + re.sub(r"\W+", "-", g["name"])),
                int(opt("--timeout", 1500)))
    elif cmd == "gates":
        res = run_gates(impl, full="--full" in a)
        for g in res:
            print(("ok   " if g["ok"] else "FAIL ") + f"{g['name']} ({g['seconds']}s)")
        bad = [g for g in res if not g["ok"]]
        if bad:
            print(bad[0]["tail"][-1500:])


if __name__ == "__main__":
    main()
