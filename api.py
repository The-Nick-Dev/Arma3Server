import os
import re
import subprocess

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


def run_steamcmd(commands, label):
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
    text = "\n".join(output)
    failed_login = re.search(r"FAILED (?:login )?with result code (.+)", text)
    if failed_login:
        code = failed_login.group(1).strip()
        hint = LOGIN_HINTS.get(code.lower(), "")
        raise RuntimeError(f"Steam login failed: {code}. {hint} "
                           "Note: restart policies retry the login in a loop and Steam may rate-limit it.")
    lowered = text.lower()
    if any(re.search(pattern, lowered) for pattern in LOGIN_OK_PATTERNS):
        return text
    # No known confirmation line: only fail when SteamCMD itself failed.
    # A missed confirmation with exit code 0 and no error is continued, because
    # the caller validates the actual work (app/workshop ERROR! lines) anyway.
    if process.returncode:
        raise RuntimeError(f"SteamCMD exited with code {process.returncode} without confirming the login. "
                           "Check the SteamCMD output above.")
    print("WARNING: SteamCMD printed no known login confirmation, but reported no login error; "
          "continuing anyway.", flush=True)
    return text


def install():
    app_update = ["+app_update", ARMA3_SERVER_APP_ID]
    branch = os.environ.get("STEAM_BRANCH") or "public"
    if os.path.basename(os.environ["ARMA_BINARY"]) == "arma3serverprofiling_x64":
        branch = "profiling"
    if branch != "public":
        app_update.extend(["-beta", branch])
    if os.environ.get("STEAM_BRANCH_PASSWORD"):
        app_update.extend(["-betapassword", os.environ["STEAM_BRANCH_PASSWORD"]])
    app_update.append("validate")
    commands = [app_update]
    for cdlc in os.environ["ARMA_CDLC"].split(";"):
        if cdlc:
            cdlc = cdlc.lower()
            print("Downloading CDLC:", cdlc)
            commands.append(["+app_update", CDLC_IDS[cdlc], "validate"])
    text = run_steamcmd(commands, f"install/update Arma 3 server (branch: {branch})")
    failed = re.findall(r"ERROR! Failed to install app '(\d+)' \((.+)\)", text)
    if failed:
        raise RuntimeError("SteamCMD failed to install: " + "; ".join(f"app {app} ({reason})" for app, reason in failed))
    print("Server files up to date.", flush=True)


def download_workshop(ids):
    if not ids:
        return
    commands = [["+workshop_download_item", ARMA3_WORKSHOP_APP_ID, i] for i in ids]
    text = run_steamcmd(commands, f"download {len(ids)} workshop item(s)")
    failed = re.findall(r"ERROR! Download item (\d+) failed", text)
    if failed:
        raise RuntimeError(f"Failed to download workshop items: {', '.join(failed)}. "
                           "The Steam account must own Arma 3 and the items must be public.")
