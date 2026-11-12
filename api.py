import os
import re
import subprocess
import threading

ARMA3_SERVER_APP_ID = "233780"
ARMA3_WORKSHOP_APP_ID = "107410"
STEAMCMD = "/steamcmd/steamcmd.sh"
INSTALL_DIR = "/arma3/server"

CDLC_IDS = {
    "csla": "233793",
    "gm": "233792",
    "vn": "233794",
    "ws": "233795",
    "spe": "233788",
    "rf": "233799",
    "ef": "233798"
}

# Lines that confirm a successful login, depending on the SteamCMD version.
# Current SteamCMD prints "Logging in user '<name>' [U:1:...] to Steam Public...OK"
# and "Waiting for user info...OK"; older versions print "Logged in OK".
# Matched case-insensitively against the whole SteamCMD output.
LOGIN_OK_PATTERNS = [
    r"logged in ok",
    r"already logged in",
    r"logging in user '.+' \[u:\d+:\d+\] to steam public\.\.\.ok",
    r"waiting for user info\.\.\.ok",
]

LOGIN_HINTS = {
    "invalid password": "Wrong user or password. In .env use STEAM_PASSWORD=value without quotes and double any $ ($$).",
    "account logon denied": "Steam Guard: a code was sent to the account email. The container cannot enter it; use an account without Steam Guard.",
    "need twofactor": "Steam Guard mobile authenticator is required. Use an account without Steam Guard.",
    "two-factor code mismatch": "Steam Guard code rejected.",
    "rate limit exceeded": "Too many login attempts. Stop the container and wait 30-60 minutes before retrying.",
    "no connection": "Could not reach Steam. Check the network/DNS of the host.",
    "service unavailable": "Steam is unavailable. Try again later.",
    "timeout": "Steam did not answer in time. Try again later.",
    "banned": "The account is banned.",
    "account not found": "The account does not exist. Use the login name, not the profile name.",
}


def folder_size(path):
    total = 0
    for folder, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total


def heartbeat(stop, interval=30):
    """Every `interval` seconds print the disk used by the workshop folders.

    SteamCMD prints nothing until each item finishes; this only shows that
    files are still growing. It is not a percentage and does not check the network.
    """
    workshop = os.path.join(INSTALL_DIR, "steamapps/workshop")
    while not stop.wait(interval):
        downloads = folder_size(os.path.join(workshop, "downloads"))
        content = folder_size(os.path.join(workshop, "content", ARMA3_WORKSHOP_APP_ID))
        print("Workshop disk use: downloads {:.2f} GB, content {:.2f} GB".format(
            downloads / 1e9, content / 1e9), flush=True)


def run_steamcmd(commands, label, show_disk_use=False):
    user = os.environ["STEAM_USER"]
    password = os.environ["STEAM_PASSWORD"]
    print(f"SteamCMD: {label}", flush=True)
    print(f"Logging in to Steam as '{user}' (password: {'set' if password else 'EMPTY'})...", flush=True)
    if password[:1] in ("'", '"') and password[-1:] == password[:1]:
        print("WARNING: STEAM_PASSWORD starts and ends with a quote; quotes in .env are part of the value.")
    args = [STEAMCMD, "+force_install_dir", INSTALL_DIR, "+login", user, password]
    for command in commands:
        args.extend(command)
    args.append("+quit")
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, text=True, errors="replace")
    stop = threading.Event()
    if show_disk_use:
        threading.Thread(target=heartbeat, args=(stop,), daemon=True).start()
    output = []
    segment = ""
    while True:
        char = process.stdout.read(1)
        if char == "":
            break
        if char in ("\n", "\r"):
            if segment.strip():
                print(segment, flush=True)
                output.append(segment)
            segment = ""
        else:
            segment += char
    if segment.strip():
        print(segment, flush=True)
        output.append(segment)
    process.stdout.close()
    process.wait()
    stop.set()
    text = "\n".join(output)
    failed_login = re.search(r"FAILED (?:login )?with result code (.+)", text)
    if failed_login:
        code = failed_login.group(1).strip()
        hint = LOGIN_HINTS.get(code.lower(), "")
        raise RuntimeError(f"Steam login failed: {code}. {hint} "
                           "Note: restart policies retry the login in a loop and Steam may rate-limit it.")
    # A successful login does not mean app/workshop commands succeeded.
    # Check command errors and exit status before accepting login confirmation.
    timeouts = re.findall(r"ERROR!\s*Timeout downloading item (\d+)", text, re.IGNORECASE)
    if timeouts:
        raise RuntimeError("SteamCMD Workshop download timed out for item(s): "
                           + ", ".join(dict.fromkeys(timeouts))
                           + f" (exit code {process.returncode}). Login is not the cause indicated by this error. "
                           "Download is not confirmed complete. Stop the container before retrying "
                           "if its restart policy repeats this failure.")
    errors = re.findall(r"ERROR![^\r\n]*", text, re.IGNORECASE)
    if errors:
        raise RuntimeError(f"SteamCMD failed during {label} (exit code {process.returncode}): "
                           + "; ".join(errors))
    if process.returncode:
        raise RuntimeError(f"SteamCMD exited with code {process.returncode} during {label}. "
                           "Check the SteamCMD output above; this alone does not establish a login failure.")
    # Require explicit completion for each requested Workshop ID. Successes can
    # share an output line, and exit code 0 alone does not confirm every item.
    requested = [str(command[2]) for command in commands
                 if len(command) >= 3 and command[0] == "+workshop_download_item"]
    completed = set(re.findall(r"Success\. Downloaded item (\d+)\b", text, re.IGNORECASE))
    missing = [item for item in requested if item not in completed]
    if missing:
        raise RuntimeError("SteamCMD did not confirm download completion for workshop item(s): "
                           + ", ".join(missing) + ". Refusing to launch with unconfirmed mods.")
    lowered = text.lower()
    if not any(re.search(pattern, lowered) for pattern in LOGIN_OK_PATTERNS):
        print("WARNING: SteamCMD printed no known login confirmation, but reported no error; "
              "checking command results.", flush=True)
    return text


def install():
    validate = os.environ.get("ARMA_VALIDATE", "false").strip().lower()
    if validate not in ("true", "false"):
        raise ValueError("ARMA_VALIDATE must be true or false.")
    app_update = ["+app_update", ARMA3_SERVER_APP_ID]
    branch = os.environ.get("STEAM_BRANCH") or "public"
    if os.path.basename(os.environ["ARMA_BINARY"]) == "arma3serverprofiling_x64":
        branch = "profiling"
    if branch != "public":
        app_update.extend(["-beta", branch])
    if os.environ.get("STEAM_BRANCH_PASSWORD"):
        app_update.extend(["-betapassword", os.environ["STEAM_BRANCH_PASSWORD"]])
    if validate == "true":
        app_update.append("validate")
    commands = [app_update]
    for cdlc in os.environ["ARMA_CDLC"].split(";"):
        if cdlc:
            cdlc = cdlc.lower()
            print("Downloading CDLC:", cdlc)
            commands.append(["+app_update", CDLC_IDS[cdlc], "validate"])
    text = run_steamcmd(commands, f"install/update Arma 3 server (branch: {branch}, validate: {validate})")
    failed = re.findall(r"ERROR! Failed to install app '(\d+)' \((.+)\)", text)
    if failed:
        raise RuntimeError("SteamCMD failed to install: " + "; ".join(f"app {app} ({reason})" for app, reason in failed))
    print("Server files up to date.", flush=True)


def download_workshop(ids):
    if not ids:
        return
    commands = [["+workshop_download_item", ARMA3_WORKSHOP_APP_ID, i] for i in ids]
    text = run_steamcmd(commands, f"download {len(ids)} workshop item(s)", show_disk_use=True)
    failed = re.findall(r"ERROR! Download item (\d+) failed", text)
    if failed:
        raise RuntimeError(f"Failed to download workshop items: {', '.join(failed)}. "
                           "The Steam account must own Arma 3 and the items must be public.")
