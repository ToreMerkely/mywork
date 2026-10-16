#!/usr/bin/python3
"""Show current branch and open PRs for each configured directory."""

import json
import re
import subprocess
import sys
import threading
import tkinter as tk
import tomllib
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import font as tkfont, ttk

CONFIG = Path("~/.config/mywork/config.toml").expanduser()
COMMENTS = CONFIG.parent / "comments.json"
WINDOW = CONFIG.parent / "window.json"
REFRESH_MS = 60_000
COMMENT_CHECK_MS = 2_000
# Rows in these states are refreshed more often, since they change while you work on them.
ACTIVE_REFRESH_MS = 10_000
ACTIVE = {"running", "findings", "failed"}

# Row colors in the order work usually goes through them: (tag, color, label, meaning).
STATES = [
    ("running", "#9ec5fe", "Blue", "Review agent or CI checks are running"),
    ("findings", "#ffb066", "Orange", "Review agent reported findings"),
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
                              review bot; enables the review colors"""


def run(args, cwd):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{args[0]} failed")
    return result.stdout.strip()


def status(repo):
    name, path, _ = repo
    comment = load_comments().get(str(path), "")
    try:
        branch = run(["git", "branch", "--show-current"], path) or "(detached)"
    except (RuntimeError, FileNotFoundError) as e:
        return (name, "?", str(e), comment), None, []
    if branch in ("master", "main"):
        return (name, branch, "–", comment), None, []
    try:
        out = run(["gh", "pr", "list", "--head", branch, "--state", "all",
                   "--json", "number,url,state,reviewDecision,statusCheckRollup,comments,commits"], path)
        all_prs = json.loads(out)
    except RuntimeError as e:
        return (name, branch, f"error: {e}", comment), None, []
    prs = [p for p in all_prs if p["state"] == "OPEN"]
    merged = [p for p in all_prs if p["state"] == "MERGED"]
    if not prs and merged:
        pr_text = ", ".join(f"#{p['number']}" for p in merged)
        return (name, branch, pr_text, comment), "merged", [p["url"] for p in merged]
    pr_text = ", ".join(f"#{p['number']}" for p in prs) or "–"
    return (name, branch, pr_text, comment), pr_color(prs), [p["url"] for p in prs]


FAILED = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}


FINDINGS = re.compile(r"(\d+) finding\(s\), verdict:")


def load_review_agent():
    """Return the review bot's (login, started text) from the config, or None if not set."""
    try:
        with CONFIG.open("rb") as f:
            agent = tomllib.load(f).get("review_agent")
    except (OSError, ValueError):
        return None
    return (agent["login"], agent["started"]) if agent else None


def review_state(pr, agent):
    """Return "running", "findings" or None from the review agent's newest status comment.

    Findings posted before the latest commit are treated as addressed."""
    if agent is None:
        return None
    login, started = agent
    last_commit = pr["commits"][-1]["committedDate"] if pr["commits"] else ""
    for c in reversed(pr["comments"]):
        if c["author"]["login"] != login:
            continue
        if started in c["body"]:
            return "running"
        if m := FINDINGS.search(c["body"]):
            # Both timestamps are UTC ISO 8601, so string comparison orders them.
            return "findings" if int(m[1]) > 0 and c["createdAt"] > last_commit else None
    return None


def pr_color(prs):
    """Return the row tag: failed checks, review findings, review or checks
    running, approved, waiting for approval, in that order of precedence."""
    # CheckRun entries have status/conclusion; commit StatusContext entries have state.
    checks = [c for p in prs for c in p["statusCheckRollup"]]
    agent = load_review_agent()
    reviews = [review_state(p, agent) for p in prs]
    if any(c.get("conclusion") in FAILED or c.get("state") in FAILED for c in checks):
        return "failed"
    if "findings" in reviews:
        return "findings"
    if "running" in reviews or any(
            c.get("status", "COMPLETED") != "COMPLETED" or c.get("state") in ("PENDING", "EXPECTED")
            for c in checks):
        return "running"
    if any(p["reviewDecision"] == "APPROVED" for p in prs):
        return "approved"
    return "waiting" if prs else None


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
        menubar = tk.Menu(root)
        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="Colors", command=self._show_colors)
        menubar.add_cascade(label="Help", menu=help_menu)
        root.configure(menu=menubar)

        self.tree = ttk.Treeview(root, columns=("dir", "branch", "prs", "comment"), show="headings")
        for col, label, width in (("dir", "Directory", 150), ("branch", "Branch", 300),
                                  ("prs", "PRs", 100), ("comment", "Comment", 300)):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width)
        for tag, color, _, _ in STATES:
            self.tree.tag_configure(tag, background=color)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Motion>", self._on_motion)
        self.tree.bind("<Leave>", lambda _: self._hide_tip())
        self.tip = self.tip_job = self.tip_cell = None
        self.urls = {}

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
        self._auto_refresh()
        self.root.after(ACTIVE_REFRESH_MS, self._refresh_active)
        self._check_comments()

    def _auto_refresh(self):
        self.refresh()
        self.root.after(REFRESH_MS, self._auto_refresh)

    def _refresh_active(self):
        dirs, rows = self.last
        active = [d for d, (_, color, _) in zip(dirs, rows) if color in ACTIVE]
        if active and not self.fetching:
            self.fetching = True
            threading.Thread(target=self._fetch_active, args=(active,), daemon=True).start()
        self.root.after(ACTIVE_REFRESH_MS, self._refresh_active)

    def _fetch_active(self, active):
        with ThreadPoolExecutor() as pool:
            updated = dict(zip((path for _, path, _ in active), pool.map(status, active)))
        self.root.after(0, self._show_active, updated)

    def _show_active(self, updated):
        dirs, rows = self.last
        self._show(dirs, [updated.get(path, row) for (_, path, _), row in zip(dirs, rows)])

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
        except (OSError, ValueError, KeyError):
            pass  # Keep the last good config while the file is broken or mid-edit.
        threading.Thread(target=self._fetch, args=(self.dirs,), daemon=True).start()

    def _fetch(self, dirs):
        with ThreadPoolExecutor() as pool:
            rows = list(pool.map(status, dirs))
        self.root.after(0, self._show, dirs, rows)

    def _show_colors(self):
        win = tk.Toplevel(self.root)
        win.title("mywork colors")
        for i, (_, color, label, meaning) in enumerate(STATES):
            tk.Label(win, text="      ", background=color, relief="solid", borderwidth=1).grid(
                row=i, column=0, padx=6, pady=2)
            tk.Label(win, text=label).grid(row=i, column=1, sticky="w")
            tk.Label(win, text=meaning).grid(row=i, column=2, sticky="w", padx=6)
        tk.Label(win, text="No color: no PR, or on master/main", foreground="#555").grid(
            row=len(STATES), column=0, columnspan=3, sticky="w", padx=6, pady=(6, 6))

    def _toggle_hidden(self):
        self.show_all = not self.show_all
        self.toggle.configure(text="Hide idle" if self.show_all else "Show all")
        self._render()

    def _show(self, dirs, rows):
        self.last = (dirs, rows)
        self._render()
        self.fetching = False
        self.button.state(["!disabled"])

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        self.urls = {}
        for (_, path, pinned), (row, color, urls) in zip(*self.last):
            if not (pinned or self.show_all) and row[1] in ("master", "main"):
                continue
            tags = (color,) if color else ()
            self.tree.insert("", "end", iid=str(path), values=row, tags=tags)
            self.urls[str(path)] = urls

    def _pr_urls_at(self, event):
        if self.tree.identify_column(event.x) != "#3":  # PRs column
            return []
        return self.urls.get(self.tree.identify_row(event.y), [])

    def _on_click(self, event):
        for url in self._pr_urls_at(event):
            webbrowser.open(url)

    def _on_motion(self, event):
        self.tree.configure(cursor="hand2" if self._pr_urls_at(event) else "")
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
