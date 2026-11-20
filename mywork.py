#!/usr/bin/python3
"""Show current branch and open PRs for each configured directory."""

import json
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import tomllib
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import font as tkfont, messagebox, ttk

CONFIG = Path("~/.config/mywork/config.toml").expanduser()
COMMENTS = CONFIG.parent / "comments.json"
WINDOW = CONFIG.parent / "window.json"
APPROVAL_REPOS = CONFIG.parent / "approval_repos.json"
# The repo list is rescanned outside work hours once it is older than this.
APPROVAL_SCAN_AGE = 20 * 3600
REFRESH_MS = 60_000
COMMENT_CHECK_MS = 2_000
# Rows in these states are refreshed more often, since they change while you work on them.
ACTIVE_REFRESH_MS = 10_000
ACTIVE = {"running", "findings", "failed"}

# Row colors in the order work usually goes through them: (tag, color, label, meaning).
STATES = [
    ("running", "#9ec5fe", "Blue", "Review agent or CI checks are running"),
    ("findings", "#ffb066", "Orange", "A review comment is unresolved"),
    ("failed", "#ff8a8a", "Red", "A CI check failed"),
    ("waiting", "#bdbdbd", "Gray", "Checks pass, review clean, waiting for approval"),
    ("approved", "#8fe39a", "Green", "PR approved"),
    ("merged", "#c9a7ff", "Purple", "PR merged; stays until you switch branch"),
]

USAGE = """usage: mywork.py [comment [TEXT] | add [NAME] | --help]

  (no command)     open the window
  comment [TEXT]   set the comment for the current repo; no TEXT clears it
  add [NAME]       add the current repo to the config
  --help           show this help

Config: ~/.config/mywork/config.toml
  roots, exclude              folders to scan for repos, and repos to skip
  [[repo]] path, name, pinned settings for one repo
  [review_agent] login, started
                              PR comment author and "review started" text of a
                              review bot; enables the review colors
  [[workflow]] url            GitHub API URL listing workflow runs, e.g.
                              repos/OWNER/REPO/actions/workflows/FILE/runs?branch=master&per_page=1;
                              the newest run is shown as a bar on top
  [work_hours] days, start, end
                              e.g. ["Mon", "Tue"], "06:30", "16:00" (local time);
                              outside these, only refresh with the button
  issue_url                   e.g. "https://github.com/OWNER/REPO/issues/{}";
                              links branches named NUMBER-... to that issue
  approval_org                e.g. "OWNER"; lists runs waiting for environment
                              approval in repos of that org, below the bar"""

WORKFLOW_HELP = ("Bar on top: newest workflow run; blue running, green passed, red failed\n"
                 "Gray lines below it: runs waiting for environment approval")


def run(args, cwd):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{args[0]} failed")
    return result.stdout.strip()


# Asks only for what pr_color needs. "gh pr list --json commits" also fetches 100 authors
# for each of 100 commits, which GitHub prices high enough to exhaust the hourly limit.
PR_QUERY = """
query($owner: String!, $repo: String!, $branch: String!) {
  repository(owner: $owner, name: $repo) {
    pullRequests(headRefName: $branch, first: 1, orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes {
        number url state reviewDecision baseRefName
        comments(last: 100) { nodes { author { login } body createdAt } }
        reviewThreads(first: 100) { nodes { isResolved } }
        commits(last: 1) { nodes { commit {
          committedDate
          statusCheckRollup { contexts(first: 100) { nodes {
            ... on StatusContext { state context createdAt }
            ... on CheckRun {
              status conclusion name startedAt
              checkSuite { workflowRun { event workflow { name } } }
            }
          } } }
        } } }
      }
    }
  }
}"""


def newest_checks(checks):
    """Drop checks superseded by a rerun: a commit keeps every run of a check, so a
    failed run would otherwise outlive the rerun that passed."""
    newest = {}
    for check in checks:
        run_ = (check.get("checkSuite") or {}).get("workflowRun") or {}
        key = (run_.get("workflow", {}).get("name"), run_.get("event"),
               check.get("name") or check.get("context"))
        # A run that has not started yet has no time, and is the newest.
        started = check.get("startedAt") or check.get("createdAt") or "9"
        if key not in newest or started >= newest[key][0]:
            newest[key] = (started, check)
    return [check for _, check in newest.values()]


def fetch_prs(branch, path):
    """Return the newest PR from branch, shaped like "gh pr list --json" output.
    Branch names get reused, so older PRs from the same name are left out."""
    out = run(["gh", "api", "graphql", "-f", f"query={PR_QUERY}", "-F", "owner={owner}",
               "-F", "repo={repo}", "-f", f"branch={branch}"], path)
    prs = json.loads(out)["data"]["repository"]["pullRequests"]["nodes"]
    for pr in prs:
        commits = [n["commit"] for n in pr["commits"]["nodes"]]
        rollup = commits[-1]["statusCheckRollup"] if commits else None
        pr["statusCheckRollup"] = newest_checks(rollup["contexts"]["nodes"]) if rollup else []
        pr["comments"] = pr["comments"]["nodes"]
        pr["reviewThreads"] = pr["reviewThreads"]["nodes"]
        pr["commits"] = commits
    return prs


def status(repo):
    """Return (row values, color tag, PR URLs, base branch of the merged PR or None)."""
    name, path, _ = repo
    comment = load_comments().get(str(path), "")
    try:
        branch = run(["git", "branch", "--show-current"], path) or "(detached)"
    except (RuntimeError, FileNotFoundError) as e:
        return (name, "?", str(e), comment), None, [], None
    if branch in ("master", "main"):
        return (name, branch, "–", comment), None, [], None
    try:
        all_prs = fetch_prs(branch, path)
    except RuntimeError as e:
        return (name, branch, f"error: {e}", comment), None, [], None
    prs = [p for p in all_prs if p["state"] == "OPEN"]
    merged = [p for p in all_prs if p["state"] == "MERGED"]
    if not prs and merged:
        pr_text = ", ".join(f"#{p['number']}" for p in merged)
        return ((name, branch, pr_text, comment), "merged", [p["url"] for p in merged],
                merged[0]["baseRefName"])
    pr_text = ", ".join(f"#{p['number']}" for p in prs) or "–"
    return (name, branch, pr_text, comment), pr_color(prs), [p["url"] for p in prs], None


FAILED = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}


FINDINGS = re.compile(r"(\d+) finding\(s\), verdict:")

ISSUE_BRANCH = re.compile(r"(\d+)-")


def load_review_agent():
    """Return the review bot's (login, started text) from the config, or None if not set."""
    try:
        with CONFIG.open("rb") as f:
            agent = tomllib.load(f).get("review_agent")
    except (OSError, ValueError):
        return None
    return (agent["login"], agent["started"]) if agent else None


def review_state(pr, agent):
    """Return "running" or None from the review agent's newest status comment.

    Findings are tracked through their review threads, not the summary comment."""
    if agent is None:
        return None
    login, started = agent
    for c in reversed(pr["comments"]):
        if c["author"]["login"] != login:
            continue
        if started in c["body"]:
            return "running"
        if FINDINGS.search(c["body"]):
            return None
    return None


def pr_color(prs):
    """Return the row tag: failed checks, unresolved review threads, review or checks
    running, approved, waiting for approval, in that order of precedence."""
    # CheckRun entries have status/conclusion; commit StatusContext entries have state.
    checks = [c for p in prs for c in p["statusCheckRollup"]]
    agent = load_review_agent()
    reviews = [review_state(p, agent) for p in prs]
    if any(c.get("conclusion") in FAILED or c.get("state") in FAILED for c in checks):
        return "failed"
    if any(not t["isResolved"] for p in prs for t in p["reviewThreads"]):
        return "findings"
    if "running" in reviews or any(
            c.get("status", "COMPLETED") != "COMPLETED" or c.get("state") in ("PENDING", "EXPECTED")
            for c in checks):
        return "running"
    if any(p["reviewDecision"] == "APPROVED" for p in prs):
        return "approved"
    return "waiting" if prs else None


def load_workflows():
    with CONFIG.open("rb") as f:
        return [w["url"] for w in tomllib.load(f).get("workflow", [])]


def load_issue_url():
    with CONFIG.open("rb") as f:
        return tomllib.load(f).get("issue_url")


def load_approval_org():
    with CONFIG.open("rb") as f:
        return tomllib.load(f).get("approval_org")


def load_approval_repos(org):
    """Return (repos, scan time) from the last scan of org, or ([], 0) if there is none."""
    try:
        cache = json.loads(APPROVAL_REPOS.read_text())
    except (OSError, ValueError):
        return [], 0
    if cache.get("org") != org:
        return [], 0
    return cache["repos"], cache["scanned"]


def needs_approval(repo):
    """Return whether any environment of repo requires a reviewer."""
    try:
        envs = json.loads(run(["gh", "api", f"repos/{repo}/environments"], Path.cwd()))
    except (RuntimeError, ValueError):
        return False  # No access to environments, or none configured.
    return any(rule["type"] == "required_reviewers"
               for env in envs.get("environments", []) for rule in env.get("protection_rules", []))


def scan_approval_repos(org):
    """Save the repos of org with an environment that requires a reviewer."""
    out = run(["gh", "repo", "list", org, "--limit", "1000", "--no-archived",
               "--json", "nameWithOwner", "-q", ".[].nameWithOwner"], Path.cwd())
    names = out.split()
    with ThreadPoolExecutor(max_workers=8) as pool:
        repos = [name for name, needed in zip(names, pool.map(needs_approval, names)) if needed]
    APPROVAL_REPOS.write_text(json.dumps({"org": org, "scanned": time.time(), "repos": repos},
                                         indent=2) + "\n")


def waiting_runs(repo):
    """Return (text, tag, url) for each run in repo waiting for environment approval."""
    try:
        runs = json.loads(run(["gh", "run", "list", "-R", repo, "--status", "waiting", "--json",
                               "url,workflowName,headBranch,displayTitle"], Path.cwd()))
    except (RuntimeError, ValueError) as e:
        return [(f"{repo}: error: {e}", None, None)]
    return [(f"{repo} {r['workflowName']} ({r['headBranch']}): {r['displayTitle']}", "waiting", r["url"])
            for r in runs]


def workflow_status(url):
    """Return (text, tag, url) for the newest run listed by a GitHub API workflow runs URL."""
    try:
        runs = json.loads(run(["gh", "api", url], Path.cwd()))["workflow_runs"]
    except RuntimeError as e:
        return f"{url}: error: {e}", None, None
    if not runs:
        return f"{url}: no runs", None, None
    r = runs[0]
    if r["status"] != "completed":
        tag = "running"
    else:
        tag = "approved" if r["conclusion"] == "success" else "failed"  # green or red
    text = f"{r['repository']['full_name']} {r['name']} ({r['head_branch']})"
    return f"{text}: {r['display_title']}", tag, r["html_url"]


def in_work_hours():
    """Return whether now is inside [work_hours], or True if it is not set."""
    try:
        with CONFIG.open("rb") as f:
            hours = tomllib.load(f).get("work_hours")
        if not hours:
            return True
        # Not strftime("%a"), since Tk may switch the locale and translate day names.
        now = time.localtime()
        day = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][now.tm_wday]
        return day in hours["days"] and hours["start"] <= f"{now.tm_hour:02}:{now.tm_min:02}" < hours["end"]
    except (OSError, ValueError, KeyError):
        return True


def load_dirs():
    if not CONFIG.exists():
        sys.exit(f"Missing config: {CONFIG}")
    with CONFIG.open("rb") as f:
        config = tomllib.load(f)
    dirs = [(r.get("name", r["path"]), Path(r["path"]).expanduser().resolve(), r.get("pinned", False))
            for r in config.get("repo", [])]
    # Every git repo directly inside a root, unless excluded or already listed above.
    skip = {path for _, path, _ in dirs}
    skip |= {Path(p).expanduser().resolve() for p in config.get("exclude", [])}
    found = {git.parent.resolve() for root in config.get("roots", [])
             for git in Path(root).expanduser().glob("*/.git")}
    dirs += [("/".join(path.parts[-2:]), path, False) for path in sorted(found - skip)]
    return sorted(dirs, key=lambda d: not d[2])


def load_comments():
    if not COMMENTS.exists():
        return {}
    return json.loads(COMMENTS.read_text())


def repo_root():
    try:
        return Path(run(["git", "rev-parse", "--show-toplevel"], Path.cwd())).resolve()
    except RuntimeError:
        sys.exit("Not in a git repository")


def add_repo(name):
    top = repo_root()
    if CONFIG.exists() and top in [path for _, path, _ in load_dirs()]:
        sys.exit(f"{top} is already in {CONFIG}")
    name = name or "/".join(top.parts[-2:])
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    text = CONFIG.read_text() if CONFIG.exists() else ""
    sep = "" if not text else "\n" if text.endswith("\n") else "\n\n"
    # json.dumps output is a valid TOML basic string.
    with CONFIG.open("a") as f:
        f.write(f"{sep}[[repo]]\npath = {json.dumps(str(top))}\nname = {json.dumps(name)}\n")
    print(f"Added {top} as {name}")


def set_comment(text):
    top = repo_root()
    if top not in [path for _, path, _ in load_dirs()]:
        sys.exit(f"{top} is not in {CONFIG}")
    comments = load_comments()
    if text:
        comments[str(top)] = text
    else:
        comments.pop(str(top), None)
    COMMENTS.write_text(json.dumps(comments, indent=2) + "\n")


class App:
    def __init__(self, root, dirs):
        self.root = root
        self.dirs = dirs
        root.title("mywork")
        icon = Path(__file__).resolve().with_name("mywork.png")
        if icon.exists():
            root.iconphoto(True, tk.PhotoImage(file=icon))
        menubar = tk.Menu(root)
        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="Colors", command=self._show_colors)
        menubar.add_cascade(label="Help", menu=help_menu)
        root.configure(menu=menubar)

        self.bar = ttk.Frame(root)
        self.bar.pack(fill="x")
        self.workflows = []
        self.runs = []
        self.approvals = []
        self.approval_org = None
        self.scanning = False

        self.tree = ttk.Treeview(root, columns=("dir", "branch", "prs", "comment"), show="headings")
        for col, label, width in (("dir", "Directory", 150), ("branch", "Branch", 300),
                                  ("prs", "PRs", 100), ("comment", "Comment", 300)):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width)
        for tag, color, _, _ in STATES:
            self.tree.tag_configure(tag, background=color)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<Motion>", self._on_motion)
        self.tree.bind("<Leave>", lambda _: self._hide_tip())
        self.tip = self.tip_job = self.tip_cell = None
        self.urls = {}
        self.bases = {}
        self.issue_url = None

        buttons = ttk.Frame(root)
        buttons.pack(pady=4)
        self.button = ttk.Button(buttons, text="Refresh", command=self.refresh)
        self.button.pack(side="left", padx=2)
        self.show_all = False
        self.toggle = ttk.Button(buttons, text="Show all", command=self._toggle_hidden)
        self.toggle.pack(side="left", padx=2)
        self.last = ([], [])
        self.fetching = False
        self.comments_mtime = None
        self.refresh()
        self.root.after(REFRESH_MS, self._auto_refresh)
        self.root.after(ACTIVE_REFRESH_MS, self._refresh_active)
        self._check_comments()

    def _auto_refresh(self):
        self._maybe_scan()
        if in_work_hours():
            self.refresh()
        self.root.after(REFRESH_MS, self._auto_refresh)

    def _refresh_active(self):
        dirs, rows = self.last
        active = [d for d, (_, color, _, _) in zip(dirs, rows) if color in ACTIVE]
        workflows = self.workflows if any(tag == "running" for _, tag, _ in self.runs) else None
        if (active or workflows) and not self.fetching and in_work_hours():
            self.fetching = True
            threading.Thread(target=self._fetch_active, args=(active, workflows),
                             daemon=True).start()
        self.root.after(ACTIVE_REFRESH_MS, self._refresh_active)

    def _fetch_active(self, active, workflows):
        with ThreadPoolExecutor() as pool:
            runs = pool.map(workflow_status, workflows) if workflows else None
            updated = dict(zip((path for _, path, _ in active), pool.map(status, active)))
            runs = list(runs) if runs else None
        self.root.after(0, self._show_active, updated, runs)

    def _show_active(self, updated, runs):
        dirs, rows = self.last
        self._show(dirs, [updated.get(path, row) for (_, path, _), row in zip(dirs, rows)], runs)

    def _maybe_scan(self):
        """Scan the org for repos needing approval if there is no scan yet, or at night
        once the last scan is old."""
        org = self.approval_org
        if not org or self.scanning:
            return
        _, scanned = load_approval_repos(org)
        if scanned and (time.time() - scanned < APPROVAL_SCAN_AGE or in_work_hours()):
            return
        self.scanning = True
        threading.Thread(target=self._scan, args=(org,), daemon=True).start()

    def _scan(self, org):
        try:
            scan_approval_repos(org)
        except RuntimeError:
            pass  # Retried on the next refresh.
        self.scanning = False
        self.root.after(0, self.refresh)

    def _check_comments(self):
        mtime = COMMENTS.stat().st_mtime if COMMENTS.exists() else None
        if mtime != self.comments_mtime:
            self.comments_mtime = mtime
            comments = load_comments()
            for iid in self.tree.get_children():
                self.tree.set(iid, "comment", comments.get(iid, ""))
        self.root.after(COMMENT_CHECK_MS, self._check_comments)

    def refresh(self):
        if self.fetching:
            return
        self.fetching = True
        self.button.state(["disabled"])
        try:
            self.dirs = load_dirs()
            self.workflows = load_workflows()
            self.issue_url = load_issue_url()
            self.approval_org = load_approval_org()
        except (OSError, ValueError, KeyError):
            pass  # Keep the last good config while the file is broken or mid-edit.
        self._maybe_scan()
        approval_repos = load_approval_repos(self.approval_org)[0] if self.approval_org else []
        threading.Thread(target=self._fetch, args=(self.dirs, self.workflows, approval_repos),
                         daemon=True).start()

    def _fetch(self, dirs, workflows, approval_repos):
        with ThreadPoolExecutor() as pool:
            runs = pool.map(workflow_status, workflows)
            waiting = pool.map(waiting_runs, approval_repos)
            rows = list(pool.map(status, dirs))
            runs = list(runs)
            approvals = [line for lines in waiting for line in lines]
        self.root.after(0, self._show, dirs, rows, runs, approvals)

    def _show_colors(self):
        win = tk.Toplevel(self.root)
        win.title("mywork colors")
        for i, (_, color, label, meaning) in enumerate(STATES):
            tk.Label(win, text="      ", background=color, relief="solid", borderwidth=1).grid(
                row=i, column=0, padx=6, pady=2)
            tk.Label(win, text=label).grid(row=i, column=1, sticky="w")
            tk.Label(win, text=meaning).grid(row=i, column=2, sticky="w", padx=6)
        tk.Label(win, text="No color: no PR, or on master/main", foreground="#555").grid(
            row=len(STATES), column=0, columnspan=3, sticky="w", padx=6, pady=(6, 0))
        tk.Label(win, text=WORKFLOW_HELP, foreground="#555", justify="left").grid(
            row=len(STATES) + 1, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 6))

    def _toggle_hidden(self):
        self.show_all = not self.show_all
        self.toggle.configure(text="Hide idle" if self.show_all else "Show all")
        self._render()

    def _show(self, dirs, rows, runs=None, approvals=None):
        self.last = (dirs, rows)
        if approvals is not None:
            self.approvals = approvals
        if runs is not None:
            self.runs = runs
            self._render_bar()
        self._render()
        self.fetching = False
        self.button.state(["!disabled"])

    def _render_bar(self):
        colors = {tag: color for tag, color, _, _ in STATES}
        for child in self.bar.winfo_children():
            child.destroy()
        lines = list(self.runs)
        if self.approvals:
            lines += [None] + self.approvals  # None marks the separator.
        for line in lines:
            if line is None:
                ttk.Separator(self.bar).pack(fill="x", pady=3)
                continue
            text, tag, url = line
            label = tk.Label(self.bar, text=text, anchor="w", padx=6,
                             background=colors.get(tag, self.bar.winfo_toplevel()["background"]))
            label.pack(fill="x")
            label.bind("<Enter>", lambda e, label=label: self._on_bar_enter(label, e))
            label.bind("<Leave>", lambda _: self._hide_tip())
            if url:
                label.configure(cursor="hand2")
                label.bind("<Button-1>", lambda _, url=url: webbrowser.open(url))

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        self.urls = {}
        self.bases = {}
        for (_, path, pinned), (row, color, urls, base) in zip(*self.last):
            if not (pinned or self.show_all) and row[1] in ("master", "main"):
                continue
            tags = (color,) if color else ()
            self.tree.insert("", "end", iid=str(path), values=row, tags=tags)
            self.urls[str(path)] = urls
            self.bases[str(path)] = base

    def _urls_at(self, event):
        """Return the URLs the cell under the pointer links to: its PRs, or the issue
        of a branch named "<number>-..."."""
        iid, col = self.tree.identify_row(event.y), self.tree.identify_column(event.x)
        if col == "#3":  # PRs column
            return self.urls.get(iid, [])
        if col == "#2" and iid and self.issue_url:  # Branch column
            if m := ISSUE_BRANCH.match(self.tree.set(iid, "branch")):
                return [self.issue_url.format(m[1])]
        return []

    def _on_click(self, event):
        for url in self._urls_at(event):
            webbrowser.open(url)

    def _on_right_click(self, event):
        """On a merged row, offer to check out the branch the PR merged into."""
        iid = self.tree.identify_row(event.y)
        base = self.bases.get(iid)
        if not base:
            return
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label=f"Set to {base}", command=lambda: threading.Thread(
            target=self._set_branch, args=(iid, base), daemon=True).start())
        menu.tk_popup(event.x_root, event.y_root)

    def _set_branch(self, path, base):
        try:
            run(["git", "checkout", base], path)
            run(["git", "pull"], path)
        except RuntimeError as e:
            self.root.after(0, messagebox.showerror, "mywork", f"{path}\n\n{e}")
        self.root.after(0, self.refresh)

    def _on_motion(self, event):
        self.tree.configure(cursor="hand2" if self._urls_at(event) else "")
        cell = (self.tree.identify_row(event.y), self.tree.identify_column(event.x))
        if cell != self.tip_cell:
            self._hide_tip()
            self.tip_cell = cell
            if cell[0]:
                self.tip_job = self.root.after(500, self._show_tip, event.x_root, event.y_root)

    def _show_tip(self, x, y):
        """Show the full cell text near the pointer, if the column cuts it off."""
        self.tip_job = None
        iid, col = self.tip_cell
        if not self.tree.exists(iid):
            return
        text = str(self.tree.set(iid, col))
        padding = 12
        if tkfont.nametofont("TkDefaultFont").measure(text) + padding <= self.tree.column(col, "width"):
            return
        self._open_tip(text, x, y)

    def _on_bar_enter(self, label, event):
        self._hide_tip()
        self.tip_job = self.root.after(500, self._show_bar_tip, label, event.x_root, event.y_root)

    def _show_bar_tip(self, label, x, y):
        """Show the full bar line near the pointer, if the window cuts it off."""
        self.tip_job = None
        text = label["text"]
        padding = 2 * int(label["padx"])
        if tkfont.Font(font=label["font"]).measure(text) + padding <= label.winfo_width():
            return
        self._open_tip(text, x, y)

    def _open_tip(self, text, x, y):
        self.tip = tk.Toplevel(self.root)
        self.tip.overrideredirect(True)
        self.tip.geometry(f"+{x + 12}+{y + 12}")
        tk.Label(self.tip, text=text, background="#ffffe0", relief="solid", borderwidth=1,
                 wraplength=600, justify="left").pack()

    def _hide_tip(self):
        if self.tip_job:
            self.root.after_cancel(self.tip_job)
            self.tip_job = None
        if self.tip:
            self.tip.destroy()
            self.tip = None
        self.tip_cell = None


def print_help():
    print(USAGE)
    print("\nRow colors:")
    for _, color, label, meaning in STATES:
        r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
        print(f"  \033[48;2;{r};{g};{b}m    \033[0m {label:<11} {meaning}")
    print("  No color: no PR, or on master/main")
    print(f"\n{WORKFLOW_HELP}")


def main():
    if len(sys.argv) > 1:
        cmd, text = sys.argv[1], " ".join(sys.argv[2:])
        if cmd in ("-h", "--help", "help"):
            print_help()
        elif cmd == "comment":
            set_comment(text)
        elif cmd == "add":
            add_repo(text)
        else:
            sys.exit(USAGE)
        return
    dirs = load_dirs()
    root = tk.Tk()
    app = App(root, dirs)
    saved = json.loads(WINDOW.read_text()) if WINDOW.exists() else {}
    # Tk reports the position of the window contents but places the title bar
    # there, so measure the title bar height to keep the window from creeping down.
    title_bar = 0
    if "geometry" in saved:
        root.geometry(saved["geometry"])
        requested_y = int(saved["geometry"].rsplit("+", 1)[1])

        def measure_title_bar():
            nonlocal title_bar
            title_bar = max(root.winfo_y() - requested_y, 0)

        root.after(1000, measure_title_bar)
    for col, width in saved.get("columns", {}).items():
        if col in app.tree["columns"]:
            app.tree.column(col, width=width)

    def on_close():
        columns = {col: app.tree.column(col, "width") for col in app.tree["columns"]}
        geometry = (f"{root.winfo_width()}x{root.winfo_height()}"
                    f"+{root.winfo_x()}+{root.winfo_y() - title_bar}")
        WINDOW.write_text(json.dumps({"geometry": geometry, "columns": columns}, indent=2) + "\n")
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
