import os

import keys


def mods(d):
    mods = []

    # Find mod folders
    for m in os.listdir(os.path.join("server", d)):
        moddir = os.path.join(d, m)
        source = os.path.join("server", moddir)
        if os.path.isdir(source):
            mods.append(moddir)
            keys.copy(source)

    return mods
