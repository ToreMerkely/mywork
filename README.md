# mywork

A small window that lists your local git repos with their current branch and open PRs, colored by CI and review state.

## Requirements

- Python 3.11+ with Tk (`sudo apt install python3-tk`)
- `git`
- [`gh`](https://cli.github.com/), logged in with `gh auth login`

## Install

```
git clone git@github.com:ToreMerkely/mywork.git
cd mywork
make install
```

This links `mywork` into `~/.local/bin` and adds mywork with its icon to the application menu. `~/.local/bin` must be on your `PATH`. Remove it all with `make uninstall`.

## Configure

Create `~/.config/mywork/config.toml`:

```toml
roots = ["~/src"]            # every git repo directly inside these folders
exclude = ["~/src/old-repo"] # repos to skip

[[repo]]                     # a repo outside the roots, or one to pin to the top
path = "~/work/server"
name = "server"
pinned = true
```

Or run `mywork add` inside a repo to append it to the config.

Optional sections:

```toml
[review_agent]               # a review bot whose PR comments set the review colors
login = "my-review-bot"
started = "Review started"

[[workflow]]                 # newest run is shown as a bar on top
url = "repos/OWNER/REPO/actions/workflows/ci.yml/runs?branch=main&per_page=1"

[work_hours]                 # outside these, only refresh with the button
days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
start = "08:00"
end = "16:00"
```

## Use

```
mywork                  open the window
mywork comment [TEXT]   set a comment for the current repo; no TEXT clears it
mywork add [NAME]       add the current repo to the config
mywork --help           show help
```

Click a PR number to open it in the browser. Help → Colors explains the row colors.
