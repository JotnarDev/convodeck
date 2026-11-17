---
name: convodeck
description: >-
  Find, resume, rename, summarize, hide and delete the user's past Claude Code conversations
  (the sessions stored in ~/.claude/projects), and install ConvoDeck — a desktop launcher window
  plus an optional GNOME top-bar menu — so they can reopen any conversation in a terminal with one
  click. Works on any Linux distro, Windows and macOS. Use this skill whenever the user asks about
  their previous or old Claude Code chats/sessions/conversations: "what did we do in the chat about X",
  "list my conversations", "reopen / resume / continue the conversation where we...", "rename that
  session", "delete old chats", "summarize my conversations", "clean up my Claude history", or wants
  a widget, shortcut, menu or launcher for their conversations — in English or Spanish
  ("mis conversaciones", "retomar la conversación", "renombrar el chat", "borrar conversaciones").
---

# ConvoDeck

ConvoDeck turns the raw session files Claude Code keeps in `~/.claude/projects/` (or
`$CLAUDE_CONFIG_DIR/projects`) into something people can use: a list with readable titles,
a ≤20-word summary and the date each conversation was last used, plus actions to resume it in a
new terminal, rename it, hide it or move it to the trash.

Everything goes through one script, `scripts/convodeck.py` (Python 3.8+, standard library only).
Call it with the path relative to this skill's base directory:

- Linux / macOS: `python3 <skill-dir>/scripts/convodeck.py <command>`
- Windows: `py -3 <skill-dir>\scripts\convodeck.py <command>` (or `python` if `py` is missing)

If Python is missing on Windows, offer `winget install Python.Python.3.12` and wait for the user.

Reply in the user's language. The script's own text follows the system language; force it with
`CONVODECK_LANG=es` or `en` if it doesn't match the user.

## Commands

| Command | What it does |
|---|---|
| `list [--limit N] [--search TEXT] [--json] [--hidden]` | Conversations, newest first: short id, last used, folder, title, summary |
| `show ID` | Metadata plus a condensed transcript (user messages + last reply) |
| `open ID` | Opens a new terminal in the conversation's folder running `claude --resume ID` |
| `new [DIR]` | New terminal with a fresh `claude` |
| `rename ID [TITLE]` | Sets the title; no title = back to the automatic one |
| `summary ID TEXT` | Stores a summary you wrote (cut to 20 words) |
| `summarize [--all] [--id ID]` | Writes missing/stale summaries with `claude -p --model haiku` |
| `delete ID --yes` | Moves the session (and its tool-results folder) to the trash / Recycle Bin |
| `hide ID` / `unhide ID` | Hides from lists without deleting |
| `ui` | Opens the ConvoDeck window |
| `install [--no-gnome]` / `uninstall` | Launcher shortcut + `convodeck` command (+ GNOME menu) |
| `doctor` | What was detected: OS, claude path, terminal, browser, GNOME version, counts |

IDs accept any unique prefix of 4+ characters (the 8-character ids `list` prints are enough).

## How to handle common requests

**"What conversations do I have / what did we do about X?"** Run `list` (add `--search X` when
there's a topic) and present the result as a short table: title, one-line summary, folder, last
used. For "what exactly did we do in X", use `show ID` and summarize the transcript yourself —
don't paste it raw; it's long and noisy.

**"Reopen / continue the conversation about X."** Find it with `list --search`, confirm which one if
more than one matches, then `open ID`. A terminal window appears on the user's screen; tell them
that. If the conversation's folder no longer exists, `open` refuses: say so, since Claude Code can
only resume a session from the folder it was started in.

**"Rename it."** `rename ID New title`. Keep titles short (≤ 6 words) and specific; if the user
asks you to pick names, base them on the summary.

**"Summarize my conversations."** You are already running, so for a handful of conversations it is
cheaper and better to read each with `show ID` and store your own sentence with
`summary ID "..."` (≤ 20 words, in the user's language, saying what was done and where it ended).
For many conversations, `summarize` (or `summarize --all` to redo everything) does it in the
background with Haiku — mention that it uses a little of the user's Claude usage. Auto-summaries
skip conversations active in the last 15 minutes, so a running session isn't summarized half-way.

**"Delete these / clean up old chats."** Deleting is the one destructive action here, so list
exactly what will go (titles + dates) and get a clear yes before calling `delete ID --yes` for each.
Mention it goes to the trash and can be restored from there. Don't delete the conversation you are
in right now (it is the newest one, last used a few seconds ago); if they ask, explain why not.
Prefer `hide` when the user only wants a tidier list.

**"Give me a widget / shortcut / menu for my conversations."** Install:

1. Run `doctor` first and read it: it tells you the OS, whether `claude` was found, which terminal
   will be used, whether a Chromium-based browser is available for the app window, and the GNOME
   Shell version (if any).
2. Run `install`. It copies the app to the user's data folder and creates:
   - Linux: `~/.local/bin/convodeck` and an app-menu entry "ConvoDeck"; on GNOME 45+ it also
     installs and enables the top-bar menu (the user must log out and back in on Wayland).
   - Windows: `%LOCALAPPDATA%\convodeck\` with `convodeck.cmd` and a Start Menu shortcut "ConvoDeck".
   - macOS: `~/.local/bin/convodeck` (start the window with `convodeck ui`).
   Use `install --no-gnome` if the user doesn't want the top-bar menu.
3. Tell the user how to open it (app menu / Start Menu → ConvoDeck, or `convodeck ui`) and what
   each part does. Opening the window yourself (`ui`) is fine if they want to see it right away.

If `doctor` shows no terminal on Linux, ask which one they use and set `CONVODECK_TERMINAL`
(supported: gnome-terminal, ptyxis, konsole, xfce4-terminal, mate-terminal, tilix, terminator,
lxterminal, qterminal, kitty, alacritty, wezterm, foot, ghostty, kgx, xterm, x-terminal-emulator) —
or save it permanently in the settings file (below).

## Settings and data

- Titles, summaries and hidden flags: `data.json` in the config folder
  (Linux `~/.config/convodeck/`, Windows `%APPDATA%\convodeck\`, macOS `~/Library/Application Support/convodeck/`).
  Session files are never edited; only `delete` touches them (moving them to the trash).
- Optional `settings.json` in the same folder:
  `{"lang": "es", "terminal": "kitty", "browser": "/usr/bin/chromium", "auto_summaries": false}`.

## The window and the GNOME menu (so you can explain them)

- The window is a small local web page served on `127.0.0.1` with a random token, opened as an app
  window in Edge/Chrome/Chromium/Brave (or the default browser). It has search, folder filters, and
  per-conversation resume, rename and delete (with an inline confirmation). It closes its server a
  few minutes after the window is closed.
- The GNOME menu shows the 3 most recent conversations and a submenu per folder; each row has
  rename and delete buttons with native dialogs, plus "New conversation" and "Open ConvoDeck window".
- Both trigger background summaries at most every 5 minutes (disable with `"auto_summaries": false`).

## Troubleshooting

- *Resume opens but says the session isn't found*: the conversation was started from another folder
  or with another `CLAUDE_CONFIG_DIR`; check `show ID` for its `cwd`.
- *Linux: terminal opens and closes / `claude: not found`*: `claude` lives somewhere only an
  interactive shell knows (nvm, asdf). ConvoDeck already runs `$SHELL -ic`; if it still fails,
  check `doctor` → `claude` path.
- *GNOME menu doesn't appear*: log out and in; then `gnome-extensions info convodeck@jotnardev`.
  GNOME older than 45 isn't supported — the window still works.
- *Windows: nothing happens on resume*: make sure `claude` runs in a normal `cmd` window; ConvoDeck
  uses Windows Terminal if installed, otherwise a classic console.
