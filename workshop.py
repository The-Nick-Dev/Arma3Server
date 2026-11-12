import re
import urllib.request

import api
import keys
import local

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_9_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/35.0.1916.47 Safari/537.36"  # noqa: E501
WORKSHOP_CONTENT = "steamapps/workshop/content/107410/"


def preset(mod_file):
    if mod_file.startswith("http"):
        req = urllib.request.Request(
            mod_file,
            headers={"User-Agent": USER_AGENT},
        )
        remote = urllib.request.urlopen(req)
        with open("preset.html", "wb") as f:
            f.write(remote.read())
        mod_file = "preset.html"
    print("Reading mod preset:", mod_file)
    mods = []
    with open(mod_file) as f:
        html = f.read()
        regex = r"filedetails\/\?id=(\d+)\""
        matches = re.finditer(regex, html, re.MULTILINE)
        for _, match in enumerate(matches, start=1):
            mods.append(match.group(1))
            print(f"Mod {len(mods)}: workshop item {match.group(1)}")
    print(f"Preset: {len(mods)} mods")
    api.download_workshop(mods)
    moddirs = [WORKSHOP_CONTENT + mod for mod in mods]
    for moddir in moddirs:
        local.lowercase_pbo("server/" + moddir)
        keys.copy("server/" + moddir)
    return moddirs
