import os

import keys


def lowercase_pbo(root):
    """Lowercase the names of .pbo and .bisign files under root.

    Only file names change: no contents, no folders, no other file types.
    If any new name would collide with an existing file or with another
    renamed file, nothing is renamed and an error lists the collisions.
    """
    renames = []
    for folder, _, files in os.walk(root):
        for name in sorted(files):
            lower = name.lower()
            if lower != name and lower.endswith((".pbo", ".bisign")):
                source = os.path.join(folder, name)
                if not os.path.islink(source):
                    renames.append((source, os.path.join(folder, lower)))
    targets = [target for _, target in renames]
    collisions = [f"{source} -> {target}" for source, target in renames
                  if os.path.lexists(target) or targets.count(target) > 1]
    if collisions:
        raise RuntimeError("Lowercase: name collisions in {}, nothing was renamed. "
                           "Remove or rename one of each pair:\n{}".format(root, "\n".join(collisions)))
    for source, target in renames:
        os.rename(source, target)
    if renames:
        print(f"Lowercase: renamed {len(renames)} file(s) in {root}", flush=True)


def mods(d):
    mods = []

    # Find mod folders
    for m in os.listdir(os.path.join("server", d)):
        moddir = os.path.join(d, m)
        source = os.path.join("server", moddir)
        if os.path.isdir(source):
            print("Found mod:", moddir)
            mods.append(moddir)
            lowercase_pbo(source)
            keys.copy(source)
    if not mods:
        print("No mods found in", os.path.join("server", d))

    return mods
