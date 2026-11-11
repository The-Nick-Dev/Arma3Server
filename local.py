import os

import keys


def mods(d):
    mods = []

    # Find mod folders
    for m in os.listdir(os.path.join("server", d)):
        moddir = os.path.join(d, m)
        source = os.path.join("server", moddir)
        if os.path.isdir(source):
            print("Found mod:", moddir)
            mods.append(moddir)
            keys.copy(source)
    if not mods:
        print("No mods found in", os.path.join("server", d))

    return mods
