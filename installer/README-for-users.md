# ProjectZen — Installation & First Run

## What you need

- A Windows PC
- A **Claude Enterprise** account

That's all. ProjectZen brings its own Python and its own Claude CLI — you do **not**
need to install Python, Node.js, npm, or anything else. It never asks for an API key.

## Install

1. Double-click **`ProjectZen-Setup.exe`**.
2. Accept the default location (`…\AppData\Local\Programs\ProjectZen`).
   No administrator password is required.
3. Leave **"Create a desktop shortcut"** ticked.
4. Finish. A **ProjectZen** icon appears on your Desktop and in the Start Menu.

> If Windows shows *"Windows protected your PC"*, click **More info → Run anyway**.
> This appears because the installer is not code-signed; it is expected.

## First run

Double-click the **ProjectZen** icon. A console window opens and works through:

```
==> Locating Python              (bundled — nothing to install)
==> Checking dependencies        (bundled — nothing to download)
==> Locating the Claude CLI      (bundled — nothing to install)
==> Checking your Claude sign-in
==> Starting the ProjectZen server
==> Opening ProjectZen
```

**The one manual step is the sign-in.** On first run only, you'll see:

```
    Not signed in yet - this is a one-time step.
      A browser window will open. Sign in with your Claude Enterprise account.
      Press Enter to open the sign-in page...
```

Press Enter, sign in with your normal work account, and return to the window.
Every later run skips this and goes straight to the app.

## Everyday use

Double-click the icon → the app opens in your browser.

**Keep the console window open** while you work; it runs the server. Press any key
in it to stop ProjectZen.

## Where your files go

| What | Where |
|---|---|
| Generated documents | `%LOCALAPPDATA%\ProjectZen\storage\document_store` |
| Your library database | `%LOCALAPPDATA%\ProjectZen\storage\documents.db` |
| The program itself | `%LOCALAPPDATA%\Programs\ProjectZen` |

Uninstalling removes the program but **keeps your documents**.

## Generation takes a while — that's normal

A full delivery document runs **25–50 minutes**. The progress panel shows which
stage is running, what percentage is complete, and an estimated time remaining.
The longest stage ("Authoring the file") reports each step as it builds your
workbook. It is working, not stuck.

## If something goes wrong

The console window prints the reason. The two most common:

**"Not signed in"** — run the icon again and complete the browser sign-in.

**"No Claude CLI was found"** — the installation is damaged. Re-run the installer.

For anything else, open this address in your browser while ProjectZen is running
(the port is shown in the console) and send the result to your administrator:

```
http://127.0.0.1:<port>/health/llm
```

It reports exactly which part of the setup is unhealthy.
