"""One SteamCMD session, one request per item, completion from fresh job events."""
import codecs
import os
import queue
import re
import signal
import subprocess
import threading
import time


class JobLog:
    def __init__(self, path):
        self.path = path
        self.identity = None
        self.offset = 0
        self.partial = ""
        self.tail = b""
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            return
        self.identity = (stat.st_dev, stat.st_ino)
        self.offset = stat.st_size  # Never accept results left by an earlier run.
        with open(path, "rb") as source:
            source.seek(max(0, self.offset - 64))
            self.tail = source.read()

    def lines(self):
        try:
            with open(self.path, "rb") as source:
                stat = os.fstat(source.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if self.identity is not None and (identity != self.identity or stat.st_size < self.offset):
                    raise RuntimeError("Workshop log rotated or truncated; completion cannot be verified.")
                self.identity = identity
                source.seek(max(0, self.offset - len(self.tail)))
                if self.tail and source.read(len(self.tail)) != self.tail:
                    raise RuntimeError("Workshop log was rewritten; completion cannot be verified.")
                source.seek(self.offset)
                data = source.read(65536)
                self.offset += len(data)
                self.tail = (self.tail + data)[-64:]
        except FileNotFoundError:
            if self.identity is not None:
                raise RuntimeError("Workshop log disappeared; completion cannot be verified.")
            return []
        text = self.partial + data.decode("utf-8", errors="replace")
        parts = text.split("\n")
        self.partial = parts.pop()
        if len(self.partial) > 65536:
            raise RuntimeError("Oversized Workshop log line; completion cannot be verified.")
        return parts


class JobResult:
    def __init__(self, item):
        self.item = item
        self.requested = False
        self.ok = False
        self.done = False

    def feed(self, line):
        if "[AppID 107410]" not in line:
            return
        if f"Download item {self.item} requested by app" in line:
            if self.requested:
                raise RuntimeError("Duplicate Workshop request; refusing ambiguous completion.")
            self.requested = True
        if not self.requested:
            return
        if "Update canceled:" in line:
            raise RuntimeError("Workshop " + line.strip())
        result = re.search(r"Download item (\d+) result\s*:\s*(.+)", line)
        if result:
            if result.group(1) != self.item:
                raise RuntimeError("Unexpected Workshop item result; refusing ambiguous completion.")
            if result.group(2).strip() != "OK":
                raise RuntimeError(f"Workshop item {self.item} failed: {result.group(2).strip()}")
            self.ok = True
        if "Finished Workshop download job :" in line:
            status = line.split("Finished Workshop download job :", 1)[1].strip()
            if status != "No Error" or not self.ok:
                raise RuntimeError(f"Workshop item {self.item}: unconfirmed job finish ({status}).")
            self.done = True


class Console:
    def __init__(self, process):
        self.process = process
        self.events = queue.Queue()
        self.text = ""
        self.prompts = 0
        self.timeout_notified = False
        self.ever_timeout = False
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                data = os.read(self.process.stdout.fileno(), 4096)
                if not data:
                    break
                self.events.put(decoder.decode(data))
        except OSError as error:
            self.events.put(error)
        finally:
            self.events.put(None)

    def pump(self, item=None):
        try:
            event = self.events.get(timeout=0.25)
        except queue.Empty:
            return
        if event is None:
            raise RuntimeError("SteamCMD output closed before confirmed completion.")
        if isinstance(event, Exception):
            raise RuntimeError("Cannot read SteamCMD output.") from event
        print(event, end="", flush=True)
        self.text += event
        # Parse full lines and prompts, including those split across pipe reads.
        while True:
            boundary = re.search(r"\r|\n|Steam>", self.text)
            if boundary is None:
                break
            segment = self.text[:boundary.start()]
            self._check(segment, item, complete=True)
            if boundary.group() == "Steam>":
                self.prompts += 1
            self.text = self.text[boundary.end():]
        # Errors may have no newline before a long-running download.
        self._check(self.text, item)
        if len(self.text) > 65536:
            raise RuntimeError("Unrecognized oversized SteamCMD output; refusing to continue.")

    def _check(self, text, item, complete=False):
        if re.search(r"FAILED (?:login )?with result code", text, re.I):
            raise RuntimeError("Steam login failed. See SteamCMD output above.")
        if "ERROR!" not in text:
            return
        timeout = re.search(r"ERROR!\s*Timeout downloading item (\d+)\b", text, re.I)
        if timeout and item == timeout.group(1):
            rest = text[:timeout.start()] + text[timeout.end():]
            if "ERROR!" in rest:
                raise RuntimeError("SteamCMD command failed. See output above.")
            self.ever_timeout = True
            if not self.timeout_notified:
                print(f"\nWorkshop item {item}: command wait expired; keeping the same download alive, no retry.", flush=True)
                self.timeout_notified = True
        else:
            # Wait for a fragmented timeout/ERROR line to finish before classifying it.
            if complete or re.search(r"ERROR!.*(?:failed|Failure|Access Denied)", text, re.I):
                raise RuntimeError("SteamCMD command failed. See output above.")


def close_session(process, console):
    try:
        if process.poll() is None:
            try:
                process.stdin.write(b"quit\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)  # Also clean up descendants after wrapper exit.
        except ProcessLookupError:
            pass
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        console.thread.join(timeout=2)
        process.stdout.close()


def download(ids, steamcmd, install_dir):
    items = list(dict.fromkeys(str(item) for item in ids))
    if not items:
        return
    if any(not re.fullmatch(r"[0-9]+", item) for item in items):
        raise ValueError("Workshop IDs must contain digits only.")
    try:
        minutes = int(os.environ.get("WORKSHOP_TIMEOUT_MINUTES", "35"))
    except ValueError:
        raise ValueError("WORKSHOP_TIMEOUT_MINUTES must be a positive integer.") from None
    if minutes <= 0:
        raise ValueError("WORKSHOP_TIMEOUT_MINUTES must be a positive integer.")
    log_path = os.path.expanduser("~/Steam/logs/workshop_log.txt")
    user, password = os.environ["STEAM_USER"], os.environ["STEAM_PASSWORD"]
    print(f"SteamCMD: Workshop session, {minutes} minutes maximum per item; no retries.", flush=True)
    print(f"Logging in to Steam as '{user}' (password: {'set' if password else 'EMPTY'})...", flush=True)
    process = subprocess.Popen(
        [steamcmd, "+@ShutdownOnFailedCommand", "0", "+@NoPromptForPassword", "1",
         "+force_install_dir", install_dir, "+login", user, password],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True)
    console = Console(process)
    succeeded = False
    try:
        # Startup uses the same configured bound, not an unbounded login wait.
        deadline = time.monotonic() + minutes * 60
        while console.prompts == 0:
            if time.monotonic() >= deadline:
                raise RuntimeError("SteamCMD did not become ready within the configured Workshop wait.")
            console.pump()
        for item in items:
            log = JobLog(log_path)
            result = JobResult(item)
            console.timeout_notified = False
            prompt_before = console.prompts
            print(f"\nWorkshop item {item}: requesting once, waiting for fresh job confirmation.", flush=True)
            process.stdin.write(f"workshop_download_item 107410 {item}\n".encode("ascii"))
            process.stdin.flush()
            started = time.monotonic()
            deadline = started + minutes * 60
            next_status = started + 30
            while not (result.done and console.prompts > prompt_before):
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Workshop item {item} exceeded {minutes} minutes; aborting without retry.")
                now = time.monotonic()
                if now >= next_status:
                    print(f"\nWorkshop item {item}: waiting for job confirmation "
                          f"({int(now - started)}s elapsed, limit {minutes}min); no retry.", flush=True)
                    next_status = now + 30
                for line in log.lines():
                    result.feed(line)
                if result.done and console.prompts > prompt_before:
                    break
                console.pump(item)
            print(f"\nWorkshop item {item}: fresh result OK and job finished.", flush=True)
        succeeded = True
    finally:
        close_session(process, console)
    # SteamCMD may retain code 10 from the command wait timeout. Accept it
    # only after every fresh item job was confirmed OK, never as proof itself.
    accepted_codes = (0, 10) if console.ever_timeout else (0,)
    if succeeded and process.returncode not in accepted_codes:
        raise RuntimeError(f"SteamCMD exited with code {process.returncode} after Workshop completion.")
