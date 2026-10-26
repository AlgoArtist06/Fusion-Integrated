#!/usr/bin/env python3
"""Pi + local llama.cpp benchmark, graded by Fusion-Integrated's own CI.

    python3 bench.py run <config> <task> [--reps N]
    python3 bench.py grade <task>            grade the repo as it stands
    python3 bench.py report                  rank configs from results.jsonl

Each run: reset repo -> start llama-server -> pi -p -> grade -> log a row.
Score is 0-100: task points (60) + CI gate points (40, only if task points > 0,
so an agent that does nothing scores 0 on a repo that is already green).
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = Path.home() / "fusion-pi-bench" / "base"
PY = ".venv/bin/python"          # relative: commands run with cwd=REPO
SERVER = ROOT / "runtime" / "llama-b11560" / "llama-server"
MODELS = ROOT / "models"
RESULTS = ROOT / "results.jsonl"
PORT = 18080
PI_DIR = ROOT / "pi-agent"


# ---------------------------------------------------------------- configs
@dataclass(frozen=True)
class Config:
    model: str                     # gguf filename in models/
    ctx: int = 32768
    server_args: tuple = ("-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0")
    thinking: str = "medium"       # pi --thinking level; "off" disables
    sampling: dict = field(default_factory=dict)
    max_tokens: int = 8192
    prompt_suffix: str = ""        # appended to the system prompt (file in prompts/)
    tools: str = ""                # pi --tools allowlist; "" = pi default


QWEN_CODE = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0}

CONFIGS = {
    # Phase 1: model selection, one shared baseline setup.
    "frognano": Config("frognano-q4km.gguf", sampling=QWEN_CODE),
    "spark": Config("spark-q4km.gguf",
                    sampling={"temperature": 1.0, "top_p": 0.95}),
    "qwen35": Config("qwen35-4b-q4km.gguf", sampling=QWEN_CODE),
}
# Phase 2: one lever at a time on the winner. The default 8 GiB host prompt
# cache (--cache-ram) grew swap until the boot disk filled; disable it.
_P2 = ("-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--cache-ram", "0", "-ctxcp", "8")
_FN = replace(CONFIGS["frognano"], server_args=_P2)
CONFIGS |= {
    "fn": _FN,
    "fn_mtp": replace(_FN, server_args=_P2 + ("--spec-type", "draft-mtp")),
    "fn_mtp2": replace(_FN, server_args=_P2 + ("--spec-type", "draft-mtp", "--spec-draft-n-max", "2")),
    "fn_mtp5": replace(_FN, server_args=_P2 + ("--spec-type", "draft-mtp", "--spec-draft-n-max", "5")),
    "fn_f16kv": replace(_FN, server_args=("-fa", "on") + _P2[6:]),
    "fn_ub1024": replace(_FN, server_args=_P2 + ("-ub", "1024", "-b", "2048")),
    "fn_f16mtp": replace(_FN, server_args=("-fa", "on") + _P2[6:] + ("--spec-type", "draft-mtp")),
    "fn_f16mtp5": replace(_FN, server_args=("-fa", "on") + _P2[6:] + ("--spec-type", "draft-mtp", "--spec-draft-n-max", "5")),
    "fn_f16mtp2": replace(_FN, server_args=("-fa", "on") + _P2[6:] + ("--spec-type", "draft-mtp", "--spec-draft-n-max", "2")),
    "fn_guide": replace(_FN, prompt_suffix="repo-guide.md"),
    # S4 kept MTP on q8_0 KV; quality levers stack on it from here.
    "fnm_guide": replace(_FN, server_args=_P2 + ("--spec-type", "draft-mtp"), prompt_suffix="repo-guide.md"),
    "fnm_guide_off": replace(_FN, server_args=_P2 + ("--spec-type", "draft-mtp"), prompt_suffix="repo-guide.md", thinking="off"),
    # pi's qwen-chat-template sends only enable_thinking on/off, so
    # low/medium/high are the same request; "off" is the only other level.
    "fn_off": replace(_FN, thinking="off"),
}


# ---------------------------------------------------------------- tasks
GUIDE = "docs/08-delivery/role-assignment-guide.md"
LEAVE_PERMS = {
    "leave_elm.application.view",
    "leave_elm.application.apply",
    "leave_elm.application.approve",
}
LEAVE_GRANTS = {
    "Employee": {"leave_elm.application.view", "leave_elm.application.apply"},
    "Leave Approver": {"leave_elm.application.view",
                       "leave_elm.application.approve"},
}

TASKS = {
    "grant": {
        "timeout": 900,
        "prompt": (
            f"Read {GUIDE}. Following it, give the existing designation "
            "\"Leave Approver\" the permission `directory.user.search` in the "
            "directory module, then regenerate and verify the permission "
            "manifest the way the guide says. Do not hand-edit generated files. "
            "Finish only when `make check` passes."),
    },
    "module": {
        "timeout": 1800,
        "prompt": (
            f"Read {GUIDE} (Parts 2 and 3) and use `modules/placement` and "
            "`modules/directory` as the reference shape. Create a new pure-API "
            "module `modules/leave_elm` that declares exactly the three "
            "permissions and the two roles (\"Employee\", \"Leave Approver\") "
            "from the guide's Employee Leave Management example. Make it a real "
            "installed Django app (label `leave_elm`), and enforce every "
            "permission with `HasPermission` on an API view mounted under "
            "`/api/v1/leave/`. Then run `make permissions`, regenerate the "
            "OpenAPI schema with `make schema`, and finish only when "
            "`make check` passes."),
    },
}


# ---------------------------------------------------------------- grading
@dataclass
class Check:
    name: str
    weight: float
    ok: bool
    detail: str = ""


def sh(cmd, timeout=600, cwd=REPO):
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.dev"}
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, shell=isinstance(cmd, str),
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)[-1500:]
    except subprocess.TimeoutExpired:
        return 124, "timeout"


GATES = [
    ("django check", f"{PY} manage.py check"),
    ("migrations in sync", f"{PY} manage.py makemigrations --check --dry-run"),
    ("ruff", ".venv/bin/ruff check ."),
    ("import contracts", ".venv/bin/lint-imports"),
    ("ops checks", f"{PY} ops/checks/no_cross_module_fk.py && "
                   f"{PY} ops/checks/contracts_are_plural.py && "
                   f"{PY} ops/checks/nav_matches_routes.py"),
    ("permission manifest --check", f"{PY} manage.py permission_manifest --check"),
    ("openapi schema current",
     f"{PY} manage.py spectacular --fail-on-warn --file /tmp/fi.check.yaml && "
     "diff -q openapi/fusion-integrated.v1.yaml /tmp/fi.check.yaml"),
    ("pytest", f"{PY} -m pytest -q -x -p no:cacheprovider"),
]

PROBE = r"""
import json, django; django.setup()
from importlib import import_module
from django.apps import apps
out = {}
for mod in ("modules.directory.registry", "modules.leave_elm.registry"):
    try:
        r = import_module(mod)
        out[mod] = {"perms": [c for c, _ in getattr(r, "PERMISSIONS", [])],
                    "grants": {k: list(v) for k, v in getattr(r, "ROLE_GRANTS", {}).items()}}
    except Exception as e:
        out[mod] = {"error": repr(e)[:200]}
out["apps"] = [c.name for c in apps.get_app_configs()]
print("PROBE" + json.dumps(out))
"""


def probe():
    rc, txt = sh([PY, "-c", PROBE])
    m = re.search(r"PROBE(\{.*\})", txt)
    return json.loads(m.group(1)) if m else {}


def manifest_grants(module):
    try:
        data = json.loads((REPO / "registry" / "permissions.json").read_text())
        return {k: set(v) for k, v in data["modules"][module]["grants"].items()}
    except Exception:
        return {}


def changed_files():
    sh("git add -A -N .")
    _, out = sh("git diff --name-only")
    return set(out.split())


def grade_grant():
    p = probe().get("modules.directory.registry", {})
    held = set(p.get("grants", {}).get("Leave Approver", []))
    allowed = {"modules/directory/registry.py", "registry/permissions.json",
               "docs/02-iam/permission-catalog.generated.md"}
    changed = changed_files()
    return [
        Check("registry grants Leave Approver", 15, "directory.user.search" in held),
        Check("manifest regenerated with grant", 15,
              "directory.user.search" in manifest_grants("directory").get("Leave Approver", set())),
        Check("placement untouched", 15, not any(f.startswith("modules/placement") for f in changed)
              and bool(changed)),
        Check("diff scoped to the guide's files", 15, bool(changed) and changed <= allowed,
              " ".join(sorted(changed))[:300]),
    ]


HIDDEN_TEST = ROOT / "hidden" / "test_hidden_leave.py"


def grade_module():
    p = probe()
    reg = p.get("modules.leave_elm.registry", {})
    grants = {k: set(v) for k, v in reg.get("grants", {}).items()}
    checks = [
        Check("registry declares guide perms+roles", 10,
              set(reg.get("perms", [])) >= LEAVE_PERMS
              and all(grants.get(r, set()) >= g for r, g in LEAVE_GRANTS.items()),
              reg.get("error", "")),
        Check("installed Django app", 10, "modules.leave_elm" in p.get("apps", [])),
        Check("manifest carries leave_elm grants", 10,
              all(manifest_grants("leave_elm").get(r, set()) >= g
                  for r, g in LEAVE_GRANTS.items())),
    ]
    dst = REPO / "zz_hidden_test_leave.py"
    shutil.copy(HIDDEN_TEST, dst)
    xml = Path("/tmp/fi_hidden.xml")
    xml.unlink(missing_ok=True)
    sh(f"{PY} -m pytest -p no:cacheprovider --junitxml={xml} {dst.name}", timeout=300)
    dst.unlink()
    passed = 0
    if xml.exists():
        a = dict(re.findall(r'(tests|failures|errors|skipped)="(\d+)"', xml.read_text()))
        passed = int(a["tests"]) - sum(int(a[k]) for k in ("failures", "errors", "skipped"))
    checks.append(Check("hidden behaviour tests (of 9)", 30 * passed / 9,
                        passed > 0, f"{passed}/9 passed"))
    return checks


GRADERS = {"grant": grade_grant, "module": grade_module}


def grade(task):
    checks = GRADERS[task]()
    task_pts = sum(c.weight for c in checks if c.ok)
    for name, cmd in GATES:
        rc, out = sh(cmd)
        checks.append(Check(f"gate: {name}", 5, rc == 0 and task_pts > 0,
                            "rc=0" if rc == 0 else out[-300:]))
    score = round(sum(c.weight for c in checks if c.ok), 1)
    return score, checks


# ---------------------------------------------------------------- running
def reset_repo():
    subprocess.run("git reset -q --hard && git clean -qfdx -e .venv",
                   shell=True, cwd=REPO, check=True)


def disk_free_gb():
    return shutil.disk_usage("/").free / 1e9


#: macOS swaps to the boot disk; a full disk under memory pressure stalls the
#: whole machine, so no run starts below this and a running one is killed.
DISK_FLOOR_GB = 1.5


def mem_free_pct():
    out = subprocess.run(["sysctl", "-n", "kern.memorystatus_level"],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0)


def write_pi_agent(name, cfg):
    PI_DIR.mkdir(exist_ok=True)
    model = {
        "id": name, "name": name, "reasoning": cfg.thinking != "off",
        "contextWindow": cfg.ctx, "maxTokens": cfg.max_tokens,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "compat": {"thinkingFormat": "qwen-chat-template"},
        "samplingParams": cfg.sampling,
    }
    (PI_DIR / "models.json").write_text(json.dumps({"providers": {"local": {
        "baseUrl": f"http://127.0.0.1:{PORT}/v1", "api": "openai-completions",
        "apiKey": "local", "models": [model]}}}, indent=2))
    (PI_DIR / "settings.json").write_text(json.dumps(
        {"defaultProvider": "local", "defaultModel": name, "quietStartup": True}))


def start_server(cfg, log):
    cmd = [str(SERVER), "-m", str(MODELS / cfg.model), "-c", str(cfg.ctx),
           "-ngl", "999", "--jinja", "--metrics", "--parallel", "1",
           "--host", "127.0.0.1", "--port", str(PORT), *cfg.server_args]
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    t0 = time.time()
    while time.time() - t0 < 300:
        if proc.poll() is not None:
            raise RuntimeError("llama-server exited during load")
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
    vals = dict(re.findall(r"^llamacpp:(\w+) ([\d.e+-]+)$", txt, re.M))
    f = {k: float(v) for k, v in vals.items()}
    pp_s, tg_s = f.get("prompt_seconds_total", 0), f.get("tokens_predicted_seconds_total", 0)
    return {"prompt_tokens": f.get("prompt_tokens_total", 0),
            "gen_tokens": f.get("tokens_predicted_total", 0),
            "pp_tps": round(f.get("prompt_tokens_total", 0) / pp_s, 1) if pp_s else 0,
            "tg_tps": round(f.get("tokens_predicted_total", 0) / tg_s, 1) if tg_s else 0}


def kill(proc):
    if proc and proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)


def summarize_events(path):
    tools = errors = turns = compactions = 0
    for line in path.read_text(errors="ignore").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = e.get("type")
        tools += t == "tool_execution_end"
        errors += t == "tool_execution_end" and bool(e.get("isError"))
        turns += t == "turn_end"
        compactions += t == "compaction_end"
    return {"tool_calls": tools, "tool_errors": errors, "turns": turns,
            "compactions": compactions}


def run_once(name, task, rep):
    cfg = CONFIGS[name]
    if disk_free_gb() < DISK_FLOOR_GB + 1:
        raise SystemExit(f"disk guard: {disk_free_gb():.1f} GB free")
    run_id = f"{time.strftime('%m%d-%H%M%S')}_{name}_{task}_{rep}"
    rd = ROOT / "runs" / run_id
    rd.mkdir(parents=True)
    reset_repo()
    write_pi_agent(name, cfg)
    server_log = open(rd / "server.log", "w")
    server, load_s = start_server(cfg, server_log)
    pi = [shutil.which("pi"), "-p", "--mode", "json", "--provider", "local",
          "--model", name, "--thinking", cfg.thinking, "--session-dir", str(rd),
          "--no-context-files", "--no-extensions", "--no-skills",
          "--no-prompt-templates", "--offline"]
    if cfg.prompt_suffix:
        pi += ["--append-system-prompt", str(ROOT / "prompts" / cfg.prompt_suffix)]
    if cfg.tools:
        pi += ["--tools", cfg.tools]
    pi.append(TASKS[task]["prompt"])
    env = {**os.environ, "PI_CODING_AGENT_DIR": str(PI_DIR), "PI_OFFLINE": "1"}

    low = [100]
    stop = threading.Event()
    disk_abort = threading.Event()

    def sampler():
        while not stop.wait(3):
            low[0] = min(low[0], mem_free_pct())
            if disk_free_gb() < DISK_FLOOR_GB:
                disk_abort.set()
                kill(agent)
    threading.Thread(target=sampler, daemon=True).start()

    t0 = time.time()
    with open(rd / "events.jsonl", "w") as ev:
        agent = subprocess.Popen(pi, cwd=REPO, env=env, stdout=ev,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        try:
            agent.wait(TASKS[task]["timeout"])
            timed_out = False
        except subprocess.TimeoutExpired:
            kill(agent)
            timed_out = True
    wall = time.time() - t0
    stop.set()
    metrics = scrape_metrics()
    kill(server)
    server_log.close()

    sh("git add -A -N . && git diff > /tmp/fi.diff")
    shutil.copy("/tmp/fi.diff", rd / "diff.patch")
    score, checks = grade(task)
    row = {"run": run_id, "config": name, "task": task, "rep": rep,
           "score": score, "wall_s": round(wall), "load_s": round(load_s),
           "timed_out": timed_out, "disk_abort": disk_abort.is_set(), "agent_rc": agent.returncode,
           "min_mem_free_pct": low[0], **metrics,
           **summarize_events(rd / "events.jsonl"),
           "checks": [asdict(c) for c in checks]}
    (rd / "grade.json").write_text(json.dumps(row, indent=2))
    with open(RESULTS, "a") as f:
        f.write(json.dumps(row) + "\n")
    reset_repo()
    print(f"{run_id}: score={score} wall={wall:.0f}s tg={metrics.get('tg_tps')} "
          f"pp={metrics.get('pp_tps')} tools={row['tool_calls']} mem_low={low[0]}%",
          flush=True)
    return row


def report():
    rows = [json.loads(line) for line in RESULTS.read_text().splitlines()]
    by = {}
    for r in rows:
        by.setdefault(r["config"], []).append(r)
    print(f"{'config':<22}{'n':>3}{'mean':>7}{'grant':>7}{'module':>8}"
          f"{'wall_s':>8}{'tg/s':>6}{'pp/s':>7}")
    ranked = sorted(by.items(), key=lambda kv: -sum(r["score"] for r in kv[1]) / len(kv[1]))
    for name, rs in ranked:
        def m(task, key="score"):
            v = [r[key] for r in rs if task in (None, r["task"])]
            return sum(v) / len(v) if v else float("nan")
        print(f"{name:<22}{len(rs):>3}{m(None):>7.1f}{m('grant'):>7.1f}"
              f"{m('module'):>8.1f}{m(None, 'wall_s'):>8.0f}"
              f"{m(None, 'tg_tps'):>6.1f}{m(None, 'pp_tps'):>7.0f}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "run":
        reps = int(sys.argv[sys.argv.index("--reps") + 1]) if "--reps" in sys.argv else 1
        for i in range(reps):
            run_once(sys.argv[2], sys.argv[3], i)
    elif cmd == "grade":
        s, cs = grade(sys.argv[2])
        for c in cs:
            print(f"{'PASS' if c.ok else 'fail'} {c.weight:>5.1f} {c.name} {c.detail[:120]}")
        print("score", s)
    elif cmd == "report":
        report()
